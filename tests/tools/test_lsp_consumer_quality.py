"""File edits retain optional enrichment quality without waiving syntax checks.

Use actual fileops/service APIs and temp disk writes. The service's synchronous
transport boundary is dependency-injected; no language server or background
loop is started, and these tests do not qualify production transport timing.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent.lsp.manager import EnrichmentResult, LSPService
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


def diagnostic(message="type mismatch", line=0):
    return {"severity": 1, "message": message, "source": "test", "code": "E1",
            "range": {"start": {"line": line, "character": 2},
                      "end": {"line": line, "character": 3}}}


def receipt(quality="complete", diagnostics=None, phase="post_edit"):
    return EnrichmentResult(diagnostics=diagnostics or [], quality=quality,
                            reason="fresh" if quality == "complete" else "server_error",
                            phases={"initialize": 0.01, phase: 0.02})


@pytest.fixture
def ops(tmp_path, monkeypatch):
    # Never touch the profile-owned singleton or launch real optional services.
    monkeypatch.setattr("agent.lsp.get_service", lambda: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("")
    return ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))


def inject_transport(monkeypatch, before, after):
    service = LSPService(enabled=False, wait_mode="document", wait_timeout=0.1,
                         idle_timeout=0, install_strategy="manual")
    responses = iter([before, after])
    calls = []

    def run(coro, *, timeout):
        # Transport DI only: do not execute or reschedule this coroutine.
        coro.close()
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    monkeypatch.setattr(service._loop, "run", run)
    monkeypatch.setattr(service, "enabled_for", lambda path: True)
    original = service.get_enrichment_sync

    def observe(path, **kwargs):
        calls.append(kwargs)
        return original(path, **kwargs)

    monkeypatch.setattr(service, "get_enrichment_sync", observe)
    monkeypatch.setattr("agent.lsp.get_service", lambda: service)
    return service, calls


@pytest.mark.parametrize("quality", ["complete", "degraded", "unavailable"])
@pytest.mark.parametrize("nonempty", [False, True])
def test_quality_survives_empty_and_nonempty_results(ops, tmp_path, monkeypatch, quality, nonempty):
    _, calls = inject_transport(monkeypatch, receipt(phase="baseline"),
                                receipt(quality, [diagnostic()] if nonempty else []))
    path = tmp_path / "x.py"
    result = ops.write_file(str(path), "x = 1\n").to_dict()
    assert not result.get("error")
    assert path.read_text() == "x = 1\n"
    assert result["lint"]["status"] == "ok"
    info = result["lsp_enrichment"]
    assert info["quality"] == quality
    assert info["reason"] == ("fresh" if quality == "complete" else "server_error")
    assert info["phases"]["post_edit"] == 0.02
    assert info["baseline"]["quality"] == "complete"
    assert calls[0]["delta"] is True
    if nonempty:
        assert "ERROR [1:3] type mismatch [E1] (test)" in result["lsp_diagnostics"]
    else:
        assert "lsp_diagnostics" not in result


@pytest.mark.parametrize("quality", ["degraded", "unavailable"])
def test_failed_baseline_then_fresh_post_is_current_not_introduced(ops, tmp_path, monkeypatch, quality):
    _, calls = inject_transport(monkeypatch, receipt(quality, phase="baseline"),
                                receipt(diagnostics=[diagnostic("existing problem")]))
    path = tmp_path / "x.py"
    path.write_text("x = 0\n")
    result = ops.write_file(str(path), "x = 1\n").to_dict()
    assert result["lsp_enrichment"]["quality"] == "complete"
    assert result["lsp_enrichment"]["baseline"]["quality"] == quality
    assert result["lsp_enrichment"]["diagnostic_scope"] == "current"
    assert "existing problem" in result["lsp_diagnostics"]
    assert "introduced" not in result["lsp_diagnostics"]
    assert calls[0]["delta"] is False


def test_delta_remaps_shifted_old_findings_and_preserves_new_findings(ops, tmp_path, monkeypatch):
    old = diagnostic("old error", line=1)
    _, calls = inject_transport(monkeypatch, receipt(diagnostics=[old], phase="baseline"),
                                receipt(diagnostics=[diagnostic("old error", line=2),
                                                     diagnostic("new error", line=0)]))
    path = tmp_path / "x.py"
    path.write_text("x = 0\ny = 1\n")
    result = ops.write_file(str(path), "# inserted\nx = 0\ny = 1\n").to_dict()
    assert "old error" not in result["lsp_diagnostics"]
    assert "new error" in result["lsp_diagnostics"]
    assert "introduced by this edit" in result["lsp_diagnostics"]
    assert result["lsp_enrichment"]["diagnostic_scope"] == "introduced"
    assert calls[0]["line_shift"] is not None


@pytest.mark.parametrize("failure", ["none", "disabled", "acquire", "enabled", "baseline", "post"])
def test_optional_service_failure_preserves_write_and_is_unavailable(ops, tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("optional server failed")

    if failure in {"baseline", "post"}:
        svc, _ = inject_transport(monkeypatch, receipt(phase="baseline"), receipt())
        monkeypatch.setattr(svc, "snapshot_baseline" if failure == "baseline" else "get_enrichment_sync", fail)
    elif failure == "acquire":
        monkeypatch.setattr("agent.lsp.get_service", fail)
    elif failure in {"disabled", "enabled"}:
        svc = SimpleNamespace(enabled_for=fail if failure == "enabled" else lambda path: False)
        monkeypatch.setattr("agent.lsp.get_service", lambda: svc)
    path = tmp_path / "x.py"
    result = ops.write_file(str(path), "x = 1\n").to_dict()
    assert not result.get("error")
    assert path.read_text() == "x = 1\n"
    assert result["lint"]["status"] == "ok"  # real independent Python parser
    info = result["lsp_enrichment"]
    if failure == "baseline":
        assert info["baseline"]["quality"] == "unavailable"
        assert info["diagnostic_scope"] == "current"
    else:
        assert info["quality"] == "unavailable"
    assert "lsp_diagnostics" not in result


@pytest.mark.parametrize("suffix,bad", [(".json", '{"x":'), (".yaml", 'x: [unterminated'),
                                        (".toml", '[section\nx = 1')])
@pytest.mark.parametrize("exists", [False, True])
def test_structured_syntax_refusal_precedes_optional_enrichment(ops, tmp_path, monkeypatch, suffix, bad, exists):
    def forbidden():
        pytest.fail("LSP must not be consulted for a refused candidate")
    monkeypatch.setattr("agent.lsp.get_service", forbidden)
    path = tmp_path / ("bad" + suffix)
    if exists:
        path.write_text("# original\n")
    result = ops.write_file(str(path), bad).to_dict()
    assert "Refusing to write" in result["error"]
    assert path.read_text() == "# original\n" if exists else not path.exists()


def test_python_syntax_remains_postwrite_and_blocks_semantic_verdict(ops, tmp_path, monkeypatch):
    _, calls = inject_transport(monkeypatch, receipt(phase="baseline"), receipt())
    path = tmp_path / "bad.py"
    result = ops.write_file(str(path), "def broken(:\n").to_dict()
    assert not result.get("error")
    assert path.read_text() == "def broken(:\n"
    assert result["lint"]["status"] == "error"
    assert not calls
    assert result["lsp_enrichment"]["quality"] == "unavailable"
    assert result["lsp_enrichment"]["reason"] == "syntax_failed"


@pytest.mark.parametrize("ext", [".ts", ".go", ".rs"])
def test_potential_lsp_availability_never_waives_shell_lint(ops, tmp_path, monkeypatch, ext):
    monkeypatch.setattr(ops, "_lsp_will_handle", lambda path: True)
    monkeypatch.setattr(ops, "_has_command", lambda cmd: True)
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(exit_code=1, stdout="Syntax error from required checker")
    monkeypatch.setattr(ops, "_exec", execute)
    result = ops._check_lint(str(tmp_path / ("bad" + ext)))
    assert commands
    assert not result.skipped and not result.success
    assert "Syntax error" in result.output


@pytest.mark.parametrize("mode", ["replace", "v4a"])
def test_patch_receipt_propagates_with_empty_diagnostics(ops, tmp_path, monkeypatch, mode):
    inject_transport(monkeypatch, receipt(phase="baseline"), receipt("degraded"))
    path = tmp_path / "x.py"
    path.write_text("x = 0\n")
    if mode == "replace":
        result = ops.patch_replace(str(path), "x = 0", "x = 1").to_dict()
        info = result["lsp_enrichment"]
    else:
        result = ops.patch_v4a(f"*** Begin Patch\n*** Update File: {path}\n@@\n-x = 0\n+x = 1\n*** End Patch").to_dict()
        info = result["lsp_enrichment"][str(path)]
    assert result["success"]
    assert path.read_text() == "x = 1\n"
    assert info["quality"] == "degraded"
    assert info["phases"]["post_edit"] == 0.02
    assert "lsp_diagnostics" not in result


def test_v4a_add_multi_file_and_partial_failure_keep_per_file_receipts(ops, tmp_path, monkeypatch):
    class Service:
        def enabled_for(self, path):
            return True
        def snapshot_baseline(self, path):
            return receipt("unavailable", phase="baseline")
        def get_enrichment_sync(self, path, **kwargs):
            return receipt("degraded" if path.endswith("b.py") else "complete", [diagnostic()])
    monkeypatch.setattr("agent.lsp.get_service", lambda: Service())
    a, b, c = (tmp_path / name for name in ("a.py", "b.py", "c.json"))
    patch = (f"*** Begin Patch\n*** Add File: {a}\n+x = 1\n"
             f"*** Add File: {b}\n+y = 2\n*** Add File: {c}\n+{{broken\n*** End Patch")
    result = ops.patch_v4a(patch).to_dict()
    assert not result["success"] and result.get("error")
    assert a.read_text() == "x = 1" and b.read_text() == "y = 2"
    assert not c.exists()
    assert result["lsp_enrichment"][str(a)]["quality"] == "complete"
    assert result["lsp_enrichment"][str(b)]["quality"] == "degraded"
    assert "type mismatch" in result["lsp_diagnostics"]
    assert "introduced" not in result["lsp_diagnostics"]


@pytest.mark.parametrize("mode", ["write", "replace", "v4a"])
def test_file_tool_json_carries_receipt(ops, tmp_path, monkeypatch, mode):
    from tools import file_tools
    inject_transport(monkeypatch, receipt(phase="baseline"), receipt("unavailable"))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    path = tmp_path / "x.py"
    if mode == "write":
        output = file_tools.write_file_tool(str(path), "x = 1\n", task_id="consumer-test")
    else:
        path.write_text("x = 0\n")
        if mode == "replace":
            output = file_tools.patch_tool(path=str(path), old_string="x = 0", new_string="x = 1", task_id="consumer-test")
        else:
            output = file_tools.patch_tool(mode="patch", patch=f"*** Begin Patch\n*** Update File: {path}\n@@\n-x = 0\n+x = 1\n*** End Patch", task_id="consumer-test")
    result = json.loads(output)
    assert not result.get("error")
    info = result["lsp_enrichment"]
    if mode == "v4a":
        info = info[str(path)]
    assert info["quality"] == "unavailable"
    assert info["reason"] == "server_error"


def test_safe_receipt_projection_drops_invalid_phases_and_bounds_reason(ops, tmp_path, monkeypatch):
    weird = EnrichmentResult(quality="complete", reason="fresh\n" + "x" * 500,
                             phases={"initialize": float("nan"), "post_edit": float("inf"),
                                     "baseline": -1, "untrusted": {"payload": "data"}})
    inject_transport(monkeypatch, receipt(phase="baseline"), weird)
    result = ops.write_file(str(tmp_path / "x.py"), "x = 1\n").to_dict()
    info = result["lsp_enrichment"]
    assert info["quality"] == "complete"
    assert not info["phases"]
    assert "\n" not in info["reason"] and len(info["reason"]) <= 200
    json.dumps(result, allow_nan=False)


def test_unavailable_enrichment_without_syntax_checker_is_not_a_pass(ops, tmp_path):
    path = tmp_path / "note.unknown"
    result = ops.write_file(str(path), "opaque content\n").to_dict()
    assert not result.get("error") and path.read_text() == "opaque content\n"
    assert result["lint"]["status"] == "skipped"
    assert result["lsp_enrichment"]["quality"] == "unavailable"
    assert "lsp_diagnostics" not in result


def test_actual_service_transport_failure_is_visible_after_successful_write(ops, tmp_path, monkeypatch):
    inject_transport(monkeypatch, receipt(phase="baseline"), RuntimeError("transport down"))
    path = tmp_path / "x.py"
    result = ops.write_file(str(path), "x = 1\n").to_dict()
    assert not result.get("error") and path.read_text() == "x = 1\n"
    assert result["lsp_enrichment"]["quality"] == "unavailable"
    assert result["lsp_enrichment"]["reason"] == "submission_failed"
    assert result["lsp_enrichment"]["phases"]["post_edit"] == 0


def test_baseline_from_replaced_service_does_not_claim_edit_provenance(ops, tmp_path, monkeypatch):
    first, _ = inject_transport(monkeypatch, receipt(phase="baseline"), receipt())
    second, calls = inject_transport(monkeypatch, receipt(diagnostics=[diagnostic()]), receipt())
    services = iter([first, second])
    monkeypatch.setattr("agent.lsp.get_service", lambda: next(services))
    result = ops.write_file(str(tmp_path / "x.py"), "x = 1\n").to_dict()
    assert result["lsp_enrichment"]["diagnostic_scope"] == "current"
    assert "introduced" not in result["lsp_diagnostics"]
    assert calls[0]["delta"] is False


@pytest.mark.parametrize("moment", ["before_post", "during_post"])
def test_owner_change_cannot_claim_edit_provenance(ops, tmp_path, monkeypatch, moment):
    svc, calls = inject_transport(monkeypatch, receipt(phase="baseline"),
                                  receipt(diagnostics=[diagnostic()]))
    tokens = iter(["baseline-owner", "baseline-owner", "new-owner", "new-owner"]
                  if moment == "before_post" else
                  ["baseline-owner", "baseline-owner", "baseline-owner", "new-owner", "new-owner"])
    # Service snapshot, fileops snapshot, pre-query, service query, post-query
    monkeypatch.setattr(svc, "_owner_for_file", lambda path: next(tokens))
    result = ops.write_file(str(tmp_path / "x.py"), "x = 1\n").to_dict()
    assert result["lsp_enrichment"]["diagnostic_scope"] == "current"
    assert "introduced" not in result["lsp_diagnostics"]
    if moment == "before_post":
        assert calls[0]["delta"] is False


def test_missing_pre_edit_text_cannot_prove_shifted_findings_were_introduced(ops, tmp_path, monkeypatch):
    _, calls = inject_transport(monkeypatch, receipt(diagnostics=[diagnostic("old error")], phase="baseline"),
                                receipt(diagnostics=[diagnostic("old error", line=1)]))
    path = tmp_path / "x.py"
    path.write_text("x = 0\n")
    execute = ops._exec
    def unreadable_pre_text(command, **kwargs):
        if command.startswith("cat "):
            return SimpleNamespace(exit_code=1, stdout="")
        return execute(command, **kwargs)
    monkeypatch.setattr(ops, "_exec", unreadable_pre_text)
    result = ops.write_file(str(path), "# insert\nx = 0\n").to_dict()
    assert not result.get("error")
    assert path.read_text() == "# insert\nx = 0\n"
    assert result["lsp_enrichment"]["baseline"]["quality"] == "complete"
    assert result["lsp_enrichment"]["diagnostic_scope"] == "current"
    assert "old error" in result["lsp_diagnostics"]
    assert "introduced" not in result["lsp_diagnostics"]
    assert calls[0]["delta"] is False
