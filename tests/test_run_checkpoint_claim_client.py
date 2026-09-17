"""Private checked-claim client, exercised against real SessionDB fixtures."""
import dataclasses
import hashlib
import importlib
import json
import os
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def client_module():
    try:
        return importlib.import_module("scripts.run_checkpoint_claim")
    except ModuleNotFoundError:
        pytest.fail("private file-bound claim client missing")


@pytest.fixture
def bound(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="current", source="cli")
    assert db.try_acquire_session_turn_lease("current", "holder", ttl_seconds=120)
    goal = '{"status":"cleared","outcome":"CANCELLED"}'
    db.set_meta("goal:old", goal)
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact source")
    inventory = json.dumps({str(source): sha(source.read_bytes())}).encode()
    files = {}
    for name, raw in {"plan": b"plan", "contract": b"contract", "source": inventory}.items():
        file = tmp_path / name
        file.write_bytes(raw)
        files[name] = str(file)
    members = (("historical-goal-key", "goal:old"), ("authority", "Never retry unknown effects"))
    files["members"] = {}
    for name, raw in members:
        file = tmp_path / (name + ".txt")
        file.write_text(raw)
        files["members"][name] = str(file)
    cp = RunCheckpoint(sha(b"plan"), sha(b"contract"), sha(inventory), "Reconcile only",
                       members, ("push:unknown",), ("FTS:open",), ("no activation",))
    request = {"checkpoint": dataclasses.asdict(cp), "files": files,
               "session_id": "current", "lease_holder": "holder"}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    kwargs = dict(run_id="client-fixture", expected_generation=0, request_path=path,
                  expected_request_digest=sha(path.read_bytes()), origin_session_id="old",
                  historical_goal_digest=sha(goal.encode()), controller_pid=os.getpid(), ttl_seconds=120)
    yield db, cp, request, kwargs, source, goal
    db.close()


def rows(db):
    with sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT key,value FROM state_meta ORDER BY key").fetchall()


def call(bound):
    db, _, _, kwargs, *_ = bound
    return client_module().claim_from_files(db, **kwargs)


def rewrite_request(bound):
    _, _, request, kwargs, *_ = bound
    kwargs["request_path"].write_text(json.dumps(request))
    kwargs["expected_request_digest"] = sha(kwargs["request_path"].read_bytes())


def test_client_claims_then_read_only_observer_sees_exact_native_state(bound):
    db, cp, request, kwargs, source, goal = bound
    result = call(bound)
    assert result["status"] == "claim_observed"
    assert result["generation"] == 1
    assert result["downstream_effects_executed"] is False
    assert result["resume_authorized"] is False
    owner = db.read_run_custody("client-fixture")
    assert owner.checkpoint == cp
    assert db.get_meta("goal:old") == goal
    assert owner.owner_token not in json.dumps(result)
    assert "holder" not in json.dumps(result)
    observer = importlib.import_module("scripts.run_checkpoint_resume")
    before = rows(db)
    assert observer.inspect_resume(db.db_path, "client-fixture", 1, kwargs["request_path"])["status"] == "resume_consistency"
    assert before == rows(db)
    db.set_meta("handle-still-owned", "usable")
    assert db.get_meta("handle-still-owned") == "usable"


@pytest.mark.parametrize("fault", ["plan", "contract", "source", "member", "inventory", "request",
                                    "empty-inventory", "missing-member", "extra-member", "extra-key",
                                    "duplicate-key", "invalid-digest", "symlink"])
