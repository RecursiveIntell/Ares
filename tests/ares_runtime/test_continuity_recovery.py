"""Publication and restart behavior through the actual SessionDB owners."""
import json

import pytest

from ares_runtime.continuity import runtime
from hermes_cli.goals import GoalState
from hermes_cli.heartbeat import HeartbeatState
from hermes_cli.loops import LoopState
from tests.ares_runtime.test_continuity_runtime import setup  # noqa: F401


def attempt(setup):
    _, agent, messages, history, _ = setup
    return runtime.attempt_turn_start_context_rebase(
        agent, messages, conversation_history=history,
        active_system_prompt=agent._cached_system_prompt, before_tokens=100_000,
    )


def test_local_owner_bindings_are_atomic_with_child_publication(setup, monkeypatch):
    db, _, _, _, _ = setup
    states = {
        "goal": GoalState(goal="Fix the race", created_at=1.0),
        "heartbeat": HeartbeatState(prompt="check", interval_seconds=60, created_at=1.0),
        "loop": LoopState(prompt="check", created_at=1.0),
    }
    for name, state in states.items():
        db.set_meta(f"{name}:s0", state.to_json())
    publish = db.publish_context_rebase_child
    observations = []

    def committed(**kwargs):
        result = publish(**kwargs)
        observations.append({
            name: (json.loads(db.get_meta(f"{name}:s0"))["status"],
                   db.get_meta(f"{name}:{result.child_session_id}"))
            for name in states
        })
        return result

    monkeypatch.setattr(db, "publish_context_rebase_child", committed)
    result = attempt(setup)
    assert result.ready
    assert observations
    for status, child_raw in observations[0].values():
        assert status == "cleared"
        assert child_raw is not None
        assert json.loads(child_raw)["status"] == "active"


def test_restart_reconciles_the_committed_child_without_forking(setup):
    db, agent, _, _, transitions = setup
    original = agent._transition_context_engine_session

    def crash(**kwargs):
        raise RuntimeError("lost engine acknowledgment")

    agent._transition_context_engine_session = crash
    pending = attempt(setup)
    assert pending.status is runtime.AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    child_id = pending.session_id
    before_count = db.message_count(child_id)
    agent._transition_context_engine_session = original
    result = runtime.reconcile_context_rebase(agent)
    assert result.ready
    assert result.session_id == child_id
    assert db.read_context_rebase_transition(pending.transition_id).state == "ready"
    assert db.message_count(child_id) == before_count
    assert db.get_context_continuation_tip("s0") == child_id
    assert transitions[-1]["new_session_id"] == child_id


def test_recovery_cannot_run_under_a_different_turn_lease(setup):
    db, agent, _, _, _ = setup
    agent._transition_context_engine_session = lambda **_: (_ for _ in ()).throw(RuntimeError())
    pending = attempt(setup)
    agent._active_session_turn_lease_holder = "stale-holder"
    result = runtime.reconcile_context_rebase(agent)
    assert result.status is runtime.AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    assert result.reason == "TURN_LEASE_MISMATCH"
    assert db.read_context_rebase_transition(pending.transition_id).state != "ready"


def test_failed_owner_transfer_rolls_back_child_and_parent_closure(setup, monkeypatch):
    db, _, _, _, _ = setup
    goal = GoalState(goal="Retain this goal", created_at=1.0)
    db.set_meta("goal:s0", goal.to_json())
    original = db._migrate_context_rebase_owners_on_conn

    def fail_after_transfer(conn, parent, child):
        original(conn, parent, child)
        raise OSError("transaction interrupted after owner changes")

    monkeypatch.setattr(db, "_migrate_context_rebase_owners_on_conn", fail_after_transfer)
    result = attempt(setup)
    assert result.status is runtime.AutomaticRebaseStatus.BLOCKED
    assert db.get_session("s0")["ended_at"] is None
    assert db.get_context_continuation_tip("s0") == "s0"
    assert db.get_meta("goal:s0") == goal.to_json()
    assert db.read_context_rebase_transition(result.transition_id) is None


def test_lost_publication_ack_reads_committed_child_instead_of_republishing(setup, monkeypatch):
    db, _, _, _, _ = setup
    publish = db.publish_context_rebase_child
    calls = []

    def commit_then_lose_ack(**kwargs):
        calls.append(kwargs["child_session_id"])
        publish(**kwargs)
        raise OSError("lost acknowledgment after commit")

    monkeypatch.setattr(db, "publish_context_rebase_child", commit_then_lose_ack)
    result = attempt(setup)
    assert result.ready
    assert calls == [result.session_id]
    assert db.get_context_continuation_tip("s0") == result.session_id


def test_recovery_attempt_budget_survives_store_reopen(setup):
    from hermes_state import SessionDB

    db, agent, _, _, _ = setup
    agent._transition_context_engine_session = lambda **_: (_ for _ in ()).throw(RuntimeError())
    pending = attempt(setup)
    assert pending.status is runtime.AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    other = SessionDB(db_path=db.db_path)
    try:
        agent._session_db = other
        for _ in range(2):
            result = runtime.reconcile_context_rebase(agent)
            assert result.status is runtime.AutomaticRebaseStatus.RECONCILIATION_REQUIRED
        result = runtime.reconcile_context_rebase(agent)
        assert result.reason == "CONTEXT_REBASE_RECOVERY_EXHAUSTED"
        assert other.get_context_continuation_tip("s0") == pending.session_id
        assert other.get_session("s0")["ended_at"] is not None
    finally:
        other.close()
