"""Run the canonical installer's hermetic contract battery in the per-file lane."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.linux_only
def test_installer_contract_battery() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "scripts/tests/test-ares-installer-contract.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
