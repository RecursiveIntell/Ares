"""Behavioral budgets and quality receipts for optional LSP enrichment."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from agent.lsp.client import LSPClient
from agent.lsp.manager import LSPService
from agent.lsp.servers import SERVERS, ServerDef, SpawnSpec
from agent.lsp.workspace import clear_cache, resolve_workspace_for_file

MOCK_SERVER = str(Path(__file__).with_name("_mock_lsp_server.py"))


def test_background_submission_seam(caplog):
    from agent.lsp.manager import _BackgroundLoop
    caplog.set_level("DEBUG", logger="asyncio")
    loop = _BackgroundLoop()
    loop.start()
    loop._loop.set_debug(True)

    async def answer():
        return 42

    try:
        assert loop.run(answer(), timeout=2) == 42
    except TimeoutError:
        import traceback
        if loop._thread is not None:
            frame = sys._current_frames().get(loop._thread.ident)
            if frame is not None:
                print("".join(traceback.format_stack(frame)))
        raise
    finally:
        loop.stop()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    clear_cache()
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("")
    monkeypatch.chdir(root)
    yield root
    clear_cache()


def service(**kwargs):
    return LSPService(enabled=True, wait_mode="document", wait_timeout=0.3,
                      install_strategy="manual", idle_timeout=0, **kwargs)


def install_server(monkeypatch, script="errors", command=None, resolve=None):
    index = next(i for i, srv in enumerate(SERVERS) if srv.server_id == "pyright")
    calls = []

    def spawn(root, ctx):
        calls.append(root)
        return SpawnSpec(command=command or [sys.executable, MOCK_SERVER],
                         workspace_root=root, cwd=root,
                         env={"MOCK_LSP_SCRIPT": script})

    replacement = ServerDef(
        server_id="pyright", extensions=(".py",),
        resolve_root=resolve or (lambda fp, ws: ws), build_spawn=spawn)
    monkeypatch.setattr("agent.lsp.servers.SERVERS",
                        [replacement if i == index else srv for i, srv in enumerate(SERVERS)])
    return calls


def test_baseline_uses_configured_document_budget(repo, monkeypatch):
    install_server(monkeypatch)
    source = repo / "x.py"
    source.write_text("x = 1\n")
    budgets = []
    original = LSPClient.wait_for_diagnostics

    async def observe(self, *args, **kwargs):
        budgets.append(kwargs.get("timeout"))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(LSPClient, "wait_for_diagnostics", observe)
    svc = service()
    try:
        svc.snapshot_baseline(str(source))
        assert budgets == [svc._wait_timeout]
    finally:
        svc.shutdown()


@pytest.mark.parametrize("script,quality", [("errors", "complete"), ("clean", "complete"),
                                            ("crash", None)])
def test_quality_and_positive_findings(repo, monkeypatch, script, quality, record_property):
    install_server(monkeypatch, script)
    source = repo / "x.py"
    source.write_text("x = 1\n")
    svc = service()
    try:
        result = svc.get_enrichment_sync(str(source), delta=False)
        if quality is None:
            assert result.quality in {"unavailable", "degraded"}
        else:
            assert result.quality == quality
        assert bool(result.diagnostics) == (script == "errors")
        assert set(result.phases) == {"initialize", "post_edit"}
        assert all(span >= 0 for span in result.phases.values())
        record_property("quality", result.quality)
        record_property("phase_spans", result.phases)
        print(f"{script}: quality={result.quality} phases={result.phases}")
    finally:
        svc.shutdown()


def test_stale_result_is_degraded_and_baseline_is_not_reused(repo, monkeypatch):
    install_server(monkeypatch, "stale")
    source = repo / "x.py"
    source.write_text("x = 1\n")
    svc = service()
    try:
        first = svc.get_enrichment_sync(str(source), delta=False)
        assert first.quality == "complete" and first.diagnostics
        source.write_text("x = 2\n")
        stale = svc.get_enrichment_sync(str(source))
        assert stale.quality == "degraded" and stale.reason == "fresh_diagnostics_timeout"
        assert stale.diagnostics == []
        assert svc.enabled_for(str(source))
        print(f"stale: phases={stale.phases}")
    finally:
        svc.shutdown()


def test_cold_timeout_cleans_process_and_repeat_is_cached(repo, monkeypatch, record_property):
    from agent.lsp import manager
    monkeypatch.setattr(manager, "ENRICHMENT_INIT_BUDGET", 0.15, raising=False)
    calls = install_server(monkeypatch, "slow")
    source = repo / "x.py"
    source.write_text("x = 1\n")
    clients = []
    original = LSPClient.start

    async def observe(self):
        clients.append(self)
        await original(self)

    monkeypatch.setattr(LSPClient, "start", observe)
    svc = service()
    try:
        started = time.monotonic()
        first = svc.snapshot_baseline(str(source))
        cold = time.monotonic() - started
        assert first.quality == "unavailable"
        assert clients and clients[0]._proc is None
        started = time.monotonic()
        repeat = svc.get_enrichment_sync(str(source))
        repeat_span = time.monotonic() - started
        assert repeat.quality == "unavailable"
        assert len(calls) == 1
        assert cold < 2 and repeat_span < 2
        record_property("cold_seconds", cold)
        record_property("repeat_seconds", repeat_span)
        record_property("phase_spans", first.phases)
        print(f"cold={cold:.6f}s repeat={repeat_span:.6f}s phases={first.phases}")
    finally:
        svc.shutdown()


def executable(path, script):
    path.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{MOCK_SERVER}'\n# {script}\n")
    path.chmod(0o755)


def test_missing_executable_recovers_without_restart(repo, monkeypatch, record_property):
    binary = repo / "mock-server"
    calls = install_server(monkeypatch, command=[str(binary)])
    source = repo / "x.py"
    source.write_text("x = 1\n")
    svc = service(binary_overrides={"pyright": [str(binary)]})
    try:
        assert svc.get_diagnostics_sync(str(source)) == []
        assert not svc.enabled_for(str(source))
        executable(binary, "recovered")
        assert svc.enabled_for(str(source)), "a recovered executable must invalidate failure cache"
        fresh = svc.get_enrichment_sync(str(source))
        assert fresh.quality == "complete" and fresh.diagnostics
        assert len(calls) == 2
        record_property("recovered_quality", fresh.quality)
        record_property("recovered_findings", len(fresh.diagnostics))
        record_property("phase_spans", fresh.phases)
    finally:
        svc.shutdown()


def test_replaced_executable_does_not_reuse_live_client_or_baseline(repo, monkeypatch):
    binary = repo / "mock-server"
    executable(binary, "first")
    calls = install_server(monkeypatch, command=[str(binary)])
    source = repo / "x.py"
    source.write_text("x = 1\n")
    svc = service(binary_overrides={"pyright": [str(binary)]})
    try:
        svc.snapshot_baseline(str(source))
        first = next(iter(svc._clients.values()))
        replacement = repo / "replacement"
        executable(replacement, "second")
        os.replace(replacement, binary)
        result = svc.get_enrichment_sync(str(source))
        assert result.quality == "complete" and result.diagnostics
        assert next(iter(svc._clients.values())) is not first
        assert len(calls) == 2
    finally:
        svc.shutdown()


def test_workspace_marker_removal_and_nearer_root_are_observed(repo, monkeypatch):
    from agent.lsp.workspace import find_git_worktree
    enclosing = find_git_worktree(str(repo.parent))
    source = repo / "pkg" / "x.py"
    source.parent.mkdir()
    source.write_text("x = 1\n")
    assert resolve_workspace_for_file(str(source)) == (str(repo), True)
    (source.parent / ".git").mkdir()
    monkeypatch.chdir(source.parent)
    assert resolve_workspace_for_file(str(source)) == (str(source.parent), True)
    (source.parent / ".git").rmdir()
    (repo / ".git").rmdir()
    assert resolve_workspace_for_file(str(source)) == (enclosing, enclosing is not None)


def test_changed_project_root_does_not_filter_new_server_findings(repo, monkeypatch):
    from agent.lsp.workspace import nearest_root
    calls = install_server(monkeypatch, resolve=lambda fp, ws: nearest_root(
        fp, ["pyproject.toml"], ceiling=ws) or ws)
    source = repo / "pkg" / "x.py"
    source.parent.mkdir()
    source.write_text("x = 1\n")
    svc = service()
    try:
        svc.snapshot_baseline(str(source))
        (source.parent / "pyproject.toml").write_text("")
        result = svc.get_enrichment_sync(str(source))
        assert result.quality == "complete" and result.diagnostics
        assert calls == [str(repo), str(source.parent)]
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_cancelled_initialize_reaps_real_child(tmp_path, monkeypatch, record_property):
    client = LSPClient(server_id="mock", workspace_root=str(tmp_path),
                       command=[sys.executable, MOCK_SERVER], cwd=str(tmp_path),
                       env={"MOCK_LSP_SCRIPT": "slow"})
    entered = asyncio.Event()
    original = client._initialize

    async def initialize():
        entered.set()
        await original()

    monkeypatch.setattr(client, "_initialize", initialize)
    task = asyncio.create_task(client.start())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        proc = client._proc
        assert proc is not None
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.15)
        record_property("initialize_cancel_seconds", time.monotonic() - started)
        assert client._proc is None
        assert proc.returncode is not None, "cancelled initialization must reap the OS child"
        assert client.state == "error"
        assert client._reader_task is None and client._stderr_task is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.shutdown()


@pytest.mark.asyncio
async def test_cancelled_diagnostics_wait_joins_child_tasks(tmp_path, monkeypatch):
    source = tmp_path / "x.py"
    source.write_text("x = 1\n")
    client = LSPClient(server_id="mock", workspace_root=str(tmp_path),
                       command=[sys.executable, MOCK_SERVER], cwd=str(tmp_path),
                       env={"MOCK_LSP_SCRIPT": "stale"})
    await client.start()
    try:
        version = await client.open_file(str(source), language_id="python")
        assert await client.wait_for_diagnostics(str(source), version, timeout=2)
        source.write_text("x = 2\n")
        version = await client.open_file(str(source), language_id="python")
        entered, finished = asyncio.Event(), asyncio.Event()

        async def stalled_pull(_path):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        monkeypatch.setattr(client, "_pull_document_diagnostics", stalled_pull)
        waiter = asyncio.create_task(client.wait_for_diagnostics(str(source), version, timeout=2))
        await asyncio.wait_for(entered.wait(), timeout=2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert finished.is_set(), "a cancelled parent must join its diagnostic pull"
    finally:
        await client.shutdown()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "invalid", 10000])
def test_wait_budget_is_finite_even_with_invalid_config(value):
    svc = LSPService(enabled=False, wait_mode="document", wait_timeout=value,
                     install_strategy="manual", idle_timeout=0)
    from agent.lsp.manager import ENRICHMENT_MAX_WAIT
    assert 0 < svc._wait_timeout <= ENRICHMENT_MAX_WAIT
    result = svc.get_enrichment_sync("outside.py")
    assert result.quality == "unavailable" and result.diagnostics == []


def test_optional_failure_cannot_waive_python_syntax_check(tmp_path, monkeypatch):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    calls = []

    def unavailable():
        calls.append(True)
        raise RuntimeError("optional server unavailable")

    monkeypatch.setattr("agent.lsp.get_service", unavailable)
    target = tmp_path / "bad.py"
    target.write_text("x = 1\n")
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    result = fops.write_file(str(target), "def invalid(:\n")
    assert result.lint["status"] == "error"
    assert result.lsp_diagnostics is None
    assert calls == [True], "baseline failure must not waive post-write syntax reporting"


@pytest.mark.parametrize("suffix,valid,invalid", [
    (".json", '{"ok": true}', '{"ok":'),
    (".yaml", "ok: true\n", "ok: [\n"),
    (".toml", "ok = true\n", "ok = [\n"),
])
def test_optional_failure_cannot_waive_structured_syntax_gate(
    tmp_path, monkeypatch, suffix, valid, invalid,
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    def unavailable():
        raise AssertionError("the mandatory rejection must precede enrichment")

    monkeypatch.setattr("agent.lsp.get_service", unavailable)
    target = tmp_path / ("bad" + suffix)
    target.write_text(valid)
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    result = fops.write_file(str(target), invalid)
    assert result.error and "syntax validation" in result.error
    assert target.read_text() == valid


def test_config_signature_replaces_only_changed_profile_service(monkeypatch):
    import agent.lsp as lsp
    from unittest.mock import Mock

    config = {"lsp": {"enabled": True, "wait_timeout": 1}}
    created = []

    def create(cls, **kwargs):
        svc = Mock()
        svc.is_active.return_value = True
        created.append(svc)
        return svc

    monkeypatch.setattr(lsp, "_services", {})
    monkeypatch.setattr(lsp, "_atexit_registered", True)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(LSPService, "create_from_config", classmethod(create))
    try:
        first = lsp.get_service()
        assert lsp.get_service() is first
        config["lsp"]["wait_timeout"] = 2
        second = lsp.get_service()
        assert second is not first and len(created) == 2
        first.shutdown.assert_called_once()
        assert lsp.get_service() is second
    finally:
        lsp.shutdown_service()