def test_file_refusal_happens_before_native_mutation(bound, fault):
    module = client_module()
    db, cp, request, kwargs, source, goal = bound
    from pathlib import Path
    if fault in {"plan", "contract", "source"}:
        Path(request["files"][fault]).write_bytes(b"changed")
    elif fault == "member":
        Path(request["files"]["members"]["authority"]).write_text("retry now")
    elif fault == "inventory":
        source.write_bytes(b"changed")
    elif fault == "request":
        kwargs["request_path"].write_bytes(b"{}")
    elif fault == "empty-inventory":
        Path(request["files"]["source"]).write_bytes(b"{}")
        request["checkpoint"]["source_digest"] = sha(b"{}")
        rewrite_request(bound)
    elif fault in {"missing-member", "extra-member"}:
        if fault == "missing-member":
            del request["files"]["members"]["authority"]
        else:
            request["files"]["members"]["extra"] = str(source)
        rewrite_request(bound)
    elif fault == "extra-key":
        request["grant"] = True
        rewrite_request(bound)
    elif fault == "duplicate-key":
        kwargs["request_path"].write_bytes(b'{"checkpoint":{},"checkpoint":{}}')
        kwargs["expected_request_digest"] = sha(kwargs["request_path"].read_bytes())
    elif fault == "invalid-digest":
        kwargs["expected_request_digest"] = ""
    else:
        link = source.with_name("source-link")
        link.symlink_to(source)
        request["files"]["plan"] = str(link)
        rewrite_request(bound)
    before = rows(db)
    with pytest.raises(module.ClaimRefusal):
        call(bound)
    assert rows(db) == before


@pytest.mark.parametrize("fault", ["session", "lease", "goal", "goal-digest", "controller", "generation"])
def test_client_uses_checked_native_admission_without_fallback(bound, fault):
    module = client_module()
    db, cp, request, kwargs, source, goal = bound
    if fault == "session":
        db.end_session("current", "compression")
    elif fault == "lease":
        db.release_session_turn_lease("current", "holder")
    elif fault == "goal":
        db.set_meta("goal:old", "changed")
    elif fault == "goal-digest":
        kwargs["historical_goal_digest"] = "bad"
    elif fault == "controller":
        kwargs["controller_pid"] = -1
    else:
        kwargs["expected_generation"] = 2
    before = rows(db)
    with pytest.raises(module.ClaimRefusal):
        call(bound)
    assert rows(db) == before


def test_read_only_handle_is_refused_and_not_closed(bound):
    module = client_module()
    db, cp, request, kwargs, source, goal = bound
    with SessionDB(db_path=db.db_path, read_only=True) as ro:
        with pytest.raises(module.ClaimRefusal, match="WRITABLE_OWNER_REQUIRED"):
            module.claim_from_files(ro, **kwargs)
        assert ro.get_meta("goal:old") == goal
    assert db.read_run_custody("client-fixture") is None


def test_lost_ack_is_unknown_and_retry_does_not_duplicate(bound, monkeypatch):
    module = client_module()
    db, cp, request, kwargs, source, goal = bound
    original = db.claim_run_custody_checked
    def lost_ack(*args, **kw):
        original(*args, **kw)
        raise OSError("sensitive implementation detail")
    monkeypatch.setattr(db, "claim_run_custody_checked", lost_ack)
    with pytest.raises(module.ClaimOutcomeUnknown, match="CLAIM_OUTCOME_UNKNOWN"):
        call(bound)
    assert db.read_run_custody("client-fixture").generation == 1
    before = rows(db)
    monkeypatch.setattr(db, "claim_run_custody_checked", original)
    with pytest.raises(module.ClaimRefusal, match="FENCE_MISMATCH"):
        call(bound)
    assert rows(db) == before
    assert db.get_meta("goal:old") == goal


def test_readback_failure_never_reports_refusal_or_retries(bound, monkeypatch):
    module = client_module()
    db, cp, request, kwargs, source, goal = bound
    original = db.read_run_custody
    monkeypatch.setattr(db, "read_run_custody", lambda *args: None)
    with pytest.raises(module.ClaimOutcomeUnknown, match="CLAIM_READBACK_MISMATCH"):
        call(bound)
    monkeypatch.setattr(db, "read_run_custody", original)
    assert db.read_run_custody("client-fixture").generation == 1
    assert db.get_meta("goal:old") == goal
