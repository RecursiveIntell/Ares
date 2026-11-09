"""Controlled test for WebSocket attachment readiness before broker dispatch."""
import asyncio
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.browser_control_broker import ControllerScope, ControllerUnavailable
from tests.gateway.test_browser_control_api import (
    API_KEY,
    CONTROL_PROTOCOL,
    _adapter,
    _app,
    _registration_body,
    _ticket_protocol,
    _wait_for_controller_ready,
)
from tools.browser_extension_router import route_browser_tool


@pytest.mark.asyncio
async def test_heartbeat_ack_fences_dispatch_until_exact_scope_is_attached(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    broker = adapter._browser_control_broker
    original_attach = broker.attach
    attach_entered = asyncio.Event()
    release_attach = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_attach(*args, **kwargs):
        loop.call_soon_threadsafe(attach_entered.set)
        if not release_attach.wait(timeout=2.0):
            raise AssertionError("fixture did not release the held broker attach")
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(broker, "attach", delayed_attach)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["browser_snapshot"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 201
        registration = await response.json()
        scoped = registration["scope"]
        scope = ControllerScope(
            principal_id=scoped["principal_id"],
            profile_id=scoped["profile_id"],
            session_id=scoped["session_id"],
            controller_id=scoped["controller_id"],
            browser_profile_id=scoped["browser_profile_id"],
            transport_family=scoped["transport_family"],
            capabilities=frozenset(scoped["capabilities"]),
        )
        ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
        )
        try:
            await asyncio.wait_for(attach_entered.wait(), timeout=2.0)

            # REST registration and socket-open do not bind a broker lane.
            # The exact-scope broker dispatch is the pre-readiness negative.
            with pytest.raises(ControllerUnavailable):
                await asyncio.to_thread(
                    broker.dispatch,
                    scope,
                    action="browser_snapshot",
                    arguments={},
                    tool_call_id="before-heartbeat-ready",
                )

            release_attach.set()
            await _wait_for_controller_ready(ws, "readiness-control-scope")
            pending = asyncio.create_task(
                asyncio.to_thread(
                    route_browser_tool,
                    "browser_snapshot",
                    {},
                    fallback=lambda: pytest.fail("legacy fallback after readiness"),
                    broker=broker,
                    enabled=True,
                    session_id=scoped["session_id"],
                    principal_id=scoped["principal_id"],
                    transport_family=scoped["transport_family"],
                    tool_call_id="after-heartbeat-ready",
                )
            )
            command = await ws.receive_json(timeout=2.0)
            assert command["method"] == "browser.controller.command"
            assert command["params"]["action"] == "browser_snapshot"
            await ws.send_json(
                {
                    "method": "browser.controller.result",
                    "params": {
                        "command_id": command["params"]["command_id"],
                        "ok": True,
                        "result": {"ready": True},
                    },
                }
            )
            assert await asyncio.wait_for(pending, timeout=2.0) == '{"ready": true}'
        finally:
            release_attach.set()
            await ws.close()
