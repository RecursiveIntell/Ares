"""F0.1: GoalManager.set() raises GoalPersistenceError per persistence cause;
surfaces print the per-cause message instead of a false success line."""
import pytest

from hermes_state import SessionDB
from hermes_cli import goals
from hermes_cli.goals import (
    GoalPersistenceCause,
    GoalPersistenceError,
    _goal_persistence_message,
)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(goals, "_MEMORY_DEFERRED_GOAL_WRITES", {})
    yield db
    db.close()


def test_set_raises_typed_unavailable(monkeypatch):
    monkeypatch.setattr(goals, "_get_session_db", lambda: None)
    mgr = goals.GoalManager("sid-honesty")
    with pytest.raises(GoalPersistenceError) as excinfo:
        mgr.set("ship it")
    assert excinfo.value.cause == GoalPersistenceCause.UNAVAILABLE
    assert mgr.state is None
    # ...and the per-cause message never advertises /goal status as proof.
    msg = _goal_persistence_message(excinfo.value.cause)
    assert "NOT yet persisted" in msg
    assert "Do not rely on /goal status alone" in msg


def test_set_raises_typed_conflict_on_concurrent_winner(owner):
    mgr = goals.GoalManager("sid-conflict")
    first = mgr.set("first")
    second = mgr.set("second")

    # Simulate a stale writer arriving with the OLD expected raw: the row
    # already moved, so the CAS loses and the cause must be CONFLICT.
    stale = goals.GoalState.from_json(first.to_json())
    expected_stale = goals.GoalState(goal="seed", goal_id=first.goal_id)
    outcome = goals.save_goal_outcome(
        "sid-conflict", stale,
        expected_raw=expected_stale.to_json(),
    )
    assert outcome.is_durable is False
    assert outcome.cause == GoalPersistenceCause.CONFLICT
    assert goals.load_goal("sid-conflict").goal_id == second.goal_id
    del expected_stale  # silence linters; documented above


def test_save_goal_wrapper_preserves_bool_contract(owner):
    mgr = goals.GoalManager("sid-bool")
    state = mgr.set("first")
    assert goals.save_goal("sid-bool", state) is True
    stale = goals.GoalState.from_json(state.to_json())
    mgr.set("second")
    assert goals.save_goal("sid-bool", stale) is False


def test_conflict_message_mentions_concurrent_writer():
    msg = _goal_persistence_message(GoalPersistenceCause.CONFLICT)
    assert "concurrently" in msg


def test_write_failed_and_invalid_session_messages():
    assert "retry" in _goal_persistence_message(GoalPersistenceCause.WRITE_FAILED).lower()
    assert "live session" in _goal_persistence_message(GoalPersistenceCause.INVALID_SESSION)
