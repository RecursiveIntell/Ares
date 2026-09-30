"""Explicit Stop can retire only a never-dispatched input phase."""
import json
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from hermes_state_input_turns import _key

from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_input import db, accept, project  # noqa: F401
from tests.ares_runtime.test_context_input_lifetime import begin, admit
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


@pytest.mark.parametrize("projected", [False, True])
def test_no_dispatch_stop_allows_distinct_post_cut_phase(db, projected):
    first = accept(db, event="A", content="first")
    original = begin(db, first)
    if projected:
        project(db, first)
    cut = db.record_context_stop("s")
    cancelled = db.read_context_input_work("s")["phase"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["schema"] == "SessionDBContextInputTurnV2"
    assert cancelled["final_row"] is None
    assert db.read_context_input_work("s")["receipts"] == ()
    proof = db.read_context_input_stop_disposition("s", phase_id=original["phase_id"])
    assert proof["before"] == original
    assert proof["after"] == cancelled
    assert proof["control"] == cut
    second = accept(db, event="C", content="second")
    db.reserve_context_input_wake("s", receipt=second)
    fresh = begin(db, second)
    assert fresh["phase_id"] != original["phase_id"]
    assert fresh["first_sequence"] == second.sequence
    db.project_context_inputs_before("s", receipt=second, turn_lease_holder="holder")
    project(db, second)
    admit(db, attempt="post-stop")
    assert db.read_context_input_stop_disposition("s", phase_id=original["phase_id"]) == proof
    with pytest.raises(ContextContinuationError, match="STOPPED"):
        begin(db, first)
    assert [m["content"] for m in db.get_messages("s")].count("first") == 1
    assert db.read_context_input("s", source="cli", event_id="A") == first


@pytest.mark.parametrize("settled", [False, True])
def test_stop_does_not_cancel_an_admitted_phase(db, settled):
    first = accept(db, event="A")
    phase = begin(db, first)
    project(db, first)
    admit(db)
    if settled:
        db.settle_context_dispatch_response("a", turn_lease_holder="holder")
    db.release_context_input_turn("s", turn_lease_holder="holder")
    db.record_context_stop("s")
    assert db.read_context_input_work("s")["phase"]["state"] == "uncertain"
    assert db.read_context_input_stop_disposition("s", phase_id=phase["phase_id"]) is None
    with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
        begin(db, accept(db, event="C"))


def test_unknown_effect_is_not_resolved_by_no_dispatch_stop(db):
    first = accept(db)
    phase = begin(db, first)
    project(db, first)
    db.append_message("s", "tool", "outcome unknown", tool_call_id="external", tool_name="terminal",
                      effect_disposition="unknown")
    before = db.read_context_rebase_snapshot("s").unresolved_effects
    assert before
    db.record_context_stop("s")
    assert db.read_context_input_work("s")["phase"]["state"] == "active"
    assert db.read_context_input_stop_disposition("s", phase_id=phase["phase_id"]) is None
    assert db.read_context_rebase_snapshot("s").unresolved_effects == before


def test_cancelled_phase_survives_reopen_without_releasing_lease(db):
    first = accept(db)
    phase = begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    proof = db.read_context_input_stop_disposition("s", phase_id=phase["phase_id"])
    db.record_context_stop("s")
    assert db.read_context_input_stop_disposition("s", phase_id=phase["phase_id"]) == proof
    reopened = SessionDB(db.db_path)
    try:
        assert reopened.read_context_input_work("s")["phase"]["state"] == "cancelled"
        assert not reopened.try_acquire_session_turn_lease("s", "other", ttl_seconds=300)
    finally:
        reopened.close()


@pytest.mark.parametrize("corruption", ["proof", "proof_time", "control", "control_time"])
def test_invalid_cancellation_evidence_cannot_admit_fresh_input(db, corruption):
    first = accept(db)
    phase = begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    second = accept(db, event="C")
    if corruption in {"proof", "proof_time"}:
        proof = db.read_context_input_stop_disposition("s", phase_id=phase["phase_id"])
        if corruption == "proof":
            proof["control"]["input_sequence"] = 0
        else:
            proof["control"]["stopped_at"] += 1
        db._execute_write(lambda conn: conn.execute("UPDATE state_meta SET value=? WHERE key=?",
            (json.dumps(proof), f'{_key(first.conversation_root)}:phase:{phase["phase_id"]}:cancelled')))
    elif corruption == "control_time":
        control = db.read_context_stop("s")["control"]
        control["stopped_at"] += 1
        db._execute_write(lambda conn: conn.execute("UPDATE state_meta SET value=? WHERE key=?",
            (json.dumps(control), "context-control:" + first.conversation_root)))
    else:
        db._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key=?",
            ("context-control:" + first.conversation_root,)))
    with pytest.raises(ContextContinuationError, match="STOP_DISPOSITION_INVALID|STOP_UNCONFIRMED"):
        begin(db, second)


