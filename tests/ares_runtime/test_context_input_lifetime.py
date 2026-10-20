"""Native execution fences survive projection, process death, and ACK loss."""
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_input import db, accept, row, project  # noqa: F401
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def begin(db, receipt):
    return db.begin_context_input_turn("s", receipt=receipt, turn_lease_holder="holder")


def admit(db, attempt="a"):
    return db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id=attempt,
        expected_snapshot_digest=db.read_context_rebase_snapshot("s").digest,
        payload_digest="sha256:" + "a" * 64, route_ref="test")


def test_projection_is_not_execution_or_completion(db):
    receipt = accept(db)
    phase = begin(db, receipt)
    project(db, receipt)
    assert db.read_pending_context_inputs("s") == ()
    assert db.read_context_input_work("s")["receipts"] == (receipt,)
    db.release_context_input_turn("s", turn_lease_holder="holder")
    recovered = begin(db, receipt)
    assert recovered["phase_id"] == phase["phase_id"]
    assert recovered["deadline_at"] == phase["deadline_at"]
    assert recovered["attempts"] == 2


@pytest.mark.parametrize("settled", [False, True])
def test_any_admitted_request_without_final_outcome_parks(db, settled):
    receipt = accept(db)
    begin(db, receipt)
    project(db, receipt)
    admit(db)
    if settled:
        db.settle_context_dispatch_response("a", turn_lease_holder="holder")
    db.release_context_input_turn("s", turn_lease_holder="holder")
    assert db.read_context_input_work("s")["phase"]["state"] == "uncertain"
    with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
        begin(db, receipt)
    with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
        db.reserve_context_input_wake("s", receipt=receipt)


def test_retry_budget_cannot_be_reset_by_new_input_or_lease(db):
    receipt = accept(db)
    original = begin(db, receipt)
    for event_id in ("e2", "e3"):
        db.release_context_input_turn("s", turn_lease_holder="holder")
        db.release_session_turn_lease("s", "holder")
        assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
        receipt = accept(db, event=event_id)
        phase = begin(db, receipt)
        assert phase["phase_id"] == original["phase_id"]
        assert phase["deadline_at"] == original["deadline_at"]
        assert phase["first_sequence"] == 1
    db.release_context_input_turn("s", turn_lease_holder="holder")
    with pytest.raises(ContextContinuationError, match="RECOVERY_EXHAUSTED"):
        begin(db, accept(db, event="e4"))


def test_wake_construction_budget_survives_new_arrivals(db):
    receipt = accept(db)
    first = db.reserve_context_input_wake("s", receipt=receipt)
    for event_id in ("e2", "e3"):
        current = db.reserve_context_input_wake("s", receipt=accept(db, event=event_id))
        assert current["started_at"] == first["started_at"]
        assert current["through_sequence"] == first["through_sequence"]
    with pytest.raises(ContextContinuationError, match="WAKE_EXHAUSTED"):
        db.reserve_context_input_wake("s", receipt=receipt)


def test_recovery_deadline_is_fixed_but_does_not_expire_live_productive_work(db):
    receipt = accept(db)
    phase = begin(db, receipt)
    db.release_context_input_turn("s", turn_lease_holder="holder")
    with patch("hermes_state_input_turns.time", SimpleNamespace(time=lambda: phase["deadline_at"])):
        with pytest.raises(ContextContinuationError, match="RECOVERY_EXHAUSTED"):
            begin(db, receipt)
    begin(db, receipt)
    project(db, receipt)
    admit(db)
    row_id = finish(db)
    with patch("hermes_state_input_turns.time", SimpleNamespace(time=lambda: phase["deadline_at"] + 1)):
        result = db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=row_id,
            final_content="Answered", final_attempt_id="a")
        assert result["state"] == "answered"


def test_live_controller_is_not_dead_when_lease_expires(db):
    receipt = accept(db)
    begin(db, receipt)
    db.release_session_turn_lease("s", "holder")
    assert db.try_acquire_session_turn_lease("s", "other", ttl_seconds=300)
    with pytest.raises(ContextContinuationError, match="CONTROLLER_STILL_ACTIVE"):
        db.begin_context_input_turn("s", receipt=receipt, turn_lease_holder="other")


