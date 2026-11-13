"""`session.info` must say WHAT a running turn is doing, not just that it runs.

`running: true` conflated model generation, tool execution, and the post-turn
goal judge. The judge runs *after* `message.complete`, so the transcript looked
finished while admission was still owned — a normal send landing there was
refused as "session busy". These fields let a client explain the window instead
of guessing.
"""

import threading

from tui_gateway import server


def _session(**extra):
    return {
        "history_lock": threading.Lock(),
        "running": True,
        **extra,
    }


def test_no_activity_fields_without_a_recorded_activity():
    assert server._activity_fields({}) == {}
    assert server._activity_fields(None) == {}


def test_activity_fields_carry_timestamps():
    fields = server._activity_fields(
        _session(
            turn_activity="goal_judge",
            activity_since=100.0,
            inflight_turn={"updated_at": 101.5},
        )
    )

    assert fields == {
        "activity": "goal_judge",
        "activity_since": 100.0,
        "last_progress_at": 101.5,
    }


def test_non_numeric_timestamps_are_dropped_not_coerced():
    fields = server._activity_fields(
        _session(turn_activity="user", activity_since="soon", inflight_turn={"updated_at": None})
    )

    assert fields == {"activity": "user"}


def test_session_info_merges_activity_fields():
    info = server._session_info(
        None,
        _session(
            turn_activity="goal_judge",
            activity_since=100.0,
            inflight_turn={"updated_at": 101.5},
        ),
        lock_held=True,
    )

    assert info["activity"] == "goal_judge"
    assert info["activity_since"] == 100.0
    assert info["last_progress_at"] == 101.5


def test_session_info_omits_activity_when_idle():
    info = server._session_info(None, _session(running=False), lock_held=True)

    assert "activity" not in info
