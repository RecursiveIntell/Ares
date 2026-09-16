"""Run-scoped SessionDB owner witnesses; not a release/custody certificate."""
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_state import SessionDB


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def api():
    # Fail on an absent owner API rather than silently exercising a fake store.
    assert hasattr(SessionDB, "claim_run_custody"), "native run custody API missing"
    return importlib.import_module("hermes_state_runs")


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield store
    finally:
        store.close()


def checkpoint(api, **changes):
    value = api.RunCheckpoint(
        plan_digest=digest("plan"), contract_digest=digest("contract"),
        source_digest=digest("source"), next_action="W01: reconcile source",
        members=(("authority", "Do not revive the cancelled goal"),),
        unresolved_effects=("push:unknown",), unresolved_findings=("F01",),
        restrictions=("no force push",),
    )
    return dataclasses.replace(value, **changes)


def claim(db, api):
    return db.claim_run_custody(
        "test-run", expected_generation=0, checkpoint=checkpoint(api),
        origin_session_id="origin", current_session_id="current",
        historical_goal_digest=digest("cancelled"), ttl_seconds=60,
    )


def test_owner_claim_preserves_goal_and_readback_after_lost_ack(db, api):
    db.set_meta("goal:origin", '{"status":"cleared","outcome":"CANCELLED"}')
    before = db.get_meta("goal:origin")
    owner = claim(db, api)
    assert owner.generation == 1
    assert owner.checkpoint == checkpoint(api)
    assert db.read_run_custody("test-run") == owner
    with pytest.raises(api.RunCustodyError, match="FENCE_MISMATCH"):
        claim(db, api)
    assert db.read_run_custody("test-run") == owner
    assert db.get_meta("goal:origin") == before


def test_checkpoint_publish_requires_current_fence_and_owner(db, api):
    owner = claim(db, api)
    published = db.publish_run_checkpoint(
        "test-run", owner_token=owner.owner_token, expected_generation=1,
        expected_source_digest=owner.checkpoint.source_digest,
        checkpoint=checkpoint(api, next_action="W02: inspect"), ttl_seconds=60,
    )
    assert published.generation == 2
    for token, generation in [(owner.owner_token, 1), ("wrong", 2)]:
        with pytest.raises(api.RunCustodyError):
            db.publish_run_checkpoint(
                "test-run", owner_token=token, expected_generation=generation,
                expected_source_digest=owner.checkpoint.source_digest,
                checkpoint=checkpoint(api), ttl_seconds=60,
            )
    assert db.read_run_custody("test-run") == published
    assert db.read_run_checkpoint("test-run", generation=1) == owner


@pytest.mark.parametrize("field,value", [
    ("unresolved_effects", ()), ("unresolved_findings", ()),
    ("restrictions", ()), ("members", ()),
])
def test_publish_cannot_drop_obligations_or_expected_members(db, api, field, value):
    owner = claim(db, api)
    with pytest.raises(api.RunCustodyError, match="OMITTED"):
        db.publish_run_checkpoint(
            "test-run", owner_token=owner.owner_token, expected_generation=1,
            expected_source_digest=owner.checkpoint.source_digest,
            checkpoint=checkpoint(api, **{field: value}), ttl_seconds=60,
        )
    assert db.read_run_custody("test-run") == owner


@pytest.mark.parametrize("field,value,code", [
    ("source_digest", digest("substituted-source"), "SOURCE_MISMATCH"),
    ("members", (("authority", "revive the cancelled goal"),), "SUBSTITUTED_MEMBER"),
])
def test_publish_cannot_substitute_bound_source_or_member(db, api, field, value, code):
    owner = claim(db, api)
    with pytest.raises(api.RunCustodyError, match=code):
        db.publish_run_checkpoint(
            "test-run", owner_token=owner.owner_token, expected_generation=1,
            expected_source_digest=owner.checkpoint.source_digest,
            checkpoint=checkpoint(api, **{field: value}), ttl_seconds=60,
        )
    assert db.read_run_custody("test-run") == owner
    assert db.get_meta(api.generation_key("test-run", 2)) is None


def test_publish_can_append_members_without_replacing_bound_values(db, api):
    owner = claim(db, api)
    new = checkpoint(api, members=owner.checkpoint.members + (("observation-2", "new evidence"),))
    published = db.publish_run_checkpoint(
        "test-run", owner_token=owner.owner_token, expected_generation=1,
        expected_source_digest=owner.checkpoint.source_digest, checkpoint=new, ttl_seconds=60)
    assert db.read_run_custody("test-run") == published
    assert published.checkpoint.members == new.members
    assert db.read_run_checkpoint("test-run", generation=1) == owner


