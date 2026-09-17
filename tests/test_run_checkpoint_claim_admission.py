"""Checked-claim admission against real SessionDB state; no live adoption."""
import dataclasses
import hashlib
import sqlite3
import subprocess
import sys

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustodyError


def sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def bound(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="current", source="cli")
    assert db.try_acquire_session_turn_lease("current", "holder", ttl_seconds=120)
    goal = '{"status":"cleared","outcome":"CANCELLED"}'
    db.set_meta("goal:historical", goal)
    checkpoint = RunCheckpoint(
        plan_digest=sha("plan"), contract_digest=sha("contract"), source_digest=sha("source"),
        next_action="Observe only; do not retry ambiguous effect",
        members=(("historical-goal-key", "goal:historical"), ("original-wip", "retained")),
        unresolved_effects=("push:unknown",), unresolved_findings=("open-finding",),
        restrictions=("no implicit effects",))
    kwargs = dict(expected_generation=0, checkpoint=checkpoint, origin_session_id="historical",
        current_session_id="current", historical_goal_digest=sha(goal), ttl_seconds=120)
    yield db, kwargs, goal
    db.close()


def checked(db, kwargs, holder="holder"):
    method = getattr(db, "claim_run_custody_checked", None)
    assert method is not None, "native checked-claim entrypoint missing"
    return method("claim-fixture", lease_holder=holder, **kwargs)


def run_rows(db):
    # Independent SQL readback, not the candidate's success return.
    with sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT key,value FROM state_meta WHERE key LIKE 'run-custody:%' ORDER BY key").fetchall()


def test_checked_claim_records_exact_checkpoint_without_goal_mutation(bound):
    db, kwargs, goal = bound
    value = checked(db, kwargs)
    assert value.generation == 1
    assert value.checkpoint == kwargs["checkpoint"]
    assert len(run_rows(db)) == 2
    assert db.read_run_custody("claim-fixture") == value
    assert db.get_meta("goal:historical") == goal


@pytest.mark.parametrize("holder", [None, "", " ", 1, False])
def test_checked_claim_never_falls_back_on_invalid_binding(bound, holder):
    db, kwargs, goal = bound
    with pytest.raises(RunCustodyError, match="INVALID_TEXT"):
        checked(db, kwargs, holder)
    assert run_rows(db) == []
    assert db.get_meta("goal:historical") == goal


@pytest.mark.parametrize("fault,code", [
    ("missing-session", "SESSION_MISMATCH"),
    ("ended-session", "SESSION_NOT_CURRENT"),
    ("compression-session", "SESSION_NOT_CURRENT"),
    ("missing-lease", "LEASE_MISMATCH"),
    ("wrong-lease", "LEASE_MISMATCH"),
    ("expired-lease", "LEASE_MISMATCH"),
    ("infinite-lease", "LEASE_MISMATCH"),
    ("missing-goal", "HISTORICAL_GOAL_MISMATCH"),
    ("changed-goal", "HISTORICAL_GOAL_MISMATCH"),
    ("missing-key", "HISTORICAL_GOAL_BINDING_MISSING"),
    ("wrong-key", "HISTORICAL_GOAL_MISMATCH"),
])
def test_checked_claim_refuses_invalid_native_bindings(bound, fault, code):
    db, kwargs, goal = bound
    if fault == "missing-session":
        kwargs["current_session_id"] = "absent"
    elif fault == "ended-session":
        db.end_session("current", "user_exit")
    elif fault == "compression-session":
        db.end_session("current", "compression")
    elif fault == "missing-lease":
        db.release_session_turn_lease("current", "holder")
    elif fault in {"wrong-lease", "expired-lease", "infinite-lease"}:
        sql, values = {
            "wrong-lease": ("UPDATE session_turn_leases SET holder=?", ("other",)),
            "expired-lease": ("UPDATE session_turn_leases SET expires_at=?", (0,)),
            "infinite-lease": ("UPDATE session_turn_leases SET expires_at=?", (float("inf"),)),
        }[fault]
        db._execute_write(lambda conn: conn.execute(sql, values))
    elif fault == "missing-goal":
        db._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key='goal:historical'"))
    elif fault == "changed-goal":
        db.set_meta("goal:historical", "changed")
    else:
        members = () if fault == "missing-key" else (("historical-goal-key", "goal:other"),)
        kwargs["checkpoint"] = dataclasses.replace(kwargs["checkpoint"], members=members)
    before = db.get_meta("goal:historical")
    with pytest.raises(RunCustodyError, match=code):
        checked(db, kwargs)
    assert run_rows(db) == []
    assert db.get_meta("goal:historical") == before