def test_actual_dead_process_projected_input_recovers_without_duplicate(db):
    db.release_session_turn_lease("s", "holder")
    script = '''
import os,sys
from pathlib import Path
from hermes_state import SessionDB
d=SessionDB(Path(sys.argv[1]))
h=f"pid={os.getpid()}:turn=cold"
assert d.try_acquire_session_turn_lease("s", h, ttl_seconds=300)
r=d.accept_context_input("s",source="cli",event_id="e1",content="Correction")
d.begin_context_input_turn("s",receipt=r,turn_lease_holder=h)
d.append_messages_batch("s",[{"role":"user","content":r.content,"timestamp":r.timestamp,
"_context_input":{"conversation_root":r.conversation_root,"sequence":r.sequence,"payload_digest":r.payload_digest}}],turn_lease_holder=h)
d.close()
'''
    result = subprocess.run([sys.executable, "-c", script, str(db.db_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    receipt = accept(db)
    assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
    phase = begin(db, receipt)
    assert phase["attempts"] == 2 and phase["dispatch_attempts"] == []
    assert db.project_context_inputs_before("s", receipt=receipt, turn_lease_holder="holder")["projection"] is not None
    assert [r["content"] for r in db.get_messages("s")].count("Correction") == 1


def finish(db):
    db.settle_context_dispatch_response("a", turn_lease_holder="holder")
    messages = [{"role": "assistant", "content": "Answered"}]
    db.append_messages_batch("s", messages, turn_lease_holder="holder")
    return messages[0]["_row_id"]


def test_final_response_receipt_reconciles_lost_ack_and_never_reexecutes(db):
    receipt = accept(db)
    begin(db, receipt)
    project(db, receipt)
    admit(db)
    row_id = finish(db)
    actual = db._execute_write
    def lost_ack(fn):
        actual(fn)
        raise OSError("lost acknowledgement")
    with patch.object(db, "_execute_write", side_effect=lost_ack):
        with pytest.raises(OSError):
            db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=row_id, final_content="Answered", final_attempt_id="a")
    result = db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=row_id, final_content="Answered", final_attempt_id="a")
    assert result["state"] == "answered"
    assert db.read_context_input_work("s")["receipts"] == ()
    with pytest.raises(ContextContinuationError, match="ALREADY_PROJECTED"):
        begin(db, receipt)


@pytest.mark.parametrize("mutation", ["late_input", "stop", "tool_tail", "wrong_content"])
def test_final_response_cannot_complete_superseded_or_tool_work(db, mutation):
    receipt = accept(db)
    begin(db, receipt)
    project(db, receipt)
    admit(db)
    row_id = finish(db)
    if mutation == "late_input":
        accept(db, event="e2")
    elif mutation == "stop":
        db.record_context_stop("s")
    elif mutation == "tool_tail":
        db._execute_write(lambda c: c.execute("UPDATE messages SET tool_calls=? WHERE id=?", (json.dumps([{"id": "call", "type": "function"}]), row_id)))
    with pytest.raises(ContextContinuationError):
        db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=row_id,
            final_content="Changed" if mutation == "wrong_content" else "Answered", final_attempt_id="a")
    assert db.read_context_input_work("s")["phase"]["state"] == "active"


def test_real_turn_closes_native_response_phase_and_next_turn_can_run(durable_agent):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        for event_id in ("one", "two"):
            result = agent.run_conversation("Request " + event_id, persist_user_event_id=event_id,
                conversation_history=db.get_messages_as_conversation(agent.session_id, include_row_ids=True))
            assert result["completed"] and not result["failed"], result
            phase = db.read_context_input_work(agent.session_id)["phase"]
            assert phase["state"] == "answered" and phase["final_row"]["row_id"] > 0
    assert agent.client.chat.completions.create.call_count == 2


def test_pre_request_assistant_note_cannot_complete_response(db):
    receipt = accept(db)
    begin(db, receipt)
    project(db, receipt)
    messages = [{"role": "assistant", "content": "Earlier nonfinal note"}]
    db.append_messages_batch("s", messages, turn_lease_holder="holder")
    admit(db)
    db.settle_context_dispatch_response("a", turn_lease_holder="holder")
    with pytest.raises(ContextContinuationError, match="FINAL_ROW_INVALID"):
        db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=messages[0]["_row_id"],
            final_content=messages[0]["content"], final_attempt_id="a")


def test_answered_phase_does_not_block_separately_owned_synthetic_dispatch(db):
    receipt = accept(db)
    begin(db, receipt)
    project(db, receipt)
    admit(db)
    row_id = finish(db)
    db.complete_context_input_turn("s", turn_lease_holder="holder", final_row_id=row_id,
        final_content="Answered", final_attempt_id="a")
    db.append_message("s", "user", "Native loop wake", display_kind="auto_continue", turn_lease_holder="holder")
    assert admit(db, "synthetic")["attempt_id"] == "synthetic"
    assert db.read_context_input_work("s")["phase"]["dispatch_attempts"] == ["a"]


@pytest.mark.parametrize("lose_ack", [False, True])
def test_response_and_final_row_publish_atomically_and_ack_loss_reads_back(durable_agent, lose_ack):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    actual = db.append_messages_batch
    commits = []
    def observe(*args, **kwargs):
        result = actual(*args, **kwargs)
        phase = db.read_context_input_work(agent.session_id)["phase"]
        if phase and phase["state"] == "answered":
            commits.append(phase)
            if lose_ack:
                raise OSError("lost final row acknowledgement")
        return result
    with patch.object(db, "append_messages_batch", side_effect=observe), patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("Request", persist_user_event_id="one")
    assert result["completed"] and not result["failed"], result
    assert len(commits) == 1
    assert [r["content"] for r in db.get_messages(agent.session_id)] == ["Request", "Done"]
    assert agent.client.chat.completions.create.call_count == 1
