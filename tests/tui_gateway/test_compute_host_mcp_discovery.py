"""Compute-host agent builds must start MCP discovery before snapshotting tools.

Desktop turns run in the ``tui_gateway.compute_host`` child process, which
builds session agents through ``ComputeHost._ensure_server_session`` →
``server._make_agent``. The agent snapshots its tool list once at build time.
The in-process build path (``server._start_agent_build``) calls
``tui_gateway.entry.ensure_mcp_discovery_started()`` first, and the dashboard
and CLI start discovery at process startup — but the compute host is a
separate process that ran neither, so its agents only saw MCP tools when some
unrelated path happened to start discovery in that process first.
"""

from __future__ import annotations

import pytest

from tui_gateway import entry, server
from tui_gateway.compute_host import ComputeHost


class _Agent:
    session_id = "host-session"


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, **kw: server._sessions.__setitem__(sid, {"agent": agent, "session_key": key}))

    class _DB:
        def get_session(self, key):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    instance = ComputeHost(heartbeat_secs=0)
    yield instance
    instance.close()


def test_host_session_build_starts_discovery_before_agent_snapshot(host, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: order.append("discovery"))

    def _make_agent(sid, key, **kwargs):
        order.append("make_agent")
        return _Agent()

    monkeypatch.setattr(server, "_make_agent", _make_agent)

    session = host._ensure_server_session(server, {"sid": "s1", "session_key": "k1"})

    assert session["agent"].session_id == "host-session"
    assert order == ["discovery", "make_agent"]


def test_discovery_start_failure_never_blocks_the_build(host, monkeypatch):
    def _boom():
        raise RuntimeError("mcp sdk unavailable")

    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", _boom)
    built = []
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, **kw: built.append(sid) or _Agent())

    host._ensure_server_session(server, {"sid": "s2", "session_key": "k2"})

    assert built == ["s2"]


def test_existing_host_session_is_reused_without_new_discovery(host, monkeypatch):
    calls = []
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: calls.append("discovery"))
    existing = {"agent": _Agent(), "session_key": "k3"}
    server._sessions["s3"] = existing

    assert host._ensure_server_session(server, {"sid": "s3", "session_key": "k3"}) is existing
    assert calls == []
