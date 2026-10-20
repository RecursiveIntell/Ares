"""Actual listener observation for the credential-free localhost alias."""
import asyncio
from types import SimpleNamespace

import httpx
from hermes_cli.proxy import server


def test_localhost_alias_listens_only_on_numeric_loopback_without_dns_authority(monkeypatch):
    async def exercise():
        done = asyncio.Event()
        bound = []
        actual = server.web.TCPSite
        loop = asyncio.get_running_loop()
        resolve = loop.getaddrinfo
        resolver_inputs = []
        async def observe_resolution(host, *args, **kwargs):
            resolver_inputs.append(host)
            return await resolve(host, *args, **kwargs)
        monkeypatch.setattr(loop, "getaddrinfo", observe_resolution)
        class Site(actual):
            async def start(self):
                await super().start()
                sockets = self._server.sockets
                bound.extend(sock.getsockname() for sock in sockets)
                assert bound and all(row[0] == "127.0.0.1" for row in bound)
                assert "localhost" not in resolver_inputs
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(f"http://127.0.0.1:{bound[0][1]}/health")
                    assert response.status_code == 200
                done.set()
        monkeypatch.setattr(server.web, "TCPSite", Site)
        calls = []
        adapter = SimpleNamespace(display_name="inert", is_authenticated=lambda: calls.append("health") or True)
        await asyncio.wait_for(server.run_server(adapter, host="localhost", port=0, shutdown_event=done), timeout=10)
        assert calls == ["health"]
        assert bound and all(row[0] == "127.0.0.1" for row in bound)
    asyncio.run(exercise())
