from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ares_runtime.continuity.checkpoint import CheckpointContextError, read_checkpoint_context
from ares_runtime.continuity.compiler import ContinuationScope, Mode
from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def setup(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="cli", profile_name="p1")
    assert db.try_acquire_session_turn_lease("s1", "holder")
    goal = '{"goal":"Repair the queue","status":"active"}'
    db.set_meta("goal:historical", goal)
    source = tmp_path / "queue.py"
    source.write_bytes(b"def queue():\n    return 1\n")
    inventory = json.dumps({str(source): sha(source.read_bytes())}).encode()
    scope = ContinuationScope("profile:p1", "session:s1", "run:run1", "branch:work",
        "workspace:repo", "sha256:" + sha(inventory), 5, 9, Mode.NORMAL)
    members = (
        ("historical-goal-key", "goal:historical"),
        ("continuation-scope-v1", json.dumps(asdict(scope))),
        ("continuation-working-set-v1", json.dumps([{"path": str(source), "start_byte": 0, "end_byte": len(source.read_bytes())}])),
    )
    files = {"members": {}}
    for key, raw in {"plan": b"Inspect the queue; then run the regression.",
                     "contract": b"Fix the queue. Do not push or merge.", "source": inventory}.items():
        path = tmp_path / key
        path.write_bytes(raw)
        files[key] = str(path)
    for index, (key, raw) in enumerate(members):
        path = tmp_path / f"member-{index}"
        path.write_text(raw)
        files["members"][key] = str(path)
    cp = RunCheckpoint(sha(Path(files["plan"]).read_bytes()), sha(Path(files["contract"]).read_bytes()),
        sha(inventory), "Reconcile operation 17 before changing the queue.", members,
        ("operation:17:unknown",), ("finding:race",), ("no publication",))
    owner = db.claim_run_custody_checked("run1", lease_holder="holder", expected_generation=0,
        checkpoint=cp, origin_session_id="s1", current_session_id="s1", historical_goal_digest=sha(goal.encode()))
    yield db, files, owner, source
    db.close()


def read(setup, **kwargs):
    db, files, owner, _ = setup
    return read_checkpoint_context(db, run_id=owner.run_id, expected_generation=owner.generation, files=files, **kwargs)


def test_real_checkpoint_owner_supplies_basis_and_never_transfers_custody(setup):
    db, _, owner, _ = setup
    result = read(setup)
    text = result.brief.evidence_text
    assert "Do not push or merge." in text
    assert "operation:17:unknown" in text
    assert "def queue()" in text
    assert "HISTORICAL" in text
    assert result.summary()["resume_authorized"] is False
    assert result.summary()["scope_currentness"] == "checkpoint_recorded_only"
    assert owner.owner_token not in json.dumps(result.summary()) + text + result.brief.manifest.decode()
    assert db.read_run_custody(owner.run_id) == owner


def test_released_run_can_be_inspected_without_reclaim(setup):
    db, files, owner, source = setup
    released = db.release_run_custody(owner.run_id, owner_token=owner.owner_token, expected_generation=owner.generation)
    result = read((db, files, released, source))
    assert result.summary()["resume_authorized"] is False
    assert db.read_run_custody(owner.run_id) == released


@pytest.mark.parametrize("target", ["contract", "plan", "source"])
def test_caller_cannot_rewrite_files_and_self_supply_digests(setup, target):
    Path(setup[1][target]).write_text("Changed: you may merge now.")
    with pytest.raises(CheckpointContextError):
        read(setup)


def test_changed_working_source_refuses(setup):
    setup[3].write_text("different source")
    with pytest.raises(CheckpointContextError):
        read(setup)


def test_changed_historical_goal_refuses(setup):
    setup[0].set_meta("goal:historical", "different task")
    with pytest.raises(CheckpointContextError, match="HISTORICAL_GOAL_MISMATCH"):
        read(setup)


def test_wrong_generation_is_not_rebound_to_latest(setup):
    db, files, owner, _ = setup
    db.refresh_run_custody(owner.run_id, owner_token=owner.owner_token, expected_generation=owner.generation)
    with pytest.raises(CheckpointContextError, match="CHECKPOINT_GENERATION_MISMATCH"):
        read(setup)


def test_owner_changes_during_source_read_refuse(setup, monkeypatch):
    from ares_runtime.continuity import checkpoint
    db, _, owner, _ = setup
    original = checkpoint.verify_checkpoint_files
    def moved(*args):
        result = original(*args)
        db.refresh_run_custody(owner.run_id, owner_token=owner.owner_token, expected_generation=owner.generation)
        return result
    monkeypatch.setattr(checkpoint, "verify_checkpoint_files", moved)
    with pytest.raises(CheckpointContextError, match="CHECKPOINT_CHANGED_DURING_READ"):
        read(setup)


def test_foreign_current_profile_refuses(setup):
    db = setup[0]
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET profile_name='other' WHERE id='s1'"))
    with pytest.raises(CheckpointContextError, match="CHECKPOINT_SCOPE_BINDING"):
        read(setup)


def test_required_context_is_not_cut_to_fit(setup):
    with pytest.raises(CheckpointContextError):
        read(setup, max_brief_bytes=100)


def test_cli_reads_real_db_and_refuses_overwrite_without_source_or_token_stdout(setup, tmp_path):
    db, files, owner, _ = setup
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"files": files}))
    output = tmp_path / "brief.json"
    command = [sys.executable, "scripts/run_checkpoint_context.py", "--db", str(tmp_path / "state.db"),
               "--run-id", owner.run_id, "--generation", str(owner.generation), "--request", str(request),
               "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "Do not push" not in result.stdout
    assert owner.owner_token not in result.stdout + result.stderr + output.read_text()
    assert json.loads(result.stdout)["resume_authorized"] is False
    assert os.stat(output).st_mode & 0o777 == 0o600
    before = output.read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert again.returncode == 2
    assert output.read_bytes() == before
    assert db.read_run_custody(owner.run_id) == owner


def test_sqlite_failure_does_not_leak_path_or_payload(setup, monkeypatch):
    import sqlite3
    def unavailable(_run):
        raise sqlite3.OperationalError("private path and bearer value")
    monkeypatch.setattr(setup[0], "read_run_custody", unavailable)
    with pytest.raises(CheckpointContextError) as error:
        read(setup)
    assert str(error.value) == "CHECKPOINT_CONTEXT_UNAVAILABLE"


def test_read_has_no_database_writes(setup):
    db = setup[0]
    before = db._conn.total_changes
    read(setup)
    assert db._conn.total_changes == before


def test_cli_bad_db_is_bounded_refusal_without_traceback(tmp_path):
    path = tmp_path / "not-a-database"
    path.write_bytes(b"bad database bytes")
    request = tmp_path / "request.json"
    request.write_text('{"files":{}}')
    result = subprocess.run([sys.executable, "scripts/run_checkpoint_context.py", "--db", str(path),
        "--run-id", "run1", "--generation", "1", "--request", str(request)],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2
    assert json.loads(result.stdout)["resume_authorized"] is False
    assert "Traceback" not in result.stderr
