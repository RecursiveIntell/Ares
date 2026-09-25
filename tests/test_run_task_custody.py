"""Ordinary-task custody uses real native input, never a fabricated goal."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.run_checkpoint_custody import TurnRunCustody
from ares_runtime.continuity.checkpoint import read_checkpoint_context
from ares_runtime.continuity.compiler import ContinuationScope, Mode
from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustody, RunCustodyV2, RunCustodyError, generation_key
from scripts.run_checkpoint_claim import ClaimOutcomeUnknown, ClaimRefusal
from scripts.run_checkpoint_resume import inspect_resume


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def task(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="cli", profile_name="p")
    row = db.append_message("s", "user", "Repair the queue; retain my changes.", timestamp=1.0)
    assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=120)
    source = tmp_path / "queue.py"
    source.write_text("pending = []\n")
    inventory = json.dumps({str(source): sha(source.read_bytes())}).encode()
    scope = ContinuationScope("profile:p", "session:s", "run:task", "branch:work",
        "workspace:repo", "sha256:" + sha(inventory), 1, 1, Mode.NORMAL)
    members = (("continuation-scope-v1", json.dumps(asdict(scope))), ("original-wip", "Retain my changes"))
    files = {"members": {}}
    for name, raw in {"plan": b"Inspect queue", "contract": b"Retain user changes", "source": inventory}.items():
        path = tmp_path / name
        path.write_bytes(raw)
        files[name] = str(path)
    for index, (name, raw) in enumerate(members):
        path = tmp_path / f"member-{index}"
        path.write_text(raw)
        files["members"][name] = str(path)
    cp = RunCheckpoint(sha(b"Inspect queue"), sha(b"Retain user changes"), sha(inventory),
        "Inspect the queue", members, ("effect:unknown",), ("finding:race",), ("no merge",))
    binding, control = db.read_run_task_basis(run_id="task", origin_session_id="s",
        current_session_id="s", input_row_id=row)
    kwargs = dict(lease_holder="holder", expected_generation=0, checkpoint=cp,
        task_binding=binding, current_session_id="s", expected_control_digest=control)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"checkpoint": asdict(cp), "files": files,
                                  "session_id": "s", "lease_holder": "holder"}))
    yield db, kwargs, request, source
    db.close()


def claim(task, **overrides):
    return task[0].claim_run_task_custody_checked("task", **{**task[1], **overrides})


def client_args(task, generation=0):
    _, kwargs, request, _ = task
    return dict(run_id="task", expected_generation=generation, session_id="s",
        request_path=str(request), expected_request_digest=sha(request.read_bytes()),
        origin_session_id="s", task_binding=asdict(kwargs["task_binding"]),
        expected_control_digest=kwargs["expected_control_digest"], ttl_seconds=120)


def test_claim_refresh_reopen_and_release_preserve_strict_v2_without_goal(task):
    db, kwargs, _, _ = task
    first = claim(task)
    assert type(first) is RunCustodyV2
    assert db.list_meta_prefix("goal:") == []
    history = [first]
    for _ in range(3):
        old = history[-1]
        history.append(db.refresh_run_custody("task", owner_token=old.owner_token,
            expected_generation=old.generation))
    assert json.loads(db.get_meta(generation_key("task", 4)))["schema"] == "SessionDBRunCustodyRefreshV2"
    with SessionDB(db_path=db.db_path, read_only=True) as reopened:
        assert reopened.read_run_custody("task") == history[-1]
        for value in history:
            assert reopened.read_run_checkpoint("task", generation=value.generation) == value
        assert reopened.validate_run_task_binding(first) == kwargs["task_binding"]
    old = history[-1]
    released = db.release_run_custody("task", owner_token=old.owner_token, expected_generation=old.generation)
    assert released.checkpoint == first.checkpoint
    replacement = claim(task, expected_generation=released.generation)
    assert replacement.owner_token != first.owner_token
    assert replacement.checkpoint == first.checkpoint


@pytest.mark.parametrize("fault,code", [
    ("root", "TASK_BINDING_MISMATCH"), ("profile", "TASK_BINDING_MISMATCH"),
    ("digest", "TASK_BINDING_MISMATCH"), ("inactive", "TASK_INPUT_MISSING"),
    ("edited", "TASK_BINDING_MISMATCH"), ("synthetic", "TASK_AUTHENTIC_INPUT_REQUIRED"),
    ("summary", "TASK_AUTHENTIC_INPUT_REQUIRED"), ("new-input", "TASK_CONTROL_CHANGED"),
    ("lease", "LEASE_MISMATCH"), ("wrong-session", "TASK_LINEAGE_MISMATCH"),
])
def test_invalid_native_task_binding_never_creates_generation(task, fault, code):
    db, kwargs, _, _ = task
    binding = kwargs["task_binding"]
    if fault in {"root", "profile", "digest"}:
        field = {"root": "conversation_root", "profile": "profile_name", "digest": "input_digest"}[fault]
        kwargs["task_binding"] = replace(binding, **{field: "0" * 64 if fault == "digest" else "other"})
    elif fault in {"inactive", "edited", "synthetic", "summary"}:
        column, value = {"inactive": ("active", 0), "edited": ("content", "different task"),
                         "synthetic": ("display_kind", "hidden"), "summary": ("_compressed_summary", 1)}[fault]
        db._execute_write(lambda conn: conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (value, binding.input_row_id)))
    elif fault == "new-input":
        db.append_message("s", "user", "Actually stop before editing.", turn_lease_holder="holder")
    elif fault == "lease":
        db.release_session_turn_lease("s", "holder")
    else:
        db.create_session("other", source="cli", profile_name="p")
        db.append_message("other", "user", "Unrelated task")
        assert db.try_acquire_session_turn_lease("other", "holder")
        kwargs["current_session_id"] = "other"
    with pytest.raises(RunCustodyError, match=code):
        claim(task)
    assert db.read_run_custody("task") is None


def test_control_rechecked_after_waiting_for_native_write_admission(task, monkeypatch):
    db = task[0]
    execute = db._execute_write
    def changed(fn, *args, **kwargs):
        # Independent connection commits before candidate obtains writer admission.
        with SessionDB(db_path=db.db_path) as other:
            other.append_message("s", "user", "Wait for review", turn_lease_holder="holder")
        return execute(fn, *args, **kwargs)
    monkeypatch.setattr(db, "_execute_write", changed)
    with pytest.raises(RunCustodyError, match="TASK_CONTROL_CHANGED"):
        claim(task)
    assert db.read_run_custody("task") is None


def test_v1_decoder_and_v2_binding_cannot_be_mixed(task):
    db = task[0]
    value = claim(task)
    wire = json.loads(json.dumps(asdict(value)))
    with pytest.raises(RunCustodyError, match="INTEGRITY_SCHEMA"):
        RunCustody.from_dict(wire)
    with pytest.raises(RunCustodyError, match="MIXED_TASK_BINDING"):
        replace(value, checkpoint=replace(value.checkpoint,
            members=value.checkpoint.members + (("historical-goal-key", "goal:fake"),)))
    compact = db.refresh_run_custody("task", owner_token=value.owner_token, expected_generation=1)
    record = json.loads(db.get_meta(generation_key("task", compact.generation)))
    record["schema"] = "SessionDBRunCustodyRefreshV1"
    raw = json.dumps(record)
    db.set_meta(generation_key("task", compact.generation), raw)
    db.set_meta("run-custody:task:head", json.dumps({"generation": compact.generation, "digest": sha(raw.encode())}))
    with pytest.raises(RunCustodyError, match="INTEGRITY_REFRESH_SCHEMA"):
        db.read_run_custody("task")


def test_v2_claim_file_client_and_cold_inspection_share_native_checks(task):
    db, _, request, _ = task
    handle = TurnRunCustody(db)
    handle.begin_turn("holder")
    response = handle.claim("holder", **client_args(task))
    value = db.read_run_custody("task")
    assert response["generation"] == 1
    assert value.owner_token not in json.dumps(response)
    assert inspect_resume(db.db_path, "task", 1, request)["resume_authorized"] is False
    files = json.loads(request.read_text())["files"]
    before = db.list_meta_prefix("run-custody:")
    with SessionDB(db_path=db.db_path, read_only=True) as reopened:
        context = read_checkpoint_context(reopened, run_id="task", expected_generation=1, files=files)
    assert "effect:unknown" in context.brief.evidence_text
    assert context.summary()["resume_authorized"] is False
    assert db.list_meta_prefix("run-custody:") == before
    assert handle.finish_turn("holder") == []


def test_lost_v2_claim_ack_retains_unknown_and_never_reclaims(task, monkeypatch):
    db = task[0]
    original = db.claim_run_task_custody_checked
    calls = []
    def lost(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise OSError("lost acknowledgment")
    monkeypatch.setattr(db, "claim_run_task_custody_checked", lost)
    handle = TurnRunCustody(db)
    handle.begin_turn("holder")
    with pytest.raises(ClaimOutcomeUnknown):
        handle.claim("holder", **client_args(task))
    with pytest.raises(ClaimOutcomeUnknown):
        handle.claim("holder", **client_args(task))
    assert handle.finish_turn("holder")[0]["status"] == "unknown"
    assert calls == [1]
    assert db.read_run_custody("task").generation == 1


def test_real_dead_process_takeover_preserves_exact_checkpoint_and_fences_old_token(task):
    db, _, request, _ = task
    payload = client_args(task)
    payload.pop("session_id")
    code = '''
import json, os, sys
from pathlib import Path
from hermes_state import SessionDB
from scripts.run_checkpoint_claim import claim_from_files
db = SessionDB(db_path=Path(sys.argv[1]))
args = json.loads(sys.argv[2])
claim_from_files(db, **args, controller_pid=os.getpid())
print("claimed", flush=True)
sys.stdin.readline()
db.close()
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", code, str(db.db_path), json.dumps(payload)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        first_line = pool.submit(child.stdout.readline).result(timeout=15).strip()
        if first_line != "claimed":
            _, stderr = child.communicate(timeout=15)
            pytest.fail(f"Child claim failed: {stderr}")
        old = db.read_run_custody("task")
        with pytest.raises(RunCustodyError, match="OWNER_ACTIVE"):
            claim(task, expected_generation=1)
        out, err = child.communicate("exit\n", timeout=15)
        assert child.returncode == 0, err
        with pytest.raises(RunCustodyError, match="TAKEOVER_REQUIRES_EXACT_CHECKPOINT"):
            claim(task, expected_generation=1, checkpoint=replace(old.checkpoint, unresolved_effects=()))
        handle = TurnRunCustody(db)
        handle.begin_turn("holder")
        assert handle.claim("holder", **client_args(task, generation=1))["generation"] == 2
        new = db.read_run_custody("task")
        assert new.controller_pid == os.getpid()
        assert new.owner_token != old.owner_token
        assert new.checkpoint == old.checkpoint
        assert new.task_binding == old.task_binding
        with pytest.raises(RunCustodyError, match="FENCE_MISMATCH"):
            db.release_run_custody("task", owner_token=old.owner_token, expected_generation=1)
        assert inspect_resume(db.db_path, "task", 2, request)["effects_executed"] is False
        assert handle.finish_turn("holder") == []
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)
        pool.shutdown(wait=True, cancel_futures=True)


def test_v2_session_transfer_retains_task_identity(task):
    db, kwargs, _, _ = task
    first = claim(task)
    db.end_session("s", "compression")
    db.create_session("child", source="cli", profile_name="p", parent_session_id="s")
    moved = db.transfer_run_session("task", owner_token=first.owner_token,
        expected_generation=1, expected_session_id="s", new_session_id="child")
    assert moved.task_binding == kwargs["task_binding"]
    assert db.validate_run_task_binding(moved) == first.task_binding
    assert moved.checkpoint == first.checkpoint


@pytest.mark.parametrize("fork", [False, True])
def test_v2_transfer_refuses_cross_profile_or_fork_before_mutation(task, fork):
    db = task[0]
    first = claim(task)
    db.end_session("s", "compression")
    db.create_session("child", source="cli", profile_name="p" if fork else "other",
        parent_session_id="s", model_config={"_branched_from": "s"} if fork else None)
    with pytest.raises(RunCustodyError, match="TASK_LINEAGE_MISMATCH" if fork else "TASK_PROFILE_MISMATCH"):
        db.transfer_run_session("task", owner_token=first.owner_token,
            expected_generation=1, expected_session_id="s", new_session_id="child")
    assert db.read_run_custody("task") == first
    assert db.get_meta(generation_key("task", 2)) is None


def test_cross_profile_ancestor_cannot_be_relabelled_as_current_task(task):
    db, kwargs, _, _ = task
    db.end_session("s", "compression")
    db.create_session("child", source="cli", profile_name="other", parent_session_id="s")
    db.append_message("child", "user", "Public task", turn_lease_holder="holder")
    with pytest.raises(RunCustodyError, match="TASK_PROFILE_MISMATCH"):
        db.read_run_task_basis(run_id="task", origin_session_id="child",
            current_session_id="child", input_row_id=kwargs["task_binding"].input_row_id)


def test_tool_child_cannot_inherit_parent_task_or_control(task):
    db, kwargs, _, _ = task
    db.end_session("s", "compression")
    db.create_session("child", source="tool", profile_name="p", parent_session_id="s")
    child_row = db.append_message("child", "user", "Separate child task")
    assert db.try_acquire_session_turn_lease("child", "child-holder")
    snapshot = db.read_context_rebase_snapshot("child")
    assert snapshot.conversation_root == "child"
    assert [item["row_id"] for item in snapshot.authentic_users] == [child_row]
    with pytest.raises(RunCustodyError, match="TASK_LINEAGE_MISMATCH"):
        db.read_run_task_basis(run_id="task", origin_session_id="s",
            current_session_id="child", input_row_id=kwargs["task_binding"].input_row_id)
