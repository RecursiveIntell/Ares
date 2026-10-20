"""Exact authority grammar and numeric-loopback listener regression gates."""
import asyncio
from types import SimpleNamespace

import pytest
from hermes_cli.dashboard_auth.origins import exact_origin
from hermes_cli.proxy import server


@pytest.mark.parametrize("raw", ["http://[::1]junk", "http://[::1]junk:80", "http://[::1]:80junk"])
def test_ipv6_authority_suffix_is_rejected(raw):
    assert exact_origin(raw) is None


def test_valid_ipv6_and_default_port_are_preserved():
    assert exact_origin("http://[::1]") == exact_origin("http://[::1]:80")
    assert exact_origin("https://[::1]:443") == ("https", "::1", 443)


def test_localhost_alias_is_numeric_before_listener_creation(monkeypatch):
    seen = []
    async def exercise():
        done = asyncio.Event()
        class Runner:
            async def setup(self):
                pass
            async def cleanup(self):
                pass
        class Site:
            async def start(self):
                done.set()
        monkeypatch.setattr(server, "create_app", lambda *a, **kw: object())
        monkeypatch.setattr(server.web, "AppRunner", lambda *a, **kw: Runner())
        def make_site(runner, *, host, port):
            seen.append(host)
            return Site()
        monkeypatch.setattr(server.web, "TCPSite", make_site)
        await server.run_server(SimpleNamespace(display_name="inert"), host="localhost", port=8645, shutdown_event=done)
    asyncio.run(exercise())
    assert seen == ["127.0.0.1"]
