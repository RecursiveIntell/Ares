"""Status boundaries only; no MCP processes, RPCs or provider calls."""

import pytest


@pytest.fixture
def status_runtime(monkeypatch):
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {})
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"cea_graph": {"command": "inert"}}
    )
    server = mcp_tool.MCPServerTask("cea_graph")
    mcp_tool._servers["cea_graph"] = server
    return mcp_tool, server


def test_session_before_discovery_is_connecting(status_runtime):
    mcp_tool, server = status_runtime
    # The actual stdio path assigns this before awaiting discovery. A retained
    # task can be reconnecting without appearing in _server_connecting.
    server.session = object()
    status = mcp_tool.get_mcp_status()[0]
    assert status["status"] == "connecting"
    assert status["connected"] is False
    assert status["tools"] == 0


def test_ready_session_with_no_tools_is_still_connected(status_runtime):
    mcp_tool, server = status_runtime
    server.session = object()
    server._ready.set()
    # Resource/prompt-only servers legitimately have zero tool definitions.
    status = mcp_tool.get_mcp_status()[0]
    assert status["status"] == "connected"
    assert status["connected"] is True
    assert status["tools"] == 0


def test_ready_failure_is_not_connection_success(status_runtime):
    mcp_tool, server = status_runtime
    server.session = object()
    server._ready.set()  # start() also wakes its waiter on failure.
    server._error = RuntimeError("Connection closed")
    status = mcp_tool.get_mcp_status()[0]
    assert status["status"] == "failed"
    assert status["connected"] is False
    assert "Connection closed" in status["error"]


def test_retained_failure_reports_task_error_without_discovery_map(status_runtime):
    mcp_tool, server = status_runtime
    server._error = RuntimeError("Connection closed")
    status = mcp_tool.get_mcp_status()[0]
    assert status["status"] == "failed"
    assert status["connected"] is False


def test_recovered_ready_session_overrides_old_discovery_error(status_runtime):
    mcp_tool, server = status_runtime
    server.session = object()
    server._ready.set()
    mcp_tool._server_connect_errors["cea_graph"] = "older attempt failed"
    assert mcp_tool.get_mcp_status()[0]["status"] == "connected"


def test_ready_event_without_session_is_not_connected(status_runtime):
    mcp_tool, server = status_runtime
    server._ready.set()
    assert mcp_tool.get_mcp_status()[0]["connected"] is False


def test_disabled_unstarted_server_keeps_disabled_status(status_runtime, monkeypatch):
    mcp_tool, _server = status_runtime
    mcp_tool._servers.clear()
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config",
        lambda: {"cea_graph": {"command": "inert", "enabled": False}},
    )
    assert mcp_tool.get_mcp_status()[0]["status"] == "disabled"
