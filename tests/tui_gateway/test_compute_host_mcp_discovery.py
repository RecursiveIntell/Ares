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


def test_real_init_session_still_schedules_late_refresh_after_discovery(monkeypatch):
    """Drive the REAL server._init_session on the compute-host path.

    Only its I/O side effects are stubbed; the late-refresh scheduler is
    replaced with a recorder so the test witnesses the actual call order:
    discovery start → agent build → late refresh for that same agent.
    """
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)

    class _DB:
        def get_session(self, key):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    for name in ("_register_session_cwd", "_wire_callbacks", "_notify_session_boundary", "_emit"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_info", lambda agent, session, **kw: {})
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "off")
    monkeypatch.setattr(server, "_load_memory_notifications", lambda: "off")

    order: list[tuple[str, object]] = []
    built = _Agent()
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: order.append(("discovery", None)))
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, **kw: order.append(("make_agent", None)) or built)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda sid, agent: order.append(("late_refresh", agent)))

    instance = ComputeHost(heartbeat_secs=0)
    try:
        session = instance._ensure_server_session(server, {"sid": "s4", "session_key": "k4"})
    finally:
        instance.close()

    assert session["agent"] is built
    assert [step for step, _ in order] == ["discovery", "make_agent", "late_refresh"]
    assert order[-1][1] is built
