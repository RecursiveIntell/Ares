"""Behavioral coverage for required Node dependency installation."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_node_deps_stage(
    tmp_path: Path,
    *,
    fail_directory: str | None,
    failure_output: str = "simulated npm lifecycle failure",
    failure_code: int = 37,
    sleep_seconds: int = 0,
    timeout_seconds: int = 600,
    diagnostics_available: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    bin_dir = tmp_path / "bin"
    hermes_home = tmp_path / "home"
    managed_bin = hermes_home / "bin"
    npm_calls = tmp_path / "npm-calls"

    tui_dir.mkdir(parents=True)
    bin_dir.mkdir()
    (install_dir / "scripts").mkdir()
    if diagnostics_available:
        shutil.copyfile(REPO_ROOT / "scripts/npm_failure_diagnostics.py", install_dir / "scripts/npm_failure_diagnostics.py")
    managed_bin.mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"installer-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    (tui_dir / "package.json").write_text(
        '{"name":"tui-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    _write_executable(bin_dir / "node", "#!/bin/sh\necho v26.0.0\n")
    _write_executable(
        bin_dir / "npm",
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo 12.0.0
    exit 0
fi
printf '%s\\n' "$PWD" >> "$NPM_CALLS"
if [ -n "${NPM_FAIL_DIRECTORY:-}" ] && [ "$PWD" = "$NPM_FAIL_DIRECTORY" ]; then
    printf '%s\\n' "$NPM_FAILURE_OUTPUT" >&2
    sleep "$NPM_SLEEP_SECONDS"
    exit "$NPM_FAILURE_CODE"
fi
exit 0
""",
    )
    _write_executable(managed_bin / "uv", "#!/bin/sh\necho 'uv probe'\n")

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_INSTALL_DIR": str(install_dir),
            "NPM_CALLS": str(npm_calls),
            "NPM_FAIL_DIRECTORY": fail_directory or "",
            "NPM_FAILURE_OUTPUT": failure_output,
            "NPM_FAILURE_CODE": str(failure_code),
            "NPM_SLEEP_SECONDS": str(sleep_seconds),
            "NODE_DEPS_TIMEOUT": str(timeout_seconds),
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "node-deps",
            "--json",
            "--skip-browser",
            "--skip-computer-use",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = npm_calls.read_text(encoding="utf-8").splitlines()
    return proc, install_dir, calls


def _stage_result(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(proc.stdout.splitlines()[-1])


def test_root_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    proc, actual_install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(install_dir),
    )

    assert actual_install_dir == install_dir
    assert proc.returncode != 0
    assert _stage_result(proc) == {
        "ok": False,
        "stage": "node-deps",
        "skipped": False,
        "reason": "exit code 37",
    }
    assert calls == [str(install_dir)]
    assert "Node.js dependencies installed" not in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout
    assert not (install_dir / "node_modules").exists()


def test_tui_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    proc, _, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(tui_dir),
    )

    assert proc.returncode != 0
    assert _stage_result(proc)["ok"] is False
    assert calls == [str(install_dir), str(tui_dir)]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout


def test_node_dependency_success_remains_successful(tmp_path: Path) -> None:
    proc, install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=None,
    )

    assert proc.returncode == 0, proc.stderr
    assert _stage_result(proc) == {
        "ok": True,
        "stage": "node-deps",
        "skipped": False,
    }
    assert calls == [str(install_dir), str(install_dir / "ui-tui")]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" in proc.stdout


@pytest.mark.parametrize("code", [1, 37, 124])
@pytest.mark.parametrize("stage", ["root", "tui"])
def test_error_status_and_safe_cause_without_credentials(tmp_path, code, stage):
    secrets = ["bearer-secret-123", "url-secret-456", "npmrc-secret-789", "multiline-secret-012"]
    output = "\n".join([
        "npm error code ERESOLVE",
        "Authorization: Bearer " + secrets[0],
        "https://user:" + secrets[1] + "@registry.example/pkg?token=" + secrets[1],
        "//registry.example/:_authToken=" + secrets[2],
        "_password=" + secrets[2],
        "Authorization:\n  Bearer " + secrets[3],
        "-----BEGIN PRIVATE KEY-----\n" + secrets[3] + "\n-----END PRIVATE KEY-----",
    ])
    failure_dir = tmp_path / "install"
    if stage == "tui":
        failure_dir /= "ui-tui"
    proc, _, _ = _run_node_deps_stage(tmp_path, fail_directory=str(failure_dir),
                                     failure_output=output, failure_code=code)
    assert proc.returncode == code
    assert _stage_result(proc)["reason"] == f"exit code {code}"
    transcript = proc.stdout + proc.stderr
    assert all(secret not in transcript for secret in secrets)
    diagnostic = next(json.loads(line.split(" ", 1)[1]) for line in proc.stdout.splitlines()
                      if line.startswith("HERMES_NPM_DIAGNOSTIC "))
    assert diagnostic["exit_code"] == code
    assert diagnostic["stage"] == stage
    assert diagnostic["timeout_status"] == (code == 124)
    assert diagnostic["npm_codes"] == ["ERESOLVE"]
    assert diagnostic["safe_causes"] == ["Dependency resolution conflict"]
    log = tmp_path / "current-run.log"
    log.write_text(transcript)
    artifact = tmp_path / "npm-diagnostics.json"
    collected = subprocess.run([sys.executable, str(REPO_ROOT / "scripts/npm_failure_diagnostics.py"),
                                "collect", "--input", str(log), "--output", str(artifact)],
                               capture_output=True, text=True)
    assert collected.returncode == 0
    assert json.loads(artifact.read_text()) == [diagnostic]
    assert all(secret not in artifact.read_text() for secret in secrets)


def test_actual_timeout_preserves_124(tmp_path):
    proc, _, _ = _run_node_deps_stage(tmp_path, fail_directory=str(tmp_path / "install"),
                                     sleep_seconds=5, timeout_seconds=1)
    assert proc.returncode == 124
    assert _stage_result(proc)["reason"] == "exit code 124"
    assert '"timeout_status": true' in proc.stdout


def test_empty_npm_output_is_explicit_not_success(tmp_path):
    proc, _, _ = _run_node_deps_stage(tmp_path, fail_directory=str(tmp_path / "install"), failure_output="")
    assert proc.returncode == 37
    assert "no recognized error code" in proc.stdout


def test_missing_helper_does_not_leak_raw_output_or_mask_failure(tmp_path):
    proc, _, _ = _run_node_deps_stage(tmp_path, fail_directory=str(tmp_path / "install"),
                                     failure_output="Authorization: Bearer private-sentinel",
                                     diagnostics_available=False)
    assert proc.returncode == 37
    assert "Sanitized npm diagnostics unavailable" in proc.stdout
    assert "private-sentinel" not in proc.stdout + proc.stderr