def test_source_drift_refuses_publish_and_resume(db, api):
    owner = claim(db, api)
    with pytest.raises(api.RunCustodyError, match="SOURCE_MISMATCH"):
        db.publish_run_checkpoint(
            "test-run", owner_token=owner.owner_token, expected_generation=1,
            expected_source_digest=digest("changed"), checkpoint=checkpoint(api),
            ttl_seconds=60,
        )
    with pytest.raises(api.RunCustodyError, match="SOURCE_MISMATCH"):
        db.validate_run_resume("test-run", owner_token=owner.owner_token,
            expected_generation=1, source_digest=digest("changed"),
            plan_digest=digest("plan"), contract_digest=digest("contract"))
    result = db.validate_run_resume("test-run", owner_token=owner.owner_token,
        expected_generation=1, source_digest=digest("source"),
        plan_digest=digest("plan"), contract_digest=digest("contract"))
    assert result.checkpoint.unresolved_effects == ("push:unknown",)
    # A validated read preserves an ambiguous effect, it never retries it.
    assert db.read_run_custody("test-run").generation == 1


def test_release_reclaim_aba_invalidates_token_and_preserves_history(db, api):
    old = claim(db, api)
    released = db.release_run_custody("test-run", owner_token=old.owner_token,
        expected_generation=1)
    assert released.generation == 2 and released.disposition == "released"
    new = db.claim_run_custody("test-run", expected_generation=2,
        checkpoint=released.checkpoint, origin_session_id="origin",
        current_session_id="new-session", historical_goal_digest=digest("cancelled"),
        ttl_seconds=60)
    assert new.generation == 3 and new.owner_token != old.owner_token
    for method in (db.refresh_run_custody, db.release_run_custody):
        with pytest.raises(api.RunCustodyError, match="STALE_OWNER"):
            method("test-run", owner_token=old.owner_token, expected_generation=3)
    assert db.read_run_checkpoint("test-run", generation=1) == old
    assert db.read_run_custody("test-run") == new


def test_active_owner_cannot_be_taken_over_even_by_same_process(db, api):
    old = claim(db, api)
    with pytest.raises(api.RunCustodyError, match="OWNER_ACTIVE"):
        db.claim_run_custody("test-run", expected_generation=1,
            checkpoint=old.checkpoint, origin_session_id="origin",
            current_session_id="other", historical_goal_digest=digest("cancelled"),
            ttl_seconds=60)


def test_member_substitution_cannot_replace_native_expected_digest(db, api):
    old = claim(db, api)
    # Same-UID raw SQL is outside the cooperating-writer fence. Readback must
    # still detect member-only corruption against the separately stored head.
    key = api.generation_key("test-run", 1)
    raw = json.loads(db.get_meta(key))
    raw["checkpoint"]["members"][0][1] = "silently allow force push"
    db.set_meta(key, json.dumps(raw, sort_keys=True, separators=(",", ":")))
    with pytest.raises(api.RunCustodyError, match="INTEGRITY"):
        db.read_run_custody("test-run")
    assert old.generation == 1


def test_sql_failure_after_member_before_head_rolls_back_whole_generation(db, api):
    old = claim(db, api)
    db._conn.execute(
        "CREATE TRIGGER reject_head BEFORE UPDATE ON state_meta "
        "WHEN NEW.key LIKE 'run-custody:%:head' "
        "BEGIN SELECT RAISE(ABORT, 'injected head failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.refresh_run_custody("test-run", owner_token=old.owner_token,
            expected_generation=1)
    assert db.get_meta(api.generation_key("test-run", 2)) is None
    assert db.read_run_custody("test-run") == old


@pytest.mark.parametrize("field,value", [
    ("plan_digest", ""), ("source_digest", None), ("contract_digest", "x"),
    ("next_action", ""), ("unresolved_effects", ("a", "a")),
    ("members", (("x", "1"), ("x", "2"))),
])
def test_malformed_checkpoint_is_refused_without_head(db, api, field, value):
    with pytest.raises((api.RunCustodyError, ValueError, TypeError)):
        db.claim_run_custody("test-run", expected_generation=0,
            checkpoint=checkpoint(api, **{field: value}), origin_session_id="origin",
            current_session_id="current", historical_goal_digest=digest("cancelled"),
            ttl_seconds=60)
    assert db.read_run_custody("test-run") is None


def test_owner_module_is_in_the_installable_module_set():
    import tomllib
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert "hermes_state_runs" in project["tool"]["setuptools"]["py-modules"]


