"""Actual diagnostic/stdio orchestration with inert transport seams only."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def diagnostics(tmp_path, monkeypatch):
    import tools.mcp_tool as mcp_tool
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mcp_tool, "_mcp_stderr_log_files", {}, raising=False)
    if hasattr(mcp_tool, "_mcp_stderr_log_fh"):
        monkeypatch.setattr(mcp_tool, "_mcp_stderr_log_fh", None)
    token = set_hermes_home_override(str(tmp_path / "profile"))
    try:
        yield mcp_tool
    finally:
        reset_hermes_home_override(token)
        for fh in mcp_tool._mcp_stderr_log_files.values():
            fh.close()
        legacy = getattr(mcp_tool, "_mcp_stderr_log_fh", None)
        if legacy is not None:
            legacy.close()


def test_existing_index_seam_follows_the_current_config_home(diagnostics, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    for name in ("alpha", "beta"):
        token = set_hermes_home_override(str(tmp_path / name))
        try:
            fh = diagnostics._get_mcp_stderr_log()
            fh.write(name + "\n")
            fh.flush()
        finally:
            reset_hermes_home_override(token)
    assert (tmp_path / "alpha" / "logs" / "mcp-stderr.log").read_text() == "alpha\n"
    assert (tmp_path / "beta" / "logs" / "mcp-stderr.log").read_text() == "beta\n"


def test_profile_and_attempt_output_remain_distinct(diagnostics, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    mcp_tool = diagnostics
    records = []
    for home, text in [("alpha", "first"), ("beta", "second"), ("alpha", "third")]:
        token = set_hermes_home_override(str(tmp_path / home))
        try:
            capture = mcp_tool._begin_stdio_diagnostic("cea_graph")
            capture["stream"].write(text + "\n")
            capture["status"] = "closed"
            mcp_tool._finish_stdio_diagnostic(capture)
            assert capture["stream"].closed
            records.append(capture)
        finally:
            reset_hermes_home_override(token)
    assert len({record["stderr_path"] for record in records}) == 3
    assert len({record["attempt_id"] for record in records}) == 3
    for record, text in zip(records, ["first", "second", "third"]):
        path = Path(record["stderr_path"])
        assert path.parent.parent.parent == Path(record["config_home"])
        content = path.read_text()
        assert text + "\n" in content
        assert all(other + "\n" not in content for other in {"first", "second", "third"} - {text})
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600
    for home, expected in [("alpha", 2), ("beta", 1)]:
        index = (tmp_path / home / "logs" / "mcp-stderr.log").read_text().splitlines()
        entries = [json.loads(line) for line in index]
        assert len(entries) == expected
        assert all(entry["config_home"] == str(tmp_path / home) for entry in entries)
    assert mcp_tool._mcp_stdio_diagnostic.get() is None


def test_nested_attempts_restore_context_and_close_only_their_stream(diagnostics):
    mcp_tool = diagnostics
    outer = mcp_tool._begin_stdio_diagnostic("cea_graph")
    inner = mcp_tool._begin_stdio_diagnostic("pilot_bridge")
    inner["status"] = "closed"
    mcp_tool._finish_stdio_diagnostic(inner)
    assert inner["stream"].closed
    assert not outer["stream"].closed
    assert mcp_tool._mcp_stdio_diagnostic.get() is outer
    outer["status"] = "closed"
    mcp_tool._finish_stdio_diagnostic(outer)
    assert mcp_tool._mcp_stdio_diagnostic.get() is None


def test_missing_capture_reports_discarded_without_retaining_error_text(
    diagnostics, monkeypatch, caplog
):
    import builtins

    real_open = builtins.open

    def unavailable(path, *args, **kwargs):
        if str(path) != os.devnull:
            raise OSError("MUST_NOT_RETAIN_SECRET")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unavailable)
    capture = diagnostics._begin_stdio_diagnostic("cea_graph")
    capture["status"] = "closed"
    diagnostics._finish_stdio_diagnostic(capture)
    assert capture["destination"] == "discarded"
    assert "discarded" in caplog.text
    assert "MUST_NOT_RETAIN_SECRET" not in caplog.text
    assert capture["stream"].closed
    assert diagnostics._mcp_stdio_diagnostic.get() is None


@pytest.mark.parametrize("phase", ["negotiate", "discover_tools"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_actual_stdio_failure_preserves_error_and_phase_without_live_io(
    diagnostics, monkeypatch, caplog, phase, error_type
):
    import tools.osv_check

    mcp_tool = diagnostics
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool, "StdioServerParameters", SimpleNamespace, raising=False)
    monkeypatch.setattr(mcp_tool, "_resolve_stdio_command", lambda command, env: (command, env))
    monkeypatch.setattr(mcp_tool, "_wrap_command_with_watchdog", lambda command, args: (command, args))
    monkeypatch.setattr(mcp_tool, "_kill_orphaned_mcp_children", lambda: None)
    monkeypatch.setattr(mcp_tool, "_snapshot_child_pids", lambda: set())
    monkeypatch.setattr(mcp_tool, "_MCP_NOTIFICATION_TYPES", False)
    monkeypatch.setattr(mcp_tool, "_MCP_LOGGING_CALLBACK_SUPPORTED", False)
    monkeypatch.setattr(tools.osv_check, "check_package_for_malware", lambda *_args: None)

    @asynccontextmanager
    async def inert_stdio(_params, *, errlog):
        errlog.write("inert child stderr\n")
        yield None, None

    @asynccontextmanager
    async def inert_session(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(mcp_tool, "stdio_client", inert_stdio, raising=False)
    monkeypatch.setattr(mcp_tool, "ClientSession", inert_session, raising=False)
    server = mcp_tool.MCPServerTask("cea_graph")
    monkeypatch.setattr(mcp_tool, "_servers", {"cea_graph": server})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {})
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: {"cea_graph": {"command": "inert"}})
    original = error_type("MUST_NOT_RETAIN_SECRET")

    async def discover(_server):
        # Exercise get_mcp_status during the actual retained-task window:
        # session assigned, tools not discovered, readiness not yet signalled.
        assert mcp_tool.get_mcp_status()[0]["status"] == "connecting"
        raise original

    monkeypatch.setattr(
        mcp_tool.MCPServerTask,
        "_negotiate_session",
        AsyncMock(
            side_effect=original if phase == "negotiate" else None,
            return_value=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(mcp_tool.MCPServerTask, "_discover_tools", discover)
    with pytest.raises(error_type) as caught:
        asyncio.run(server._run_stdio({"command": "inert-never-executed"}))
    assert caught.value is original
    assert not server._ready.is_set()
    assert mcp_tool._mcp_stdio_diagnostic.get() is None
    records = [json.loads(record.getMessage().split(": ", 1)[1]) for record in caplog.records
               if record.getMessage().startswith("MCP stdio attempt ended: ")]
    assert len(records) == 1
    record = records[0]
    assert record["phase"] == phase
    assert record["status"] == "failed"
    assert record["exception_type"] == error_type.__name__
    assert record["destination"] == "file"
    assert "MUST_NOT_RETAIN_SECRET" not in caplog.text
    assert "MUST_NOT_RETAIN_SECRET" not in Path(record["stderr_path"]).read_text()
    assert "inert child stderr" in Path(record["stderr_path"]).read_text()


def test_actual_run_retry_clears_readiness_before_second_stdio_discovery(diagnostics, monkeypatch):
    import tools.osv_check

    mcp_tool = diagnostics
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool, "StdioServerParameters", SimpleNamespace, raising=False)
    monkeypatch.setattr(mcp_tool, "_resolve_stdio_command", lambda command, env: (command, env))
    monkeypatch.setattr(mcp_tool, "_wrap_command_with_watchdog", lambda command, args: (command, args))
    monkeypatch.setattr(mcp_tool, "_kill_orphaned_mcp_children", lambda: None)
    monkeypatch.setattr(mcp_tool, "_snapshot_child_pids", lambda: set())
    monkeypatch.setattr(mcp_tool, "_MCP_NOTIFICATION_TYPES", False)
    monkeypatch.setattr(mcp_tool, "_MCP_LOGGING_CALLBACK_SUPPORTED", False)
    monkeypatch.setattr(tools.osv_check, "check_package_for_malware", lambda *_args: None)
    monkeypatch.setattr(mcp_tool.asyncio, "sleep", AsyncMock())

    @asynccontextmanager
    async def inert_stdio(_params, *, errlog):
        errlog.write("inert retry stderr\n")
        yield None, None

    @asynccontextmanager
    async def inert_session(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(mcp_tool, "stdio_client", inert_stdio, raising=False)
    monkeypatch.setattr(mcp_tool, "ClientSession", inert_session, raising=False)
    monkeypatch.setattr(mcp_tool.MCPServerTask, "_negotiate_session", AsyncMock(return_value=SimpleNamespace()))
    server = mcp_tool.MCPServerTask("cea_graph")
    monkeypatch.setattr(mcp_tool, "_servers", {"cea_graph": server})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    monkeypatch.setattr(mcp_tool, "_server_connect_errors", {})
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: {"cea_graph": {"command": "inert"}})
    statuses = []

    async def discover(_server):
        statuses.append(mcp_tool.get_mcp_status()[0]["status"])
        if len(statuses) == 2:
            server._shutdown_event.set()

    async def lifecycle(_server):
        if len(statuses) == 1:
            raise BrokenPipeError("inert transient failure after first readiness")
        return "shutdown"

    monkeypatch.setattr(mcp_tool.MCPServerTask, "_discover_tools", discover)
    monkeypatch.setattr(mcp_tool.MCPServerTask, "_wait_for_lifecycle_event", lifecycle)
    asyncio.run(server.run({"command": "inert-never-executed", "sampling": {"enabled": False},
                           "elicitation": {"enabled": False}}))
    assert statuses == ["connecting", "connecting"]
    assert server._ever_connected
