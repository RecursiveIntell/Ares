#!/usr/bin/env python3.14
"""Task 1.1 (PLAN-FINAL) behavior test: install_uv() pins uv at install time.

Follows tests/test_install_commit_pin_rollback.py conventions: extract the
function block from scripts/install.sh, run it under bash against a sandbox
with a fake curl (writes a fake installer) and a fake managed uv.

RED today: a managed uv outside the reviewed range (e.g. 0.13.5) is reused
as-is; and the installer URL is unversioned so a fresh install can pick up an
unreviewed uv.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("bash") is None,
        reason="needs bash",
    ),
    # live_system_guard_bypass: the extracted install_uv() block mentions
    # $HERMES_HOME and the word "update" in a comment, which trips the
    # conftest update-spawn heuristic. The executed script is inert: it runs
    # in a pytest tmp_path with a shimmed PATH (no real curl, no real hermes
    # binary reachable) and performs no repository writes.
    pytest.mark.live_system_guard_bypass,
]


def _extract_blocks() -> str:
    """Pull the pin helper and install_uv() out of scripts/install.sh."""
    text = INSTALL_SH.read_text()
    helper = re.search(r"_uv_version_in_range\(\) \{.*?\n\}\n", text, re.DOTALL)
    func = re.search(r"install_uv\(\) \{.*?\n\}\n", text, re.DOTALL)
    assert helper is not None, "pin range helper missing from scripts/install.sh"
    assert func is not None, "install_uv() not found in scripts/install.sh"
    pin = re.search(r'^UV_PIN="(\d+\.\d+\.\d+)"', text, re.M)
    assert pin is not None, 'UV_PIN="x.y.z" constant missing from scripts/install.sh'
    return f'UV_PIN="{pin.group(1)}"\n' + helper.group(0) + "\n" + func.group(0)


def _fake_uv(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho \"uv {version}\"\n")
    path.chmod(path.stat().st_mode | 0o755)


def _sandbox(tmp_path: Path, existing_version: str | None) -> tuple[Path, Path]:
    """Temp HERMES_HOME + bin dir with fake curl on PATH; returns (home, bindir)."""
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    bin_shim = tmp_path / "path"
    bin_shim.mkdir()
    fake_curl = bin_shim / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "# fake curl -LsSf URL -o PATH: writes a fake astral installer that\n"
        "# installs uv 0.12.24 (the pin) into $UV_UNMANAGED_INSTALL\n"
        'out=""\n'
        'prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'cat > "$out" <<\'INSTALLER\'\n'
        "#!/bin/sh\n"
        'mkdir -p "${UV_UNMANAGED_INSTALL:-/nonexistent}"\n'
        'printf "#!/bin/sh\\necho uv 0.12.24\\n" > "${UV_UNMANAGED_INSTALL}/uv"\n'
        'chmod +x "${UV_UNMANAGED_INSTALL}/uv"\n'
        "INSTALLER\n",
    )
    fake_curl.chmod(0o755)
    if existing_version is not None:
        _fake_uv(home / "bin" / "uv", existing_version)
    return home, bin_shim


def _run_install_uv(home: Path, bin_shim: Path) -> str:
    script = "\n".join(
        [
            "set -u",
            'DISTRO="${DISTRO:-linux}"',
            _extract_blocks(),
            f'HERMES_HOME="{home}"',
            "log_info() { :; }",
            "log_success() { :; }",
            "log_error() { echo \"ERROR: $*\" >&2; }",
            f'PATH="{bin_shim}:$PATH"',
            "install_uv",
            'echo "RESULT:$($UV_CMD --version 2>&1)"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"install_uv failed:\n{proc.stdout}\n{proc.stderr}"
    match = re.search(r"RESULT:(.*)", proc.stdout)
    assert match is not None, f"no RESULT in output:\n{proc.stdout}\n{proc.stderr}"
    return match.group(1).strip()


def test_fresh_install_gets_pinned_uv(tmp_path):
    home, bin_shim = _sandbox(tmp_path, existing_version=None)
    version = _run_install_uv(home, bin_shim)
    assert version == "uv 0.12.24", f"fresh install must produce pinned uv, got {version!r}"


def test_out_of_range_managed_uv_is_replaced_with_pin(tmp_path):
    home, bin_shim = _sandbox(tmp_path, existing_version="0.13.5")
    version = _run_install_uv(home, bin_shim)
    assert version == "uv 0.12.24", (
        f"managed uv outside reviewed range must be replaced with the pin, got {version!r}"
    )


def test_in_range_managed_uv_is_kept(tmp_path):
    home, bin_shim = _sandbox(tmp_path, existing_version="0.12.10")
    version = _run_install_uv(home, bin_shim)
    assert version == "uv 0.12.10", "in-range managed uv must not be needlessly reinstalled"
