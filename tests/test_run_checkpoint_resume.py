"""Read-only consumer witnesses with real native storage and subprocesses."""
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_checkpoint_resume.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_consumer_entrypoint_available():
    assert SCRIPT.is_file(), "read-only native resume consumer missing"


@pytest.fixture
def client():
    spec = importlib.util.spec_from_file_location("run_checkpoint_resume", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bound(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="current", source="cli")
    holder = "fixture-controller"
    assert db.try_acquire_session_turn_lease("current", holder, ttl_seconds=120)
    goal_key = "goal:historical"
    goal = '{"status":"cleared","outcome":"CANCELLED","checkpoint":"retained"}'
    db.set_meta(goal_key, goal)
    source = tmp_path / "source.dat"
    source.write_bytes(b"source before")
    source_manifest = json.dumps({str(source): sha(source.read_bytes())}).encode()
    files = {}
    for role, raw in {"plan": b"plan", "contract": b"contract", "source": source_manifest}.items():
        path = tmp_path / (role + ".json")
        path.write_bytes(raw)
        files[role] = str(path)
    members = (("historical-goal-key", goal_key), ("authority", "No implicit effect retry"))
    files["members"] = {}
    for name, raw in members:
        path = tmp_path / (name + ".txt")
        path.write_text(raw)
        files["members"][name] = str(path)
    checkpoint = RunCheckpoint(
        plan_digest=sha(b"plan"), contract_digest=sha(b"contract"),
        source_digest=sha(source_manifest), next_action="Reconcile ambiguous push, do not retry",
        members=members, unresolved_effects=("push:unknown",),
        unresolved_findings=("FTS:open",), restrictions=("no live activation",),
    )
    owner = db.claim_run_custody("resume-fixture", expected_generation=0,
        checkpoint=checkpoint, origin_session_id="origin", current_session_id="current",
        historical_goal_digest=sha(goal.encode()), ttl_seconds=120)
    request = {"checkpoint": dataclasses.asdict(checkpoint), "files": files,
               "session_id": "current", "lease_holder": holder}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    yield db, owner, request, request_path, source
    db.close()


def invoke(client, bound, generation=1):
    db, owner, request, request_path, source = bound
    request_path.write_text(json.dumps(request))
    return client.inspect_resume(db.db_path, "resume-fixture", generation, request_path)


def native_rows(db):
    with db._read_ctx() as conn:
        return tuple(conn.execute("SELECT key,value FROM state_meta ORDER BY key").fetchall())


def test_real_consumer_reads_native_state_without_authority_or_mutation(client, bound):
    db, owner, *_ = bound
    before = native_rows(db)
    result = invoke(client, bound)
    assert result["status"] == "resume_consistency"
    assert result["resume_authorized"] is False
    assert result["effects_executed"] is False
    assert result["unresolved_effects"] == 1
    assert result["generation"] == owner.generation
    assert owner.owner_token not in json.dumps(result)
    assert before == native_rows(db)


def test_subprocess_restart_reads_same_checkpoint_and_redacts_tokens(client, bound):
    db, owner, request, request_path, source = bound
    before = native_rows(db)
    command = [sys.executable, str(SCRIPT), "--db", str(db.db_path),
               "--run-id", "resume-fixture", "--generation", "1", "--request", str(request_path)]
    for _ in range(2):
        child = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert child.returncode == 0, child.stderr + child.stdout
        output = json.loads(child.stdout)
        assert output["status"] == "resume_consistency"
        assert output["resume_authorized"] is False
        assert owner.owner_token not in child.stdout + child.stderr
        assert request["lease_holder"] not in child.stdout + child.stderr
    assert before == native_rows(db)


@pytest.mark.parametrize("field", ["unresolved_effects", "unresolved_findings", "restrictions", "members"])
def test_candidate_cannot_omit_native_inventory(client, bound, field):
    db, owner, request, *_ = bound
    before = native_rows(db)
    request["checkpoint"][field] = []
    with pytest.raises(client.ResumeRefusal, match="CHECKPOINT_MISMATCH"):
        invoke(client, bound)
    assert before == native_rows(db)


def test_self_consistent_candidate_cannot_replace_native_digest(client, bound):
    db, owner, request, request_path, source = bound
    source.write_bytes(b"substitution")
    manifest = json.dumps({str(source): sha(source.read_bytes())}).encode()
    Path(request["files"]["source"]).write_bytes(manifest)
    request["checkpoint"]["source_digest"] = sha(manifest)
    with pytest.raises(client.ResumeRefusal, match="CHECKPOINT_MISMATCH"):
        invoke(client, bound)


@pytest.mark.parametrize("role", ["plan", "contract", "source", "member", "source_file"])
def test_actual_file_drift_refuses_even_when_export_is_unchanged(client, bound, role):
    db, owner, request, request_path, source = bound
    path = source if role == "source_file" else Path(
        request["files"]["members"]["authority"] if role == "member" else request["files"][role])
    path.write_bytes(b"drift")
    before = native_rows(db)
    with pytest.raises(client.ResumeRefusal, match="FILE_DIGEST_MISMATCH|MEMBER_MISMATCH"):
        invoke(client, bound)
    assert before == native_rows(db)


def test_missing_member_binding_refuses(client, bound):
    bound[2]["files"]["members"].pop("authority")
    with pytest.raises(client.ResumeRefusal, match="MEMBER_BINDINGS_MISMATCH"):
        invoke(client, bound)


@pytest.mark.parametrize("fault", ["missing", "wrong", "expired", "session"])
def test_native_session_lease_is_checked(client, bound, fault):
    db, owner, request, *_ = bound
    if fault == "missing":
        db.release_session_turn_lease("current", request["lease_holder"])
    elif fault == "wrong":
        request["lease_holder"] = "not-owner"
    elif fault == "expired":
        db._execute_write(lambda conn: conn.execute("UPDATE session_turn_leases SET expires_at=0"))
    else:
        request["session_id"] = "absent"
    before = native_rows(db)
    with pytest.raises(client.ResumeRefusal, match="SESSION_MISMATCH|LEASE_MISMATCH"):
        invoke(client, bound)
    assert before == native_rows(db)


@pytest.mark.parametrize("reason", ["compression", "user_exit"])
def test_closed_session_is_not_a_current_resume_target(client, bound, reason):
    db, *_ = bound
    db.end_session("current", reason)
    before = native_rows(db)
    with pytest.raises(client.ResumeRefusal, match="SESSION_NOT_CURRENT"):
        invoke(client, bound)
    assert before == native_rows(db)


def test_historical_goal_change_refuses_without_revival(client, bound):
    db, *_ = bound
    db.set_meta("goal:historical", '{"status":"changed"}')
    before = native_rows(db)
    with pytest.raises(client.ResumeRefusal, match="HISTORICAL_GOAL_MISMATCH"):
        invoke(client, bound)
    assert before == native_rows(db)


def test_stale_generation_refuses(client, bound):
    with pytest.raises(client.ResumeRefusal, match="FENCE_MISMATCH"):
        invoke(client, bound, generation=2)


def test_released_owner_refuses(client, bound):
    db, owner, *_ = bound
    db.release_run_custody("resume-fixture", owner_token=owner.owner_token, expected_generation=1)
    with pytest.raises(client.ResumeRefusal, match="STALE_OWNER"):
        invoke(client, bound, generation=2)


def test_native_update_during_file_read_is_detected(client, bound, monkeypatch):
    db, owner, *_ = bound
    real_read = client.read_file_bytes
    fired = False
    def read_and_publish(path):
        nonlocal fired
        raw = real_read(path)
        if not fired and str(path).endswith("plan.json"):
            fired = True
            db.refresh_run_custody("resume-fixture", owner_token=owner.owner_token, expected_generation=1)
        return raw
    monkeypatch.setattr(client, "read_file_bytes", read_and_publish)
    with pytest.raises(client.ResumeRefusal, match="NATIVE_STATE_CHANGED|FENCE_MISMATCH"):
        invoke(client, bound)
    assert fired
    assert db.read_run_custody("resume-fixture").generation == 2


def test_read_only_sessiondb_rejects_write(client, bound):
    db, *_ = bound
    readonly = SessionDB(db_path=db.db_path, read_only=True)
    try:
        with pytest.raises(Exception, match="read.only|readonly"):
            readonly.set_meta("forbidden", "value")
    finally:
        readonly.close()
    assert db.get_meta("forbidden") is None


def test_nonexistent_store_is_not_created(client, bound, tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(client.ResumeRefusal, match="INVALID_DB"):
        client.inspect_resume(missing, "resume-fixture", 1, bound[3])
    assert not missing.exists()


def test_duplicate_request_keys_refuse(client, bound):
    db, owner, request, path, source = bound
    path.write_text('{"checkpoint":{},"checkpoint":{}}')
    with pytest.raises(client.ResumeRefusal, match="DUPLICATE_KEY"):
        client.inspect_resume(db.db_path, "resume-fixture", 1, path)


@pytest.mark.linux_only
def test_symlink_and_fifo_are_refused_without_blocking(client, bound, tmp_path):
    target = tmp_path / "link"
    target.symlink_to(bound[4])
    bound[2]["files"]["plan"] = str(target)
    with pytest.raises(client.ResumeRefusal, match="INVALID_FILE"):
        invoke(client, bound)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    bound[2]["files"]["plan"] = str(fifo)
    with pytest.raises(client.ResumeRefusal, match="INVALID_FILE"):
        invoke(client, bound)


def test_missing_request_argument_has_no_default_store(client, tmp_path):
    child = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 2
    assert not (tmp_path / "state.db").exists()
