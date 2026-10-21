"""Stop must hold the goal chain even when the turn cannot be interrupted.

`AIAgent.interrupt()` is cooperative. A provider read that never cancels leaves
`result["interrupted"]` false, so the post-turn judge saw a normal completion
and immediately re-fired the continuation — the Stop button ACKed while work
carried on. The compute host now records a Stop-era hold on the session; only a
user-originated turn (or an explicit `/goal resume`) lifts it.
"""

import threading
import types

import pytest

import tui_gateway.compute_host as compute_host
from tui_gateway import server


def test_interrupt_records_goal_stop_hold(monkeypatch):
    host = object.__new__(compute_host.ComputeHost)
    host._sessions = {}
    host._turn_futures_lock = threading.RLock()
    host._active_request_ids = {"s1": "req-1"}
    host._admitted_request_sessions = {}
    host._pending_interrupts = {}
    frames: list[dict] = []
    host.emit = frames.append

    session = {
        "agent": types.SimpleNamespace(),
        "history_lock": threading.Lock(),
        "running": True,
    }
    monkeypatch.setitem(server._sessions, "s1", session)
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **k: {"applied": True})

    host._handle_interrupt({"sid": "s1", "request_id": "ack-1"})

    assert session["_goal_suppress_after_interrupt"] is True
    assert frames[-1]["type"] == "interrupt.ack"
    assert frames[-1]["applied"] is True


@pytest.mark.parametrize(
    ("display_kind", "held_after"),
    [
        (None, False),
        ("hidden", False),
        ("internal_notification", True),
    ],
)
def test_only_a_user_turn_lifts_the_hold(display_kind, held_after):
    session = {"_goal_suppress_after_interrupt": True}

    server._clear_goal_stop_hold_for_user_turn(session, display_kind)

    assert server._goal_held_after_stop(session) is held_after


def test_hold_reads_false_when_absent():
    assert server._goal_held_after_stop({}) is False
