"""Raw basis frames are strict at actual ingress; legacy JSON remains intact."""
import asyncio
import io
import json
import math
import threading
from types import SimpleNamespace

import pytest

from tui_gateway.checkpoint_json import BASIS_ROUTE, load_checkpoint_frame


BAD = [
    '{"method":"session.run_checkpoint.basis","params":{"run_id":"CANARY","run_id":"task"}}',
    '{"method":"session.run_checkpoint.basis","method":"legacy","params":{"token":"CANARY"}}',
    '{"meth\\u006fd":"session.run_checkpoint.basis","method":"legacy"}',
    '{"route_name":"session.run_checkpoint.basis","response":{"result":{"x":1,"x":2}}}',
    '{"route_name":"session.run_checkpoint.basis","route_name":"legacy"}',
    '{"type":"rpc","message":{"method":"session.run_checkpoint.basis"},"message":{"method":"legacy"}}',
    '{"method":"session.run_checkpoint.basis","params":{"n":NaN}}',
    '{"method":"session.run_checkpoint.basis","params":{"n":Infinity}}',
    '{"method":"session.run_checkpoint.basis","params":{"n":1e999}}',
    '{"method":"session.run_checkpoint.basis","params":{"secret":"CANARY",',
]


@pytest.mark.parametrize("raw", BAD)
def test_basis_raw_rejection(raw):
    with pytest.raises(json.JSONDecodeError):
        load_checkpoint_frame(raw)


def test_other_routes_keep_legacy_last_key_and_nonfinite_behavior():
    raw = '{"method":"legacy","params":{"x":1,"x":2,"nan":NaN,"inf":1e999}}'
    result = load_checkpoint_frame(raw)
    assert result["params"]["x"] == 2
    assert math.isnan(result["params"]["nan"])
    assert math.isinf(result["params"]["inf"])
    # A quoted nested route name is ordinary data, not a selected basis request.
    nested = '{"method":"legacy","params":{"method":"session.run_checkpoint.basis","x":1,"x":2}}'
    assert load_checkpoint_frame(nested) == json.loads(nested)
    assert load_checkpoint_frame('{"method":"session.run_checkpoint.basis","params":{}}')["params"] == {}


def test_real_stdio_ingress_rejects_before_dispatch(monkeypatch):
    import hermes_cli.model_switch as ms
    from tui_gateway import entry
    output, calls = [], []
    for name in ("_install_sidecar_publisher", "ensure_mcp_discovery_started"):
        monkeypatch.setattr(entry, name, lambda: None)
    monkeypatch.setattr(entry, "resolve_skin", lambda: "default")
    monkeypatch.setattr(entry.server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(entry, "_log_exit", lambda reason: None)
    monkeypatch.setattr(entry, "handle_spurious_eof", lambda *a: False)
    monkeypatch.setattr(ms, "prewarm_picker_cache_async", lambda: None)
    monkeypatch.setattr(entry, "write_json", lambda value: output.append(value) or True)
    monkeypatch.setattr(entry, "dispatch", lambda value: calls.append(value))
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO("\n".join(BAD) + '\n{"method":"legacy"}\n'))
    entry.main()
    assert calls == [{"method":"legacy"}]
    assert sum("error" in x for x in output) == len(BAD)
    assert "CANARY" not in json.dumps(output)


def test_real_compute_host_raw_ingress_rejects_before_control(monkeypatch):
    from tui_gateway import compute_host
    output, calls = io.StringIO(), []
    host = compute_host.ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(compute_host, "ComputeHost", lambda **kw: host)
    monkeypatch.setattr(compute_host.signal, "signal", lambda *a: None)
    monkeypatch.setattr(host, "handle_frame", lambda value: calls.append(value))
    compute_host.run_host(stdin=io.StringIO("\n".join(BAD) + '\n{"type":"legacy"}\n'), stdout=output)
    assert calls == [{"type":"legacy"}]
    frames = [json.loads(x) for x in output.getvalue().splitlines()]
    assert sum(x.get("type") == "error" for x in frames) == len(BAD)
    assert "CANARY" not in output.getvalue()


def test_real_supervisor_ack_decoder_drops_ambiguous_frames_and_redacts(monkeypatch, caplog):
    from tui_gateway.host_supervisor import HostSupervisor
    supervisor = HostSupervisor.__new__(HostSupervisor)
    proc = SimpleNamespace(stdout=io.StringIO("\n".join(BAD) + '\n{"type":"hello"}\n'))
    supervisor._proc = proc
    supervisor._registry_lock = threading.RLock()
    frames = []
    supervisor._handle_host_frame = frames.append
    supervisor._drain_stdout(proc)
    assert frames == [{"type":"hello"}]
    assert "CANARY" not in caplog.text


def test_real_websocket_ingress_rejects_before_dispatch_and_redacts(monkeypatch, caplog):
    from tui_gateway import ws
    from gateway import browser_control_broker
    output, calls = [], []
    class Socket:
        client = None
        scope = {}
        def __init__(self): self.inputs = iter(BAD + ['{"method":"legacy"}'])
        async def accept(self): pass
        async def receive_text(self):
            try: return next(self.inputs)
            except StopIteration: raise ws._WebSocketDisconnect(code=1000)
        async def send_text(self, raw): output.append(json.loads(raw))
        async def close(self): pass
    monkeypatch.setattr(ws.server, "resolve_skin", lambda: {})
    for name in ("_ensure_skin_watcher", "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(ws.server, name, lambda: None)
    for name in ("register_live_transport", "unregister_live_transport", "_release_wake_for_transport"):
        monkeypatch.setattr(ws.server, name, lambda *a: None)
    monkeypatch.setattr(ws.server, "_close_sessions_for_transport", lambda *a, **k: (0,0))
    monkeypatch.setattr(browser_control_broker, "get_browser_control_broker",
        lambda: SimpleNamespace(disconnect_owner=lambda *a: None))
    monkeypatch.setattr(ws.server, "dispatch", lambda value, transport: calls.append(value))
    asyncio.run(ws.handle_ws(Socket()))
    assert calls == [{"method":"legacy"}]
    assert sum("error" in x for x in output) == len(BAD)
    assert "CANARY" not in caplog.text + json.dumps(output)
