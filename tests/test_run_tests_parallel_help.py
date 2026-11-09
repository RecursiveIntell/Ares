"""Regression (audit L7): runner help must print usage and never run discovery.

Each subprocess is bounded and runs the runner with its test-discovery entry
points unreachable: the help path must return before any pytest dispatch.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests_parallel.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )


def test_long_help_prints_usage_without_discovery():
    result = _run("--help")
    combined = result.stdout + result.stderr
    assert result.returncode == 0
    assert "usage" in combined.lower()
    assert "Discovered" not in combined


def test_short_help_prints_usage_without_discovery():
    result = _run("-h")
    combined = result.stdout + result.stderr
    assert result.returncode == 0
    assert "usage" in combined.lower()
    assert "Discovered" not in combined
