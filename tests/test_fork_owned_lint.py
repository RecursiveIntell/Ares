"""Phase 6.2 guard: fork-owned Python paths carry no unused imports (pyflakes F401).

The repo's blocking lint selects PLW1514 only. Enabling F repo-wide would touch unrelated
code, which the card forbids. This guard enforces F401 on the fork-owned paths directly, by
running ruff with an explicit --select on those paths, so it cannot drift into a repo-wide
cleanup. It fails if an unused import reappears there.
"""

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Fork-owned paths named by the Phase 6.2 card.
FORK_PATHS = [
    "ares_runtime/continuity/runtime.py",
    "ares_runtime/continuity/live.py",
    "ares_runtime/contracts.py",
    "ares_runtime/specialist_dispatch.py",
    "hermes_cli/ares_candidate_store.py",
    "hermes_state_common.py",
]


def _ruff():
    exe = shutil.which("ruff")
    if exe is None:
        pytest.skip("ruff not on PATH; lint guard needs the project lint tool")
    return exe


def test_fork_owned_paths_have_no_unused_imports():
    existing = [p for p in FORK_PATHS if (ROOT / p).exists()]
    assert existing, "fork-owned paths must exist for the guard to mean anything"
    proc = subprocess.run(
        [_ruff(), "check", "--no-cache", "--select", "F401", "--output-format", "concise", *existing],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"unused imports on fork-owned paths:\n{proc.stdout}{proc.stderr}"
