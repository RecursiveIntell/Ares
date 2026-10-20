"""Potential LSP availability cannot waive independent shell lint.

The existing tsconfig project exception and unsupported TSX behavior remain
separate from the optimistic service probe.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_fops():
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    return ShellFileOperations(LocalEnvironment())


@pytest.mark.parametrize("ext", [".ts", ".go", ".rs"])
def test_shell_linter_runs_when_lsp_will_handle(ext, tmp_path):
    """A configured service has supplied no fresh syntax verdict."""
    fops = _make_fops()
    src = tmp_path / f"bad{ext}"
    src.write_text("intentionally invalid content\n")
    failure = MagicMock(exit_code=1, stdout="syntax checker rejected content")
    with patch.object(fops, "_lsp_will_handle", return_value=True), \
         patch.object(fops, "_exec", return_value=failure) as execute, \
         patch.object(fops, "_has_command", return_value=True):
        result = fops._check_lint(str(src))
    assert execute.called
    assert not result.skipped and not result.success
    assert "rejected content" in result.output


def test_lsp_will_handle_swallows_enabled_for_exception(tmp_path):
    """A flaky LSP service must never break the shell-linter fallback —
    if ``enabled_for`` raises, we treat the file as "not handled" so the
    shell linter still runs."""
    fops = _make_fops()
    src = tmp_path / "foo.ts"
    src.write_text("const x = 1\n")

    fake_svc = MagicMock()
    fake_svc.enabled_for.side_effect = RuntimeError("server crashed")

    with patch.object(fops, "_lsp_local_only", return_value=True), \
         patch("agent.lsp.get_service", return_value=fake_svc):
        assert fops._lsp_will_handle(str(src)) is False




def test_tsx_default_check_lint_returns_skipped(tmp_path):
    """End-to-end: ``.tsx`` files get ``LintResult(skipped=True)`` from
    ``_check_lint`` regardless of LSP status — this is the no-regression
    contract that addresses Copilot review #3271017282."""
    fops = _make_fops()
    src = tmp_path / "foo.tsx"
    src.write_text("export const X = () => <div/>\n")

    # Even with LSP claiming the file, no shell linter runs for .tsx
    # because there's no LINTERS entry — the ``ext not in LINTERS``
    # branch fires before the LSP short-circuit is consulted.
    with patch.object(fops, "_lsp_will_handle", return_value=True), \
         patch.object(fops, "_exec") as exec_mock:
        result = fops._check_lint(str(src))

    assert result.skipped is True
    assert not exec_mock.called, "no shell linter should run for .tsx"


def test_ts_shell_linter_skipped_when_ancestor_tsconfig_present(tmp_path):
    """A .ts file under a dir tree containing tsconfig.json skips the per-file
    shell tsc EVEN WHEN LSP is inactive — single-file tsc can't read the
    project config, so its diagnostics are pure noise. This closes the
    LSP-disabled gap (the common default).

    _exec is patched to raise so any accidental shell-linter invocation fails
    the test.
    """
    fops = _make_fops()
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{}}\n')
    sub = tmp_path / "src" / "app"
    sub.mkdir(parents=True)
    src = sub / "thing.ts"
    src.write_text("import { x } from '@/store'\nexport const y = x\n")

    def _exec_must_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("shell tsc ran despite an ancestor tsconfig.json")

    with patch.object(fops, "_lsp_local_only", return_value=True), \
         patch.object(fops, "_lsp_will_handle", return_value=False), \
         patch.object(fops, "_exec", side_effect=_exec_must_not_run), \
         patch.object(fops, "_has_command", return_value=True):
        result = fops._check_lint(str(src))

    assert result.skipped is True
    assert "tsconfig.json" in (result.message or "")


def test_ts_shell_linter_runs_when_no_ancestor_tsconfig(tmp_path):
    """Without any ancestor tsconfig.json (a standalone .ts file), the shell
    tsc still runs — the ancestor-skip must not suppress lint for non-project
    files. We assert _exec IS reached (LSP inactive)."""
    fops = _make_fops()
    src = tmp_path / "loose.ts"
    src.write_text("const x: number = 'nope'\n")

    exec_result = MagicMock()
    exec_result.exit_code = 2
    exec_result.stdout = "loose.ts(1,7): error TS2322: Type 'string' ...\n"

    with patch.object(fops, "_lsp_local_only", return_value=True), \
         patch.object(fops, "_lsp_will_handle", return_value=False), \
         patch.object(fops, "_has_command", return_value=True), \
         patch.object(fops, "_exec", return_value=exec_result) as exec_mock:
        fops._check_lint(str(src))

    assert exec_mock.called, "shell tsc should run when there's no project tsconfig"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
