"""Regression coverage for the Ares profile-collaboration runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "optional-skills"
    / "productivity"
    / "profile-collaboration"
    / "scripts"
    / "run_panel.py"
)


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
    home = tmp_path / "home"
    (home / ".ares" / "profiles" / "public").mkdir(parents=True)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime",
            str(runtime),
            "--workspace",
            str(workspace),
            "--out",
            str(tmp_path / "receipt"),
            "--brief",
            "bounded regression probe",
            "--profiles",
            "public",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HOME": str(home)},
    )


def test_profile_process_imports_runtime_not_workspace_shadow(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "print('runtime-owner')\n")
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
    assert stdout.strip() == "runtime-owner"
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
