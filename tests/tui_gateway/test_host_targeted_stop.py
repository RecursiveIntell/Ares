"""A Stop target cannot drift from one host request to its successor."""
import io
import json
import os
import threading
import types
from pathlib import Path

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor


@pytest.mark.parametrize("target,applied", [("A", True), ("old", False)])
def test_host_interrupt_is_bound_to_current_request(monkeypatch, target, applied):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    session = {"history_lock": threading.Lock(), "running": True}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    calls = []
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **kw: calls.append(a))
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": target})
        ack = json.loads(output.getvalue().strip())
        assert ack["applied"] is applied
        assert len(calls) == int(applied)
        assert host._active_request_ids["s"] == "A"
    finally:
        host.close()


def test_supervisor_preserves_explicit_stop_target(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    sent = []
    monkeypatch.setattr(host, "is_ready", lambda: True)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **kwargs: sent.append(frame))
    host.interrupt("s", request_id="stop", target_request_id="A")
    assert sent == [{"type": "interrupt", "sid": "s", "request_id": "stop", "target_request_id": "A"}]


def test_false_idle_parent_still_targets_known_owner(monkeypatch):
    session = {"running": False, "_compute_host_active_request_id": "A",
               "history_lock": threading.Lock(), "session_key": "stored"}
    calls = []
    def interrupt(sid, **kw):
        calls.append((sid, kw))
        return {"type": "interrupt.ack", "sid": sid, "request_id": kw["request_id"],
                "target_request_id": kw["target_request_id"], "applied": True}
    host = types.SimpleNamespace(interrupt=interrupt)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: host)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    assert server._interrupt_session_turn("s", session, request_id="stop")
    assert calls == [("s", {"request_id": "stop", "target_request_id": "A", "wait": True})]


def test_late_parent_stop_does_not_clear_successor_state(monkeypatch):
    queued = {"text": "post-handoff input"}
    session = {"running": True, "_compute_host_active_request_id": "A",
               "history_lock": threading.Lock(), "session_key": "stored", "queued_prompt": queued}
    def handoff(sid, **kwargs):
        assert kwargs["target_request_id"] == "A"
        session["_compute_host_active_request_id"] = "B"
        session["_turn_cancel_requested"] = False
        session["queued_prompt"] = queued
        return {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
                "target_request_id": "A", "applied": True}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor",
                        lambda: types.SimpleNamespace(interrupt=handoff))
    monkeypatch.setattr(server, "_clear_pending", lambda *a: pytest.fail("new owner pending UI cleared"))
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    assert server._interrupt_session_turn("s", session, request_id="stop")
    assert session["queued_prompt"] is queued
    assert not session.get("_turn_cancel_requested")


def test_cancelled_successor_cannot_clear_interrupt_or_start(monkeypatch):
    interrupted = []
    session = {"running": True, "history_lock": threading.Lock(),
               "agent": types.SimpleNamespace(
                   interrupt=lambda: interrupted.append(True),
                   clear_interrupt=lambda: pytest.fail("cancel latch cleared"))}
    monkeypatch.setitem(server._sessions, "s", session)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a: pytest.fail("cancelled turn admitted"))
    assert server._interrupt_session_turn("s", session) is False
    stopped_generation = session["_queued_prompt_generation"]
    assert server._run_prompt_submit("A", "s", session, "follow-up",
                                    display_kind="internal_notification") is False
    assert interrupted == [True]
    assert session["running"] is False
    assert session["inflight_turn"] is None
    assert session["_turn_cancel_requested"] is True
    assert session["_queued_prompt_generation"] == session["_last_stop_queue_generation"] == stopped_generation


def test_real_host_rejects_stale_stop_then_interrupts_matching_request(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    host = HostSupervisor(registry_path=home / "host.json", autostart=False,
        heartbeat_secs=0, respawn_max=0,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "HERMES_HOME": str(home),
             "HERMES_ISO_CERTIFY_SYNTH_TURN": "1", "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0"})
    started = threading.Event()
    wrong_seen = threading.Event()
    right_seen = threading.Event()
    done = threading.Event()
    acks = {}
    terminals = []
    original = host._handle_host_frame

    def capture(frame):
        if frame.get("type") == "turn.started":
            started.set()
        if frame.get("type") == "interrupt.ack":
            acks[frame["request_id"]] = frame
            (wrong_seen if frame["request_id"] == "wrong-stop" else right_seen).set()
        original(frame)

    def terminal(frame):
        terminals.append(frame)
        done.set()

    monkeypatch.setattr(host, "_handle_host_frame", capture)
    try:
        host.submit_turn({"sid": "s", "session_key": "stored", "request_id": "A",
                          "source": "desktop", "text": json.dumps({"duration_s": 5, "chunk": 1000})},
                         on_complete=terminal)
        assert started.wait(8)
        wrong_ack = host.interrupt("s", request_id="wrong-stop", target_request_id="old", wait=True, timeout=3)
        assert wrong_ack is not None and wrong_ack["applied"] is False
        assert wrong_seen.wait(3)
        assert acks["wrong-stop"]["applied"] is False
        assert not done.is_set()
        right_ack = host.interrupt("s", request_id="right-stop", target_request_id="A", wait=True, timeout=3)
        assert right_ack is not None and right_ack["applied"] is True
        assert right_seen.wait(3)
        assert acks["right-stop"]["applied"] is True
        assert done.wait(8)
        assert len(terminals) == 1
        assert terminals[0]["type"] == "turn.end"
        assert terminals[0]["request_id"] == "A"
        assert terminals[0]["interrupted"] is True
    finally:
        host.shutdown()
