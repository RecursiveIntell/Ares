"""Reasoning writes must execute on the selected host, not its serving mirror."""
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture
def serving(monkeypatch):
    agent = SimpleNamespace(model="fixture", provider="fixture", reasoning_config={"enabled": True, "effort": "low"})
    record = {"session_key": "stored", "agent": agent, "_compute_host_active": True,
              "history_lock": threading.Lock(), "history": [], "running": False,
              "_metadata_mirror": {"reasoning_effort": "low"}}
    monkeypatch.setattr(server, "_sessions", {"live": record})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: True)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_emit", Mock())
    monkeypatch.setattr(server, "_persist_live_session_runtime", Mock())
    monkeypatch.setattr(server, "_write_config_key", Mock(side_effect=AssertionError("global write")))
    return record


def dispatch():
    return server._methods["config.set"]("request", {"session_id": "live", "key": "reasoning", "value": "xhigh"})


def ack():
    return {"type": "control.ack", "sid": "live", "route_name": "config.set.reasoning",
            "result": {"key": "reasoning", "value": "xhigh"},
            "session_info": {"reasoning_effort": "xhigh"}}


def test_host_ack_updates_mirror_not_parent(serving, monkeypatch):
    send = Mock(return_value=ack())
    monkeypatch.setattr(server, "_send_compute_host_control", send)
    response = dispatch()
    assert send.call_count == 1
    assert send.call_args.kwargs["route_name"] == "config.set.reasoning"
    assert response["result"]["value"] == "xhigh"
    assert serving["agent"].reasoning_config["effort"] == "low"
    assert serving["_metadata_mirror"]["reasoning_effort"] == "xhigh"
    assert serving["create_reasoning_override"] == {"enabled": True, "effort": "xhigh"}
    server._persist_live_session_runtime.assert_not_called()


@pytest.mark.parametrize("bad", ["timeout", "wrong_session", "missing_readback", "contradictory_readback"])
def test_unconfirmed_write_never_paints_success(serving, monkeypatch, bad):
    response = ack()
    if bad == "wrong_session":
        response["sid"] = "other"
    elif bad == "missing_readback":
        response.pop("session_info")
    elif bad == "contradictory_readback":
        response["session_info"]["reasoning_effort"] = "low"
    send = Mock(side_effect=TimeoutError("lost ack")) if bad == "timeout" else Mock(return_value=response)
    monkeypatch.setattr(server, "_send_compute_host_control", send)
    result = dispatch()
    assert result["error"]["code"] == 5019
    assert serving["agent"].reasoning_config["effort"] == "low"
    assert serving["_metadata_mirror"]["reasoning_effort"] == "low"
    assert "create_reasoning_override" not in serving


@pytest.mark.parametrize("busy", [False, True])
def test_host_handler_applies_only_to_idle_owner(monkeypatch, busy):
    agent = SimpleNamespace(model="fixture", provider="fixture", reasoning_config={"enabled": True, "effort": "low"})
    record = {"session_key": "stored", "agent": agent, "running": busy}
    monkeypatch.setattr(server, "_sessions", {"live": record})
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_emit", Mock())
    persist = Mock()
    monkeypatch.setattr(server, "_persist_live_session_runtime", persist)
    monkeypatch.setattr(server, "_write_config_key", Mock(side_effect=AssertionError("global write")))
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._handle_control({"sid": "live", "request_id": "control", "route_name": "config.set.reasoning",
                              "params": {"value": "xhigh"}})
    finally:
        host.close()
    result = json.loads(output.getvalue().splitlines()[0])
    if busy:
        assert result["type"] == "control.error"
        assert result["message"] == "session busy"
        assert agent.reasoning_config["effort"] == "low"
        persist.assert_not_called()
    else:
        assert result["type"] == "control.ack"
        assert agent.reasoning_config["effort"] == "xhigh"
        assert result["session_info"]["reasoning_effort"] == "xhigh"
        persist.assert_called_once_with(record)


def test_replaced_parent_owner_does_not_accept_late_ack(serving, monkeypatch):
    replacement = {"session_key": "replacement", "_metadata_mirror": {"reasoning_effort": "medium"}}

    def replace_then_ack(*args, **kwargs):
        server._sessions["live"] = replacement
        return ack()

    monkeypatch.setattr(server, "_send_compute_host_control", replace_then_ack)
    assert dispatch()["error"]["code"] == 5019
    assert replacement["_metadata_mirror"]["reasoning_effort"] == "medium"
    assert serving["_metadata_mirror"]["reasoning_effort"] == "low"


def test_cold_reasoning_uses_existing_snapshot_reconstruction(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_emit", Mock())
    monkeypatch.setattr(server, "_persist_live_session_runtime", Mock())
    agent = SimpleNamespace(model="fixture", provider="fixture", reasoning_config=None)
    record = {"session_key": "stored", "agent": agent, "running": False}
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    snapshot = {"sid": "live", "session_key": "stored"}

    def restore(module, frame):
        assert frame is snapshot
        module._sessions["live"] = record
        return record

    reconstruct = Mock(side_effect=restore)
    monkeypatch.setattr(host, "_ensure_server_session", reconstruct)
    try:
        host._handle_control({"sid": "live", "request_id": "cold", "route_name": "config.set.reasoning",
                              "session_snapshot": snapshot, "params": {"value": "xhigh"}})
    finally:
        host.close()
    reconstruct.assert_called_once()
    result = json.loads(output.getvalue().splitlines()[0])
    assert result["type"] == "control.ack"
    assert result["session_info"]["reasoning_effort"] == "xhigh"
