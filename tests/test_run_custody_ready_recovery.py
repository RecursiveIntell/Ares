"""Controller adoption is distinct from durable READY publication."""
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import reconcile_context_rebase
from hermes_state_continuity import ContextContinuationError
from tests.test_run_task_custody import task  # noqa: F401
from tests.test_run_custody_cold_recovery import publisher, exited, fresh_agent
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


READY_PROCESS = '''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.run_checkpoint_custody import TurnRunCustody
from ares_runtime.continuity.runtime import reconcile_context_rebase
db=SessionDB(Path(sys.argv[1]))
owner=TurnRunCustody(db);owner.begin_turn('holder')
agent=SimpleNamespace(_session_db=db,session_id='s',_active_session_turn_lease_holder='holder',
 _run_checkpoint_custody=owner,_transition_context_engine_session=lambda **_:None,context_compressor=SimpleNamespace())
result=reconcile_context_rebase(agent)
assert result.ready,result.reason
db.append_message('child','assistant','Durable assistant tail',turn_lease_holder='holder')
print('ready',flush=True)
sys.stdin.readline()
db.close()
'''


def ready_process(task):
    proc = publisher(task)
    exited(proc)
    process = subprocess.Popen([sys.executable, "-u", "-c", READY_PROCESS, str(task[0].db_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            line = pool.submit(process.stdout.readline).result(timeout=15)
            assert line.strip() == "ready", process.communicate(timeout=15)[1]
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
            raise
    return process


def test_ready_cold_process_reconstructs_custody_and_assistant_tail_without_republication(task):
    proc = ready_process(task)
    exited(proc)
    db = task[0]
    before = db.read_run_custody("task")
    publication = db.get_meta("context-rebase-recovery:cold-tx")
    episode = db.read_context_rebase_episode("child")
    agent = fresh_agent(task)
    result = reconcile_context_rebase(agent)
    assert result.ready, result.reason
    after = db.read_run_custody("task")
    assert after.generation == before.generation + 1 and after.owner_token != before.owner_token
    assert after.task_binding == before.task_binding and after.checkpoint == before.checkpoint
    assert result.messages[-1]["role"] == "assistant"
    assert agent.session_id == "child"
    assert db.get_meta("context-rebase-recovery:cold-tx") == publication
    assert db.read_context_rebase_episode("child") == episode
    assert agent._run_checkpoint_custody.finish_turn("holder") == []


def test_lost_ready_ack_completes_adoption_without_repeating_publication(task, monkeypatch):
    proc = publisher(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    original = db.mark_context_rebase_ready
    calls = []
    def lost(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise RuntimeError("lost READY ack")
    monkeypatch.setattr(db, "mark_context_rebase_ready", lost)
    result = reconcile_context_rebase(agent)
    assert result.ready, result.reason
    assert calls == [1]
    assert agent.session_id == "child"
    assert db.read_context_rebase_episode("child").attempts_without_recovery == 1
    assert agent._run_checkpoint_custody.finish_turn("holder") == []


def test_failed_local_adoption_retains_ready_custody_through_cleanup_and_next_turn(task, monkeypatch):
    proc = publisher(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    def fail(*args):
        raise RuntimeError("local adoption failed")
    with monkeypatch.context() as m:
        m.setattr("ares_runtime.continuity.runtime._publish_runtime_session_context", fail)
        result = reconcile_context_rebase(agent)
    assert not result.ready
    assert db.read_context_rebase_transition("cold-tx").state == "ready"
    errors = agent._run_checkpoint_custody.finish_turn("holder")
    assert errors[0]["code"] == "RUN_CUSTODY_RECOVERY_PENDING"
    assert db.read_run_custody("task").disposition == "active"
    db.release_session_turn_lease("s", "holder")
    assert db.try_acquire_session_turn_lease("child", "next", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "next"
    agent._run_checkpoint_custody.begin_turn("next")
    recovered = reconcile_context_rebase(agent)
    assert recovered.ready, recovered.reason
    assert agent._run_checkpoint_custody.finish_turn("next") == []


def test_ready_live_process_cannot_be_reclaimed(task):
    proc = ready_process(task)
    try:
        db = task[0]
        before = db.read_run_custody("task")
        result = reconcile_context_rebase(fresh_agent(task))
        assert not result.ready
        assert db.read_run_custody("task") == before
    finally:
        exited(proc)


def test_ready_ack_readback_rejects_late_control_and_retains_native_custody(task, monkeypatch):
    proc = publisher(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    original = db.mark_context_rebase_ready
    def late(*args, **kwargs):
        original(*args, **kwargs)
        db.record_context_stop("child")
        raise RuntimeError("lost ack plus stop")
    monkeypatch.setattr(db, "mark_context_rebase_ready", late)
    result = reconcile_context_rebase(agent)
    assert not result.ready
    assert agent._run_checkpoint_custody.finish_turn("holder")[0]["code"] == "RUN_CUSTODY_RECOVERY_PENDING"
    assert db.read_run_custody("task").disposition == "active"


def test_controller_completion_ack_is_read_back_without_second_mutation(task, monkeypatch):
    proc = ready_process(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    original = db.complete_context_controller_recovery
    calls = []
    def lost(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise RuntimeError("lost controller ack")
    monkeypatch.setattr(db, "complete_context_controller_recovery", lost)
    result = reconcile_context_rebase(agent)
    assert result.ready, result.reason
    assert calls == [1]


def test_controller_recovery_budget_is_fixed_across_lease_change(task, monkeypatch):
    proc = ready_process(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    def fail(**kwargs):
        raise RuntimeError("engine unavailable")
    agent._transition_context_engine_session = fail
    for n in range(3):
        result = reconcile_context_rebase(agent)
        assert not result.ready
        record = json.loads(db.get_meta(db._context_controller_recovery_key("cold-tx")))
        assert record["attempts"] == n + 1
        if n == 0:
            started = record["started_at"]
        assert record["started_at"] == started
        holder = agent._active_session_turn_lease_holder
        assert agent._run_checkpoint_custody.finish_turn(holder)[0]["code"] == "RUN_CUSTODY_RECOVERY_PENDING"
        db.release_session_turn_lease("child", holder)
        next_holder = f"retry-{n}"
        assert db.try_acquire_session_turn_lease("child", next_holder, ttl_seconds=300)
        agent._active_session_turn_lease_holder = next_holder
        agent._run_checkpoint_custody.begin_turn(next_holder)
    result = reconcile_context_rebase(agent)
    assert result.reason == "CONTEXT_CONTROLLER_RECOVERY_EXHAUSTED"
    assert db.read_context_rebase_episode("child").attempts_without_recovery == 1


def test_ready_idempotent_mark_requires_original_owner_confirmation(task):
    proc = ready_process(task)
    exited(proc)
    db = task[0]
    transition = db.read_context_rebase_transition("cold-tx")
    with pytest.raises(ContextContinuationError, match="TURN_LEASE_MISMATCH"):
        db.mark_context_rebase_ready("cold-tx", expected_continuation_digest=transition.continuation_digest,
                                    expected_child_session_id="child")


@pytest.mark.parametrize("accepted_before_stop", [False, True])
def test_actual_ready_turn_resumes_only_with_one_authentic_post_stop_input(agent, tmp_path, accepted_before_stop):
    from hermes_state import SessionDB
    from ares_runtime.continuity.runtime import AutomaticRebaseError
    from tests.ares_runtime.test_context_rebase_state import _publish, _ready
    with SessionDB(tmp_path / "ready-turn.db") as db:
        db.create_session("s0", source="cli", profile_name="p1")
        db.append_message("s0", "user", "Original requirement")
        assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
        transition = _publish(db)
        _ready(db, transition.transition_id, expected_continuation_digest=transition.continuation_digest,
               expected_child_session_id="s1")
        db.append_message("s1", "assistant", "Previous response", turn_lease_holder="holder")
        db.release_session_turn_lease("s0", "holder")
        agent._session_db, agent.session_id, agent._session_db_created = db, "s1", True
        agent._cached_system_prompt = "trusted base prompt"
        agent.context_rebase_enabled, agent.compression_enabled = True, False
        agent.max_iterations, agent.max_tokens = 1, 1000
        source = agent.platform or "agent"
        if accepted_before_stop:
            db.accept_context_input("s1", source=source, event_id="resume", content="Continue with my correction")
        db.record_context_stop("s1")
        agent.client.chat.completions.create.return_value = _mock_response(content="Resumed", finish_reason="stop")
        with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            if accepted_before_stop:
                with pytest.raises(AutomaticRebaseError, match="CONTEXT_DISPATCH_STOPPED"):
                    agent.run_conversation("Continue with my correction", persist_user_event_id="resume")
                agent.client.chat.completions.create.assert_not_called()
                assert db.read_context_rebase_snapshot("s1").dispatch_stopped
            else:
                result = agent.run_conversation("Continue with my correction", persist_user_event_id="resume")
                assert result.get("error") is None, result
                assert result["api_calls"] == 1
                assert agent.client.chat.completions.create.call_count == 1
                assert not db.read_context_rebase_snapshot("s1").dispatch_stopped
        receipt = db.read_context_input("s1", source=source, event_id="resume")
        assert receipt.sequence == 1
        assert [m["content"] for m in db.get_messages("s1")].count(receipt.content) == 1


def test_ready_custody_reconstruction_can_observe_post_stop_input_without_admitting_effects(task):
    proc = ready_process(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    db.record_context_stop("child")
    db.accept_context_input("child", source="cli", event_id="resume", content="Continue with my correction")
    result = reconcile_context_rebase(agent)
    assert result.ready, result.reason
    assert db.read_run_custody("task").controller_pid == os.getpid()
    snapshot = db.read_context_rebase_snapshot("child")
    assert snapshot.dispatch_stopped and snapshot.has_pending_inputs
    with pytest.raises(ContextContinuationError, match="CONTEXT_DISPATCH_INPUT_PENDING"):
        db.admit_context_dispatch("child", turn_lease_holder="holder", attempt_id="premature",
            expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "a" * 64, route_ref="test")


@pytest.mark.parametrize("boundary", ["file_read", "completion"])
def test_controller_deadline_is_rechecked_at_native_admission(task, monkeypatch, boundary):
    from types import SimpleNamespace
    import hermes_state_continuity
    from scripts import run_checkpoint_claim
    proc = ready_process(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    before = db.read_run_custody("task")
    clock = hermes_state_continuity.time.time
    def expire():
        record = json.loads(db.get_meta(db._context_controller_recovery_key("cold-tx")))
        # Only advance the continuity phase clock, preserving live lease and
        # custody clocks so this witnesses its own deadline fence.
        monkeypatch.setattr(hermes_state_continuity, "time", SimpleNamespace(time=lambda: record["deadline_at"] + 1))
        assert record["deadline_at"] > clock()
    if boundary == "file_read":
        verify = run_checkpoint_claim.verify_checkpoint_files
        def expired(*args, **kwargs):
            result = verify(*args, **kwargs)
            expire()
            return result
        monkeypatch.setattr(run_checkpoint_claim, "verify_checkpoint_files", expired)
    else:
        complete = db.complete_context_controller_recovery
        def expired(*args, **kwargs):
            expire()
            return complete(*args, **kwargs)
        monkeypatch.setattr(db, "complete_context_controller_recovery", expired)
    result = reconcile_context_rebase(agent)
    assert not result.ready
    record = json.loads(db.get_meta(db._context_controller_recovery_key("cold-tx")))
    assert record["completed"] is False
    assert db.get_meta(db._context_controller_recovery_key("cold-tx") + ":epoch:1:completed") is None
    if boundary == "file_read":
        assert db.read_run_custody("task") == before
    else:
        assert agent._run_checkpoint_custody.finish_turn("holder")[0]["code"] == "RUN_CUSTODY_RECOVERY_PENDING"


@pytest.mark.parametrize("change", ["control", "inventory", "head"])
def test_controller_completion_rechecks_exact_native_state(task, monkeypatch, change):
    proc = ready_process(task)
    exited(proc)
    db, agent = task[0], fresh_agent(task)
    complete = db.complete_context_controller_recovery
    def raced(*args, **kwargs):
        value = db.read_run_custody("task")
        if change == "control":
            db.accept_context_input("child", source="cli", event_id="late", content="Stop for review")
        elif change == "inventory":
            db.release_run_custody("task", owner_token=value.owner_token, expected_generation=value.generation)
        else:
            db.refresh_run_custody("task", owner_token=value.owner_token, expected_generation=value.generation)
        return complete(*args, **kwargs)
    monkeypatch.setattr(db, "complete_context_controller_recovery", raced)
    assert not reconcile_context_rebase(agent).ready
    record = json.loads(db.get_meta(db._context_controller_recovery_key("cold-tx")))
    assert record["completed"] is False
