"""Conversation attempt limits require a canonical, one-use operator action."""
import uuid
from unittest.mock import patch

import pytest

from hermes_cli import goals
from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.ares_runtime.test_continuity_rollback import successor
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def episode(durable_agent, monkeypatch):
    agent, db = durable_agent
    successor(agent, db)
    agent.context_rebase_enabled = True
    goal = goals.GoalState(goal="Keep the same task", created_at=1.0, turns_used=7)
    db.set_meta("goal:s1", goal.to_json())
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    return agent, db, goals.GoalManager("s1")


def test_healthy_provider_response_does_not_rearm_episode(durable_agent, monkeypatch):
    agent, db, _ = episode(durable_agent, monkeypatch)
    agent.client.chat.completions.create.return_value = _mock_response(
        content="A fluent reply, without verified work", usage={"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010},
    )
    before = db.read_context_rebase_episode(agent.session_id)
    with (patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Continue", conversation_history=db.get_messages_as_conversation("s1", repair_alternation=False))
    assert result["api_calls"] == 1
    assert db.read_context_rebase_episode(agent.session_id) == before
    with pytest.raises(ContextContinuationError, match="PROGRESS_EVIDENCE_REQUIRED"):
        db.reset_context_rebase_episode(agent.session_id)


def test_explicit_goal_resume_atomically_rearms_once_and_preserves_total_budget(durable_agent, monkeypatch):
    _, db, manager = episode(durable_agent, monkeypatch)
    assert db.read_context_rebase_episode("s1").attempts_without_recovery == 1
    resumed = manager.resume()
    assert resumed is not None
    assert resumed.turns_used == 7
    assert resumed.checkpoint["stop_reason"] == "USER_RESUMED"
    assert db.read_context_rebase_episode("s1").attempts_without_recovery == 0
    receipts = db.list_meta_prefix("context-operator-resume:")
    assert len(receipts) == 1
    basis = db.read_context_resume_basis("s1")
    with pytest.raises(ContextContinuationError, match="PROGRESS_ALREADY_CONSUMED"):
        db.resume_context_goal("s1", **basis, operator_action_id=receipts[0][0].split(":", 1)[1])
    assert db.read_context_resume_basis("s1") == basis


def test_operator_resume_rejects_stale_goal_episode_and_wrong_conversation(durable_agent, monkeypatch):
    _, db, _ = episode(durable_agent, monkeypatch)
    basis = db.read_context_resume_basis("s1")
    db.resume_context_goal("s1", **basis, operator_action_id=str(uuid.uuid4()))
    current = db.read_context_resume_basis("s1")
    with pytest.raises(ContextContinuationError, match="EPISODE_CHANGED"):
        db.resume_context_goal("s1", **basis, operator_action_id=str(uuid.uuid4()))
    with pytest.raises(ContextContinuationError, match="GOAL_CHANGED"):
        db.resume_context_goal("s1", expected_goal_raw=basis["expected_goal_raw"],
            expected_episode_raw=current["expected_episode_raw"], operator_action_id=str(uuid.uuid4()))
    db.create_session("other", source="cli")
    with pytest.raises(ContextContinuationError):
        db.resume_context_goal("other", **current, operator_action_id=str(uuid.uuid4()))
    assert db.read_context_resume_basis("s1") == current


def test_resume_failure_rolls_back_goal_episode_and_does_not_defer(durable_agent, monkeypatch):
    _, db, manager = episode(durable_agent, monkeypatch)
    before = db.read_context_resume_basis("s1")
    memory_before = manager.state.to_json()
    original = db._reset_context_rebase_episode_on_conn

    def interrupt(conn, session):
        original(conn, session)
        raise OSError("lost transaction before commit")

    monkeypatch.setattr(db, "_reset_context_rebase_episode_on_conn", interrupt)
    with patch.object(goals, "_defer_goal_write") as deferred:
        assert manager.resume() is None
    deferred.assert_not_called()
    assert manager.state.to_json() == memory_before
    assert db.read_context_resume_basis("s1") == before
    assert db.list_meta_prefix("context-operator-resume:") == []


def test_resume_lost_ack_can_be_read_without_replaying_reset(durable_agent, monkeypatch):
    _, db, manager = episode(durable_agent, monkeypatch)
    commit = db.resume_context_goal

    def lose_ack(*args, **kwargs):
        commit(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(db, "resume_context_goal", lose_ack)
    resumed = manager.resume()
    assert resumed is not None
    durable = goals.load_goal("s1")
    assert durable.checkpoint["stop_reason"] == "USER_RESUMED"
    assert db.read_context_rebase_episode("s1").attempts_without_recovery == 0
    assert len(db.list_meta_prefix("context-operator-resume:")) == 1
    revision, token = durable.checkpoint_revision, durable.continuation_token
    manager.pause()
    after = goals.load_goal("s1")
    assert after.checkpoint_revision == revision
    assert after.continuation_token == token


@pytest.mark.parametrize("failure", ["missing_db", "missing_session", "missing_helper"])
def test_resume_has_no_deferred_fallback(durable_agent, monkeypatch, failure):
    _, db, manager = episode(durable_agent, monkeypatch)
    before = db.read_context_resume_basis("s1")
    if failure == "missing_db":
        monkeypatch.setattr(goals, "_get_session_db", lambda: None)
    elif failure == "missing_session":
        monkeypatch.setattr(db, "get_session", lambda _: None)
    else:
        monkeypatch.setattr(type(db), "resume_context_goal", None)
    with patch.object(goals, "_defer_goal_write") as deferred:
        assert manager.resume() is None
    deferred.assert_not_called()
    assert db.read_context_resume_basis("s1") == before
    assert db.list_meta_prefix("context-operator-resume:") == []


def test_unresolved_resume_readback_prevents_stale_manager_writes(durable_agent, monkeypatch):
    _, db, manager = episode(durable_agent, monkeypatch)
    commit = db.resume_context_goal

    def lose_ack(*args, **kwargs):
        commit(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(db, "resume_context_goal", lose_ack)
    monkeypatch.setattr(db, "read_context_resume_outcome", lambda *_: (_ for _ in ()).throw(OSError("owner unavailable")))
    assert manager.resume() is None
    after = db.read_context_resume_basis("s1")
    assert manager.pause() is None
    assert manager.resume() is None
    assert manager._checkpoint(goals.CONTINUATION_REQUIRED, "stale", "stale", continuation=True) is False
    assert db.read_context_resume_basis("s1") == after
