"""Task 1.2 (PLAN-FINAL): the install/update E2E harness flag probes.

Runs tests/install/test_harness_flag_probe.sh, which extracts the probes
verbatim from tests/install/install-update-e2e.sh and exercises them against
200 KB inputs (E-47). RED before the probe fix, GREEN after.

Agreed scope: behavioral assertions through the bash harness — no source-text
snapshot tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "install" / "test_harness_flag_probe.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash",
)


def test_harness_flag_probes_handle_large_scripts() -> None:
    proc = subprocess.run(
        ["bash", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"harness flag probe failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
