"""Finalization must never run underneath a live turn thread.

`_teardown_popped_session` waits a bounded grace for the run thread, then used
to finalize anyway. A thread that outlived the grace kept appending history
after the row was stamped `ended_at` — one observed session had 53 messages
newer than its own end timestamp. The teardown now defers to the turn's settle
path, which already knows how to run a deferred teardown.
"""

import threading

import tui_gateway.server as server


def _live_thread():
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()

    return thread, stop


def _dead_thread():
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()

    return thread


def _session(thread):
    return {"_run_thread": thread, "_sid": "s1", "history_lock": threading.Lock()}


def _record_teardown(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda session, end_reason="tui_close": calls.append(end_reason),
    )
    monkeypatch.setattr(server, "_TURN_SETTLE_BEFORE_CLOSE_SECONDS", 0.01)

    return calls


def test_live_turn_defers_teardown(monkeypatch):
    thread, stop = _live_thread()
    calls = _record_teardown(monkeypatch)
    session = _session(thread)

    try:
        assert server._teardown_popped_session(session, end_reason="ws_orphan_reap") is True
    finally:
        stop.set()
        thread.join(timeout=2)

    assert calls == []
    assert session["_run_checkpoint_teardown_deferred"] == "ws_orphan_reap"


def test_finished_turn_tears_down_immediately(monkeypatch):
    calls = _record_teardown(monkeypatch)
    session = _session(_dead_thread())

    server._teardown_popped_session(session, end_reason="ws_orphan_reap")

    assert calls == ["ws_orphan_reap"]
    assert "_run_checkpoint_teardown_deferred" not in session


def test_shutdown_never_defers(monkeypatch):
    thread, stop = _live_thread()
    calls = _record_teardown(monkeypatch)

    try:
        server._teardown_popped_session(_session(thread), end_reason="tui_shutdown")
    finally:
        stop.set()
        thread.join(timeout=2)

    assert calls == ["tui_shutdown"]
