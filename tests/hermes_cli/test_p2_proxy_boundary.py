"""NET-05 real aiohttp callers; inert upstream and credential effect counters.

Listeners are admitted only inside the controller's isolated network namespace.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp import web

from hermes_cli.proxy import cli, server
from tests.hermes_cli.test_proxy import FakeAdapter, _start_runner

CLIENT = "synthetic-client-credential-" + "x" * 32


def test_cli_bind_refusal_precedes_adapter(monkeypatch):
    adapter_factory = Mock(side_effect=AssertionError("must not resolve adapter"))
    monkeypatch.setattr(cli, "get_adapter", adapter_factory)
    assert cli.cmd_proxy_start(SimpleNamespace(host="0.0.0.0", port=0, provider="nous")) == 2
    adapter_factory.assert_not_called()


def test_server_bind_refusal_precedes_setup(monkeypatch):
    setup = Mock(side_effect=AssertionError("must not listen"))
    monkeypatch.setattr(server.web, "AppRunner", setup)
    adapter = FakeAdapter("http://unused.invalid/v1")
    with pytest.raises(ValueError, match="Nonloopback"):
        asyncio.run(server.run_server(adapter, host="::", port=0))
    assert adapter.calls == 0
    setup.assert_not_called()


def test_client_auth_origin_paths_redirect_and_distinct_credentials():
    async def run():
        upstream_effects = []
        async def upstream(request):
            upstream_effects.append((request.path, request.headers.get("Authorization")))
            if request.path.endswith("redirect"):
                return web.Response(status=302, headers={"Location": "/sentinel"})
            return web.json_response({"ok": True})
        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream)
        upstream_runner, upstream_url = await _start_runner(upstream_app)
        adapter = FakeAdapter(upstream_url + "/v1", bearer="synthetic-upstream-only",
                              allowed=["/chat/completions", "/redirect"])
        proxy_runner, proxy_url = await _start_runner(server.create_app(adapter, client_token=CLIENT))
        try:
            async with aiohttp.ClientSession() as client:
                for headers in [{}, {"Authorization": "Bearer wrong"},
                    {"Authorization": "Bearer " + CLIENT, "Origin": "null"},
                    {"Authorization": "Bearer " + CLIENT, "Origin": "http://127.0.0.1:9"},
                    {"Authorization": "Bearer " + CLIENT, "Host": "evil.test"}]:
                    async with client.post(proxy_url + "/v1/chat/completions", headers=headers) as response:
                        assert response.status in {401, 403}
                        await response.read()
                assert adapter.calls == 0
                assert upstream_effects == []
                valid = {"Authorization": "Bearer " + CLIENT}
                async with client.post(proxy_url + "/v1/disallowed", headers=valid) as response:
                    assert response.status == 404
                assert adapter.calls == 0
                async with client.options(proxy_url + "/v1/chat/completions",
                    headers={**valid, "Origin": proxy_url, "Access-Control-Request-Method": "POST"}) as response:
                    assert response.status == 204
                    assert "Access-Control-Allow-Origin" not in response.headers
                assert adapter.calls == 0
                async with client.post(proxy_url + "/v1/chat/completions", headers=valid) as response:
                    assert response.status == 200
                    await response.read()
                assert upstream_effects == [("/v1/chat/completions", "Bearer synthetic-upstream-only")]
                async with client.post(proxy_url + "/v1/redirect", headers=valid) as response:
                    assert response.status == 502
                    assert "Location" not in response.headers
                assert len(upstream_effects) == 2
                adapter._bearer = CLIENT
                async with client.post(proxy_url + "/v1/chat/completions", headers=valid) as response:
                    assert response.status == 403
                assert len(upstream_effects) == 2
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()
    asyncio.run(run())


def test_authenticated_nonloopback_bind_real_listener(monkeypatch):
    async def run():
        started = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        original_site = server.web.TCPSite
        class ObservedSite(original_site):
            async def start(self):
                await super().start()
                started.set_result(self._server.sockets[0].getsockname()[1])
        monkeypatch.setattr(server.web, "TCPSite", ObservedSite)
        adapter = FakeAdapter("http://unused.invalid/v1")
        adapter.is_authenticated = Mock(return_value=True)
        task = asyncio.create_task(server.run_server(adapter, host="0.0.0.0", port=0,
            client_token=CLIENT, shutdown_event=stop))
        try:
            port = await asyncio.wait_for(started, 5)
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{port}/health") as response:
                    assert response.status == 401
                adapter.is_authenticated.assert_not_called()
                async with client.get(f"http://127.0.0.1:{port}/health",
                    headers={"Authorization": "Bearer " + CLIENT}) as response:
                    assert response.status == 200
                adapter.is_authenticated.assert_called_once()
                assert adapter.calls == 0
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
    asyncio.run(run())
