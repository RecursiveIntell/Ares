"""A targeted Stop can cancel an accepted request before its session exists."""
import io
import json
import threading

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def frames(output):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def test_stop_before_construction_prevents_agent_creation(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: pytest.fail("cancelled request constructed an agent"))
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        ack = frames(output)[-1]
        assert ack["applied"] is True
        assert ack["pending"] is True
        host._run_real_turn({"sid": "s", "request_id": "A", "text": "never run"})
        terminal = frames(output)[-1]
        assert terminal["type"] == "turn.end"
        assert terminal["request_id"] == "A"
        assert terminal["interrupted"] is True
        assert not host._pending_interrupts
        assert not host._active_request_ids
    finally:
        host.close()


def test_stale_prestart_stop_does_not_latch_new_owner(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(server, "_sessions", {})
    host._active_request_ids["s"] = "B"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "old-stop", "target_request_id": "A"})
        assert frames(output)[-1]["applied"] is False
        assert host._pending_interrupts == {}
        assert host._active_request_ids["s"] == "B"
    finally:
        host.close()


def test_stop_in_materialized_handoff_blocks_goal_entry(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    state = {"history_lock": threading.Lock(), "running": False, "session_key": "stored"}
    monkeypatch.setattr(server, "_sessions", {"s": state})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        assert frames(output)[-1]["applied"] is True
        assert state["_turn_cancel_requested"] is True
        assert host._pending_interrupts["s"] == "A"
        assert server._run_prompt_submit("A", "s", state, "goal successor") is False
    finally:
        host.close()


def test_stop_during_construction_prevents_provider_admission(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    state = {"history_lock": threading.Lock(), "running": False, "session_key": "stored"}
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a: pytest.fail("cancelled request admitted"))
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: pytest.fail("provider invoked"))
    def construct(*args):
        entered.set()
        release.wait(3)
        return state
    monkeypatch.setattr(host, "_ensure_server_session", construct)
    original = host.emit
    def emit(frame):
        original(frame)
        if frame.get("type") in {"turn.end", "turn.error"}:
            done.set()
    monkeypatch.setattr(host, "emit", emit)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A", "text": "never run"})
        assert entered.wait(1)
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        assert frames(output)[-1]["applied"] is True
        release.set()
        assert done.wait(2)
        terminal = frames(output)[-1]
        assert terminal["type"] == "turn.end"
        assert terminal["interrupted"] is True
        assert not any(f["type"] == "turn.started" for f in frames(output))
    finally:
        release.set()
        host.close()
        host._executor.shutdown(wait=True, cancel_futures=True)