def test_expiry_while_waiting_for_write_lock_cannot_refresh(db, api, monkeypatch):
    old = claim(db, api)
    real_write = db._execute_write

    def delayed_write(fn, *args, **kwargs):
        # Deterministic admission barrier: the original lease expires while
        # the new generation waits, before any SQL mutation can commit.
        monkeypatch.setattr(api.time, "monotonic_ns", lambda: old.expires_monotonic_ns + 1)
        return real_write(fn, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", delayed_write)
    with pytest.raises(api.RunCustodyError, match="OWNER_EXPIRED"):
        db.refresh_run_custody("test-run", owner_token=old.owner_token,
            expected_generation=1, ttl_seconds=3600)
    assert db.read_run_custody("test-run") == old


def test_expired_owner_refuses_refresh_but_can_explicitly_release(db, api, monkeypatch):
    old = claim(db, api)
    monkeypatch.setattr(api.time, "monotonic_ns", lambda: old.expires_monotonic_ns)
    with pytest.raises(api.RunCustodyError, match="OWNER_EXPIRED"):
        db.refresh_run_custody("test-run", owner_token=old.owner_token, expected_generation=1)
    released = db.release_run_custody("test-run", owner_token=old.owner_token, expected_generation=1)
    assert released.disposition == "released"


def test_process_start_identity_mismatch_refuses_current_token(db, api, monkeypatch):
    old = claim(db, api)
    real_identity = api._process_identity
    monkeypatch.setattr(api, "_process_identity",
        lambda pid: "different-start" if pid == old.controller_pid else real_identity(pid))
    with pytest.raises(api.RunCustodyError, match="STALE_PROCESS"):
        db.refresh_run_custody("test-run", owner_token=old.owner_token, expected_generation=1)
    assert db.read_run_custody("test-run") == old


_WORKER = r'''
import json, os, sys
from pathlib import Path
from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustodyError
from dataclasses import asdict
store = SessionDB(db_path=Path(sys.argv[1]))
mode = sys.argv[2]
checkpoint = RunCheckpoint.from_dict(json.loads(sys.argv[3]))
if mode == "crash-before-head":
    store._conn.create_function("crash_now", 0, lambda: os._exit(81))
    store._conn.execute("CREATE TEMP TRIGGER crash_head BEFORE INSERT ON state_meta "
        "WHEN NEW.key LIKE 'run-custody:%:head' BEGIN SELECT crash_now(); END")
print("ready", flush=True)
assert sys.stdin.readline().strip() == "go"
try:
    value = store.claim_run_custody("test-run", expected_generation=0,
        checkpoint=checkpoint, origin_session_id="origin", current_session_id="worker",
        historical_goal_digest=sys.argv[4], ttl_seconds=60)
    if mode == "crash-after-commit":
        os._exit(82)
    print(json.dumps({"result":"won", "value":asdict(value)}), flush=True)
except RunCustodyError as exc:
    print(json.dumps({"result":exc.code}), flush=True)
store.close()
'''


def start_worker(tmp_path, api, mode):
    import hermes_state
    return subprocess.Popen([sys.executable, "-c", _WORKER,
        str(tmp_path / "state.db"), mode, json.dumps(dataclasses.asdict(checkpoint(api))),
        digest("cancelled")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(hermes_state.__file__).resolve().parent),
             "HERMES_HOME": str(tmp_path / "worker-home")})


def ready(worker):
    # The pipe read has an independent deadline, even before the outer test
    # runner deadline. Linux marker below admits this real pipe observer.
    import select
    assert select.select([worker.stdout], [], [], 15)[0], "worker readiness timeout"
    assert worker.stdout.readline().strip() == "ready"


def cleanup(worker):
    if worker.poll() is None:
        worker.kill()
    worker.communicate(timeout=10)


@pytest.mark.linux_only
def test_independent_process_claims_have_one_native_winner(tmp_path, api):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.close()
    workers = []
    try:
        for _ in range(2):
            worker = start_worker(tmp_path, api, "claim")
            workers.append(worker)
            ready(worker)
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        outcomes = []
        for worker in workers:
            out, err = worker.communicate(timeout=20)
            assert worker.returncode == 0, err
            outcomes.append(json.loads(out))
        assert sorted(item["result"] for item in outcomes) == ["FENCE_MISMATCH", "won"]
        store = SessionDB(db_path=tmp_path / "state.db")
        try:
            winner = next(item["value"] for item in outcomes if item["result"] == "won")
            observed = store.read_run_custody("test-run")
            assert observed.owner_token == winner["owner_token"]
            assert observed.generation == 1
            # Both contenders exited: takeover preserves the exact checkpoint,
            # gets a fresh token, and invalidates the old token without ABA.
            taken = store.claim_run_custody("test-run", expected_generation=1,
                checkpoint=observed.checkpoint, origin_session_id="origin",
                current_session_id="parent", historical_goal_digest=digest("cancelled"))
            assert taken.generation == 2 and taken.owner_token != observed.owner_token
            with pytest.raises(api.RunCustodyError, match="STALE_OWNER"):
                store.refresh_run_custody("test-run", owner_token=observed.owner_token,
                    expected_generation=2)
        finally:
            store.close()
    finally:
        for worker in workers:
            cleanup(worker)


@pytest.mark.linux_only
@pytest.mark.parametrize("mode,exit_code,committed", [
    ("crash-before-head", 81, False), ("crash-after-commit", 82, True),
])
def test_process_crash_exposes_only_old_or_complete_generation(tmp_path, api, mode, exit_code, committed):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.set_meta("goal:origin", "cancelled goal bytes")
    store.close()
    worker = start_worker(tmp_path, api, mode)
    try:
        ready(worker)
        out, err = worker.communicate("go\n", timeout=20)
        assert worker.returncode == exit_code, (out, err)
        store = SessionDB(db_path=tmp_path / "state.db")
        try:
            value = store.read_run_custody("test-run")
            assert (value is not None) == committed
            if committed:
                assert value.checkpoint == checkpoint(api)
            else:
                assert store.get_meta(api.generation_key("test-run", 1)) is None
            assert store.get_meta("goal:origin") == "cancelled goal bytes"
            assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            store.close()
    finally:
        cleanup(worker)