def test_tampered_current_cancelled_phase_digest_is_rejected(db):
    first = accept(db)
    begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    value = db.read_context_input_work("s")["phase"]
    digest = value["stop_disposition_digest"]
    value["stop_disposition_digest"] = ("0" if digest[0] != "0" else "1") + digest[1:]
    db._execute_write(lambda conn: conn.execute("UPDATE state_meta SET value=? WHERE key=?",
        (json.dumps(value), _key(first.conversation_root))))
    with pytest.raises(ContextContinuationError, match="STOP_DISPOSITION_INVALID"):
        db.read_context_input_work("s")
    with pytest.raises(ContextContinuationError, match="STOP_DISPOSITION_INVALID"):
        begin(db, accept(db, event="C"))


def test_cancelled_phase_cannot_dispatch_without_a_new_phase(db):
    first = accept(db)
    begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    second = accept(db, event="C")
    project(db, second)
    with pytest.raises(ContextContinuationError, match="OWNER_CHANGED"):
        admit(db, attempt="old-owner")


def test_new_unknown_effect_still_blocks_post_cut_dispatch(db):
    first = accept(db)
    begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    second = accept(db, event="C")
    begin(db, second)
    project(db, second)
    db.append_message("s", "tool", "unknown", tool_call_id="late", tool_name="terminal", effect_disposition="unknown")
    with pytest.raises(ContextContinuationError, match="UNRESOLVED_EFFECTS"):
        admit(db, attempt="post-stop")


def test_stop_cancellation_and_control_are_atomic(db, monkeypatch):
    first = accept(db)
    original = begin(db, first)
    def fail(*args):
        raise OSError("phase write failed")
    monkeypatch.setattr(db, "_write_input_turn_on_conn", fail)
    with pytest.raises(OSError, match="phase write failed"):
        db.record_context_stop("s")
    assert db.read_context_stop("s") is None
    assert db.read_context_input_work("s")["phase"] == original
    assert db.read_context_input_stop_disposition("s", phase_id=original["phase_id"]) is None


def test_cancelled_wake_budget_resets_only_for_new_stop_epoch(db):
    first = accept(db)
    for _ in range(3):
        db.reserve_context_input_wake("s", receipt=first)
    begin(db, first)
    project(db, first)
    db.record_context_stop("s")
    second = accept(db, event="C")
    assert db.reserve_context_input_wake("s", receipt=second)["attempts"] == 1
    assert db.reserve_context_input_wake("s", receipt=second)["attempts"] == 2
    third = accept(db, event="D")
    assert db.reserve_context_input_wake("s", receipt=third)["attempts"] == 3
    with pytest.raises(ContextContinuationError, match="WAKE_EXHAUSTED"):
        db.reserve_context_input_wake("s", receipt=third)


def test_actual_agent_can_answer_fresh_input_after_pre_dispatch_stop(durable_agent):
    agent, db = durable_agent
    def stop_before_dispatch(payload, execute, **kwargs):
        db.record_context_stop(agent.session_id)
        return execute(payload)
    with (patch("hermes_cli.middleware.run_llm_execution_middleware", side_effect=stop_before_dispatch),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        first_result = agent.run_conversation("first", persist_user_event_id="A")
    assert first_result.get("completed") is False
    agent.client.chat.completions.create.assert_not_called()
    old = db.read_context_input_work(agent.session_id)["phase"]
    assert old["state"] == "cancelled"
    agent.client.chat.completions.create.return_value = _mock_response(content="second answered", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("second", persist_user_event_id="C",
            conversation_history=db.get_messages_as_conversation(agent.session_id, include_row_ids=True))
    assert result.get("error") is None, result
    assert agent.client.chat.completions.create.call_count == 1
    fresh = db.read_context_input_work(agent.session_id)["phase"]
    assert fresh["state"] == "answered"
    assert fresh["phase_id"] != old["phase_id"]
    assert fresh["first_sequence"] == fresh["last_sequence"]
    assert db.read_context_input_stop_disposition(agent.session_id, phase_id=old["phase_id"])["after"] == old
