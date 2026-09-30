"""Run the bootstrap subprocess contract in the canonical per-file test lane."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.linux_only
def test_bootstrap_uses_available_runtime_and_preserves_setup_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "scripts/tests/test-ares-bootstrap.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
