"""A judge outage must not drive a goal continuation.

`judge_goal` returns ``("continue", "judge error: …", …, transport_failed=True)``
when the auxiliary provider is unreachable. Treating that as a real verdict fired
the next turn blind and counted it against the budget as if the goal had been
evaluated. The counters still auto-pause a persistently broken judge.
"""

import hermes_cli.goals as goals
from hermes_cli.goals import GoalManager, GoalState


def _manager():
    mgr = object.__new__(GoalManager)
    mgr.session_id = "session-1"
    mgr._state = GoalState(
        goal="ship it",
        status="active",
        outcome=goals.GOAL_ACTIVE,
        turns_used=1,
        max_turns=20,
    )

    return mgr


def test_transport_failure_parks_instead_of_continuing(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *a, **k: ("continue", "judge error: ConnectionError: boom", False, None, True),
    )
    mgr = _manager()

    decision = mgr.evaluate_after_turn("some work")

    assert decision["should_continue"] is False
    assert decision["continuation_prompt"] is None
    assert decision["status"] == "active"
    assert "Goal judge unavailable" in decision["message"]


def test_parse_failure_keeps_its_existing_contract(monkeypatch):
    # Deliberate distinction: parse failures tolerate two and auto-pause on the
    # third (tests/hermes_cli/test_goals.py). Only an unreachable judge parks
    # immediately, because no verdict was ever produced.
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *a, **k: ("continue", "unparseable verdict", True, None, False),
    )
    mgr = _manager()
    mgr._checkpoint = lambda *a, **k: True
    mgr.next_continuation_prompt = lambda: "next step"

    decision = mgr.evaluate_after_turn("some work")

    assert decision["should_continue"] is True
    assert "Continuing toward goal" in decision["message"]
