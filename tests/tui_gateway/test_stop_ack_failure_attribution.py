"""Interrupt rejection must retain correlation without claiming no effects."""
import io
import json
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor, HostSendUncertain


def test_interrupt_application_error_keeps_target_and_redacts_exception(monkeypatch):
    session = {"agent": None, "running": True, "history_lock": threading.Lock(), "session_key": "stored"}
    monkeypatch.setattr(server, "_sessions", {"s": session})

    def fail(*_args, **_kwargs):
        raise RuntimeError("private-auth-value-and-prompt")

    monkeypatch.setattr(server, "_interrupt_session_turn", fail)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        ack = json.loads(output.getvalue().strip())
        assert ack["target_request_id"] == "A", "failure lost the accepted target correlation"
        assert ack["applied"] is False
        assert ack["effect_disposition"] == "indeterminate", "an application exception is not non-effect proof"
        assert ack["failure_code"] == "interrupt_application_failed"
        assert "private-auth-value-and-prompt" not in output.getvalue()
    finally:
        host.close()


def test_known_target_rejection_is_explicitly_before_effects(monkeypatch):
    called = []
    monkeypatch.setattr(server, "_sessions", {"s": {"agent": None, "running": True}})
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **kw: called.append(True))
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids["s"] = "B"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        ack = json.loads(output.getvalue().strip())
        assert ack["applied"] is False
        assert ack["target_request_id"] == "A"
        assert ack["effect_disposition"] == "not_applied"
        assert ack["failure_code"] == "turn_owner_changed"
        assert called == []
        assert host._active_request_ids["s"] == "B"
    finally:
        host.close()


def test_interrupt_ack_from_other_boot_is_not_confirmation(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "current"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)

    def answer(frame, **_kwargs):
        host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": frame["request_id"],
                                 "target_request_id": "A", "applied": True, "_host_boot_id": "retired"})

    monkeypatch.setattr(host, "_send_owner_control_bounded", answer)
    with pytest.raises(HostSendUncertain, match="boot"):
        host.interrupt("s", request_id="stop", target_request_id="A", wait=True, expected_boot_id="current")
    assert not host._pending_controls
