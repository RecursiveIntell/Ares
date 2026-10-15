"""Budget settlement is not permission for another model turn."""
import pytest

from hermes_cli import goals


@pytest.fixture
def exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals._get_session_db().create_session("budget-stop", source="cli")
    mgr = goals.GoalManager("budget-stop")
    mgr.set("bounded work", max_turns=1)
    mgr.state.turns_used = 1
    assert goals.save_goal(mgr.session_id, mgr.state)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: pytest.fail("exhausted goal was judged"))
    monkeypatch.setattr(goals, "run_gate", lambda *a, **k: pytest.fail("exhausted goal ran a gate"))
    yield mgr
    goals._DB_CACHE.clear()


def test_current_exhausted_evaluation_persists_stop_once(exhausted):
    before_revision = exhausted.state.checkpoint_revision
    assert not exhausted.auto_eligible()
    assert not exhausted.claim_continuation("no-budget")
    decision = exhausted.evaluate_after_turn("work finished")
    assert decision["status"] == "paused"
    assert "turns used" in decision["message"]
    persisted = goals.GoalManager(exhausted.session_id)
    assert persisted.state.outcome == goals.TURN_BUDGET_EXHAUSTED
    assert persisted.state.turns_used == 1
    assert persisted.state.checkpoint_revision == before_revision + 1
    assert persisted.validate_checkpoint()[0]
    assert not persisted.state.continuation_pending
    assert not persisted.start_continuation()
    before = persisted.state.to_json()
    assert persisted.evaluate_after_turn("duplicate evaluation")["verdict"] == "inactive"
    assert goals.load_goal(persisted.session_id).to_json() == before


@pytest.mark.parametrize("metadata", [
    {"origin": "notification"},
    {"continuation": {"goal_id": "wrong", "checkpoint_revision": 1, "continuation_token": "wrong"}},
])
def test_nonowning_evaluation_cannot_rewrite_budget_stop(exhausted, metadata):
    before = goals.load_goal(exhausted.session_id).to_json()
    assert exhausted.evaluate_after_turn("notice", turn_metadata=metadata)["verdict"] == "inactive"
    assert goals.load_goal(exhausted.session_id).to_json() == before


def test_stale_goal_manager_cannot_pause_replacement(exhausted):
    replacement = goals.GoalManager(exhausted.session_id)
    replacement.set("new authorized work", max_turns=3)
    before = goals.load_goal(exhausted.session_id).to_json()
    assert exhausted.evaluate_after_turn("late reply")["verdict"] == "inactive"
    assert goals.load_goal(exhausted.session_id).to_json() == before
