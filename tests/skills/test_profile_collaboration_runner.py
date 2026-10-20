"""Regression coverage for the Ares profile-collaboration runner."""

from __future__ import annotations

import json
import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from agent.secret_scope import ProfileEnvBoundary


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "optional-skills"
    / "productivity"
    / "profile-collaboration"
    / "scripts"
    / "run_panel.py"
)
_spec = importlib.util.spec_from_file_location("profile_panel_runner_under_test", SCRIPT)
_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_panel)


def _runtime(tmp_path: Path, main_body: str) -> Path:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    package = runtime / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(main_body, encoding="utf-8")
    (runtime / "hermes_state.py").write_text(
        "class SessionDB:\n"
        "    def get_session(self, session_id): return None\n"
        "    def close(self): pass\n",
        encoding="utf-8",
    )
    return runtime


def _invoke(tmp_path: Path, runtime: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
    # Load the real controller CLI package before main adds the synthetic
    # worker runtime to sys.path. That runtime deliberately has only a fake
    # CLI entrypoint and archive owner, not a second policy implementation.
    importlib.import_module("hermes_cli")
    home = tmp_path / "home"
    (home / ".ares" / "profiles" / "public").mkdir(parents=True)
    args = argparse.Namespace(
        runtime=runtime, workspace=workspace, out=tmp_path / "receipt",
        brief="bounded regression probe", profiles="public", full_panel=False,
        max_workers=1, timeout=10, panel_timeout=20, dry_run=False,
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    source_home = home / ".ares"
    # The fake CLI runtime has no profile policy/config implementation. Use
    # the real controller policy owner with a finite empty secret snapshot;
    # worker and archive interpreters still run against the hostile workspace.
    def boundary(*, source_home, target_home):
        return ProfileEnvBoundary(source_home, target_home, frozenset(), {})
    previous_path = list(sys.path)
    previous_umask = os.umask(0o077)
    try:
        with patch.dict(os.environ, {"HOME": str(home), "HERMES_HOME": str(source_home), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "TERMINAL_CWD": "/hostile/controller-workspace"}, clear=True), patch.object(Path, "home", return_value=home), patch.object(_panel, "parse_args", return_value=args), patch.object(_panel, "runtime_revision", return_value=None), patch("agent.secret_scope.build_profile_env_boundary", side_effect=boundary), redirect_stdout(stdout), redirect_stderr(stderr):
            code = _panel.main()
    finally:
        sys.path[:] = previous_path
        os.umask(previous_umask)
    receipt = json.loads((tmp_path / "receipt" / "panel.json").read_text(encoding="utf-8"))
    return subprocess.CompletedProcess(["inprocess-panel"], code, stdout.getvalue(), stderr.getvalue() + json.dumps(receipt["results"]))


def test_profile_process_imports_runtime_not_workspace_shadow(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        "import os\nprint('runtime-owner')\nprint(os.environ.get('TERMINAL_CWD'))\n",
    )
    workspace = tmp_path / "workspace"
    shadow = workspace / "hermes_cli"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "main.py").write_text("print('workspace-shadow')\n", encoding="utf-8")
    poison = workspace / "archive-shadow-imported"
    (workspace / "hermes_state.py").write_text(
        f"from pathlib import Path\nPath({str(poison)!r}).write_text('poison')\n"
        "raise RuntimeError('workspace hermes_state imported')\n",
        encoding="utf-8",
    )

    completed = _invoke(tmp_path, runtime, workspace)

    assert completed.returncode == 0, completed.stderr
    panel = json.loads((tmp_path / "receipt" / "panel.json").read_text(encoding="utf-8"))
    result = panel["results"][0]
    stdout = (tmp_path / "receipt" / result["stdout_path"]).read_text(encoding="utf-8")
    assert stdout.splitlines() == ["runtime-owner", str(workspace)]
    assert result["session_archive_outcome"] == "not_created"
    assert not poison.exists()


def test_exit_zero_daemon_pool_block_report_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        "print(\"Unable to complete because every inspection tool failed: "
        "DaemonThreadPoolExecutor object has no attribute '_initializer'\")\n",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    completed = _invoke(tmp_path, runtime, workspace)

    assert completed.returncode == 1
    panel = json.loads((tmp_path / "receipt" / "panel.json").read_text(encoding="utf-8"))
    result = panel["results"][0]
    assert result["exit_code"] == 0
    assert result["outcome"] == "blocked"
    assert result["termination_reason"] == "operational_blocked_report"
    assert result["operational_block"] == "daemon_pool_initializer_failure"
    assert panel["execution_complete"] is False
