"""Live input must reach the executing host, never a prewarmed parent agent."""
import io
import json
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSendUncertain
from tests.run_agent.test_run_agent import agent  # noqa: F401


@pytest.mark.parametrize("route,status", [("session.steer", "queued"), ("session.redirect", "redirected")])
def test_live_input_routes_to_exact_host_owner(monkeypatch, route, status):
    parent = types.SimpleNamespace(steer=lambda text: pytest.fail("parent agent steered"),
                                   redirect=lambda text: pytest.fail("parent agent redirected"),
                                   _supports_active_turn_redirect=True)
    session = {"agent": parent, "history_lock": threading.Lock(), "running": True,
               "_compute_host_active_request_id": "A"}
    calls = []
    class Host:
        boot_id = "boot"
        def control(self, sid, **kwargs):
            calls.append(kwargs)
            payload = kwargs["payload"]
            return {"type": "control.ack", "sid": sid, "request_id": payload["request_id"],
                    "target_request_id": "A", "route_name": route,
                    "response": server._ok(payload["request_id"], {"status": status, "text": "correction"})}
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: Host())
    corrections = []
    monkeypatch.setattr(server, "_record_inflight_correction", lambda state, text: corrections.append(text))
    result = server._methods[route]("rpc", {"session_id": "s", "text": "correction"})
    assert result["result"]["status"] == status
    assert result["id"] == "rpc"
    assert calls[0]["payload"]["target_request_id"] == "A"
    assert calls[0]["expected_boot_id"] == "boot"
    assert corrections == ["correction"]


def test_unknown_live_input_outcome_is_not_replayed(monkeypatch):
    session = {"history_lock": threading.Lock(), "_compute_host_active_request_id": "A"}
    calls = []
    class Host:
        boot_id = "boot"
        def control(self, *args, **kwargs):
            calls.append(kwargs)
            raise HostSendUncertain("ACK lost")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: Host())
    result = server._send_host_live_input("rpc", "s", session, "correction", "session.steer")
    assert result["error"]["code"] == 5032
    assert len(calls) == 1


@pytest.mark.parametrize("handoff", ["request", "mirror"])
def test_late_live_input_ack_does_not_edit_successor_snapshot(monkeypatch, handoff):
    session = {"history_lock": threading.Lock(), "_compute_host_active_request_id": "A"}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    class Host:
        boot_id = "boot"
        def control(self, sid, **kwargs):
            if handoff == "request":
                session["_compute_host_active_request_id"] = "B"
            else:
                server._sessions["s"] = dict(session)
            payload = kwargs["payload"]
            return {"type": "control.ack", "sid": sid, "request_id": payload["request_id"],
                    "target_request_id": "A", "route_name": "session.steer",
                    "response": server._ok(payload["request_id"], {"status": "queued"})}
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: Host())
    monkeypatch.setattr(server, "_record_inflight_correction", lambda *a: pytest.fail("successor was changed"))
    result = server._send_host_live_input("rpc", "s", session, "correction", "session.steer")
    assert result["result"]["status"] == "queued"


@pytest.mark.parametrize("target,applied", [("A", True), ("old", False)])
def test_host_control_reaches_real_agent_only_for_current_target(agent, monkeypatch, target, applied):
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    agent._pending_steer = None
    session = {"agent": agent, "running": True, "history_lock": threading.Lock()}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    host._active_request_ids["s"] = "A"
    try:
        host._handle_control({"sid": "s", "request_id": "control", "route_name": "session.steer",
                              "target_request_id": target, "params": {"text": "correction"}})
        ack = json.loads(host._stdout.getvalue().strip())
        if applied:
            assert ack["response"]["result"]["status"] == "queued"
            assert agent._pending_steer == "correction"
        else:
            assert ack["not_applied"] is True
            assert agent._pending_steer is None
    finally:
        host.close()


@pytest.mark.parametrize("code", [4010, 5032])
def test_busy_input_only_queues_after_proven_refusal(monkeypatch, code):
    session = {"agent": types.SimpleNamespace(steer=lambda text: pytest.fail("parent steer called")),
               "running": True, "history_lock": threading.Lock()}
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_send_host_live_input", lambda *a: server._err("rpc", code, "test refusal"))
    response = server._handle_busy_submit("rpc", "s", session, "correction", None)
    assert response is not None
    if code == 4010:
        assert response["result"]["status"] == "queued"
        assert session["queued_prompt"]["text"] == "correction"
    else:
        assert response["error"]["code"] == 5032
        assert not session.get("queued_prompt")


@pytest.mark.parametrize("code", [4010, 5032])
def test_slash_steer_does_not_turn_uncertain_delivery_into_send(monkeypatch, code):
    session = {"agent": types.SimpleNamespace(steer=lambda text: pytest.fail("parent steer called"))}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_send_host_live_input", lambda *a: server._err("rpc", code, "test refusal"))
    response = server._methods["command.dispatch"]("rpc", {"session_id": "s", "name": "steer", "arg": "correction"})
    if code == 4010:
        assert response["result"] == {"type": "send", "message": "correction"}
    else:
        assert response["error"]["code"] == 5032
