"""Publication rollback preserves control and readers for committed lineages."""
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import context_dispatch_required
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.ares_runtime.test_context_rebase_state import _publish, _ready
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def successor(agent, db, *, ready=True, compressed=False):
    db.create_session("s0", source="cli", model="test", profile_name="p1")
    db.append_message("s0", "user", "Retain the original restrictions")
    assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
    transition = _publish(db)
    if ready:
        _ready(db, transition.transition_id,
               expected_continuation_digest=transition.continuation_digest,
               expected_child_session_id="s1")
    session_id = "s1"
    if compressed:
        db.end_session("s1", "compression")
        db.create_session("s2", source="cli", model="test", profile_name="p1", parent_session_id="s1")
        db.append_message("s2", "user", "Continue within those restrictions")
        session_id = "s2"
    db.release_session_turn_lease("s0", "holder")
    agent.session_id = session_id
    agent.context_rebase_enabled = False
    return transition


@pytest.mark.parametrize("state", ["ready", "pending", "compressed"])
def test_disabled_publication_still_seals_existing_lineage_turn(durable_agent, state):
    agent, db = durable_agent
    transition = successor(agent, db, ready=state != "pending", compressed=state == "compressed")
    agent.client.chat.completions.create.return_value = _mock_response(content="continued")
    with (patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("A new authentic instruction", conversation_history=db.get_messages_as_conversation(agent.session_id, repair_alternation=False, include_row_ids=True))
    assert result.get("error") is None, result
    assert result["api_calls"] == 1
    assert db.read_context_rebase_transition(transition.transition_id).state == "ready"
    assert len(db.list_meta_prefix("context-dispatch:")) == 1
    assert len(db.list_meta_prefix("context-dispatch-result:")) == 1


def test_disabled_publication_still_records_durable_stop(durable_agent):
    agent, db = durable_agent
    successor(agent, db)
    agent.hard_interrupt()
    assert db.get_meta("context-control:s0") is not None
    assert agent._context_stop_unacknowledged is False


def test_disabled_legacy_session_keeps_legacy_admission(durable_agent):
    agent, db = durable_agent
    agent.context_rebase_enabled = False
    assert context_dispatch_required(agent) is False
    agent.client.chat.completions.create.return_value = _mock_response(content="legacy")
    with (patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Ordinary legacy task")
    assert result.get("error") is None, result
    assert result["api_calls"] == 1
    assert db.list_meta_prefix("context-dispatch:") == []
