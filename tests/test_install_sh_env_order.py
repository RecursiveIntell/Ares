"""install.sh must resolve the Recursive Agent source before the step that reads it.

`install.sh` runs under `set -u`. Its `managed-runtime` step reads
ARES_RECURSIVE_AGENT_MODE and ARES_RECURSIVE_AGENT_SOURCE_SHA from the environment. These tests run the
real `recursive-agent-source` step and a real consumer of those variables, in both orders, and assert on
what happens - not on the text of the script.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
CONSUMED = ("ARES_RECURSIVE_AGENT_MODE", "ARES_RECURSIVE_AGENT_SOURCE_SHA")


def _step(text: str, name: str, next_name: str) -> str:
    """The body of one STEP, so it can be executed in isolation."""
    start = text.index(f"STEP={name}\n")
    end = text.index(f"STEP={next_name}\n", start)
    return text[start:end]


def _consumer() -> str:
    """A stand-in for the managed-runtime step's use of the two variables.

    This is the contract: a consumer that reads both names under `set -u` must succeed. It is written out
    here rather than derived from the script so that a rename in install.sh fails this test.
    """
    probe = "import os; " + "; ".join(f"print(os.environ[{name!r}])" for name in CONSUMED)
    return f'"{sys.executable}" -c {shlex.quote(probe)}'


def _run(script: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize("branch", ["disabled", "explicit-source", "manifest-pinned"])
def test_the_source_step_runs_before_the_consumer_that_reads_it(tmp_path, branch):
    text = INSTALL.read_text(encoding="utf-8")
    step = _step(text, "recursive-agent-source", "managed-runtime")

    source = tmp_path / "ra-source"
    (source / "scripts").mkdir(parents=True)
    installer = source / "scripts/install-hermes-plugin.sh"
    installer.write_text("#!/bin/sh\n")
    installer.chmod(0o755)
    recursive_agent, agent_source = {
        "disabled": ("false", ""),
        "explicit-source": ("true", str(source)),
        "manifest-pinned": ("true", ""),
    }[branch]

    preamble = "\n".join([
        "set -euo pipefail",
        'die() { printf "die: %s\\n" "$*" >&2; exit 1; }',
        'checkout_exact() { mkdir -p "$2"; }',
        f"PYTHON={shlex.quote(sys.executable)}",
        f"RECURSIVE_AGENT={recursive_agent}",
        f"RECURSIVE_AGENT_SOURCE={shlex.quote(agent_source)}",
        f"SOURCE_ROOT={shlex.quote(str(tmp_path / 'sources'))}",
    ])

    ok = _run("\n".join([preamble, step, _consumer()]), tmp_path)
    assert ok.returncode == 0, ok.stderr
    values = dict(zip(CONSUMED, ok.stdout.splitlines()))
    assert values["ARES_RECURSIVE_AGENT_MODE"] == {
        "disabled": "disabled",
        "explicit-source": "explicit-development-source",
        "manifest-pinned": "manifest-pinned",
    }[branch]
    assert values["ARES_RECURSIVE_AGENT_SOURCE_SHA"] == ("" if branch == "disabled" else "UNQUALIFIED")

    # The order is the fix: a consumer that runs first must fail under `set -u`, which is the defect this
    # change removes. If this ever passes, the test above has stopped proving anything.
    reversed_order = _run("\n".join([preamble, _consumer(), step]), tmp_path)
    assert reversed_order.returncode != 0, "reading the variables before the step must fail under set -u"