@pytest.mark.parametrize("fault,code", [
    ("session", "SESSION_NOT_CURRENT"),
    ("lease", "LEASE_MISMATCH"),
    ("goal", "HISTORICAL_GOAL_MISMATCH"),
    ("control", None),
])
def test_competing_process_changes_binding_before_write_admission(bound, monkeypatch, fault, code):
    db, kwargs, goal = bound
    child_code = r'''
import json,sqlite3,sys
conn=sqlite3.connect(sys.argv[1],timeout=10,isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
changes={
 "session": ("UPDATE sessions SET end_reason='compression' WHERE id='current'",()),
 "lease": ("UPDATE session_turn_leases SET holder='other'",()),
 "goal": ("UPDATE state_meta SET value='changed' WHERE key='goal:historical'",()),
 "control": ("SELECT 1",()),
}
conn.execute(*changes[sys.argv[2]])
print("holding",flush=True)
if sys.stdin.readline().strip() != "commit": raise SystemExit(3)
conn.commit(); conn.close(); print("committed",flush=True)
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", child_code, str(db.db_path), fault],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout is not None
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    real_write = db._execute_write
    admitted = False
    try:
        assert pool.submit(child.stdout.readline).result(timeout=10).strip() == "holding"
        def barrier(fn, *args, **kwargs):
            nonlocal admitted
            admitted = True
            # The claim has already pre-read its head. A real separate process
            # commits the changed binding before the native write admission.
            out, err = child.communicate("commit\n", timeout=10)
            assert child.returncode == 0, err
            assert out.strip() == "committed"
            return real_write(fn, *args, **kwargs)
        monkeypatch.setattr(db, "_execute_write", barrier)
        if code is None:
            assert checked(db, kwargs).checkpoint == kwargs["checkpoint"]
            assert len(run_rows(db)) == 2
        else:
            with pytest.raises(RunCustodyError, match=code):
                checked(db, kwargs)
            assert run_rows(db) == []
        assert admitted
        assert db.get_meta("goal:historical") == ("changed" if fault == "goal" else goal)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        pool.shutdown(wait=True, cancel_futures=True)


def test_checked_claim_uses_existing_compression_lease_root(bound):
    db, kwargs, goal = bound
    db.end_session("current", "compression")
    db.create_session(session_id="continued", source="cli", parent_session_id="current")
    kwargs["current_session_id"] = "continued"
    value = checked(db, kwargs)
    assert value.current_session_id == "continued"
    assert db.get_meta("goal:historical") == goal


def test_checked_claim_rollback_leaves_no_partial_generation(bound):
    db, kwargs, goal = bound
    db._conn.execute("CREATE TRIGGER reject_checked_head BEFORE INSERT ON state_meta "
        "WHEN NEW.key LIKE 'run-custody:%:head' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        checked(db, kwargs)
    assert run_rows(db) == []
    assert db.get_meta("goal:historical") == goal


def test_checked_claim_retains_takeover_and_aba_fences(bound):
    db, kwargs, goal = bound
    first = checked(db, kwargs)
    with pytest.raises(RunCustodyError, match="FENCE_MISMATCH"):
        checked(db, kwargs)
    kwargs["expected_generation"] = 1
    with pytest.raises(RunCustodyError, match="OWNER_ACTIVE"):
        checked(db, kwargs)
    released = db.release_run_custody("claim-fixture", owner_token=first.owner_token, expected_generation=1)
    kwargs["expected_generation"] = released.generation
    bad = dict(kwargs, checkpoint=dataclasses.replace(kwargs["checkpoint"], unresolved_effects=()))
    with pytest.raises(RunCustodyError, match="TAKEOVER_REQUIRES_EXACT_CHECKPOINT"):
        checked(db, bad)
    current = checked(db, kwargs)
    assert current.owner_token != first.owner_token
    assert current.checkpoint == first.checkpoint
    assert db.read_run_checkpoint("claim-fixture", generation=1) == first
    assert db.get_meta("goal:historical") == goal
