"""Real-process configuration propagation for interrupted-turn replay."""

from __future__ import annotations

import os
import subprocess
import sys


_PROBE = """
from tui_gateway.server import _auto_continue_config
print(_auto_continue_config())
"""


def _probe(home):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["ARES_HOME"] = str(home)
    repo_root = os.getcwd()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (repo_root, env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", _PROBE],
        cwd=os.getcwd(),
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = "\n".join((result.stdout, result.stderr)).strip().splitlines()
    assert output, "configuration probe produced no observable output"
    return output[-1]


def test_default_and_explicit_auto_continue_config_in_real_process(tmp_path):
    assert _probe(tmp_path / "default") == "(False, 900.0, 2)"

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "config.yaml").write_text(
        "desktop:\n"
        "  auto_continue:\n"
        "    enabled: true\n"
        "    freshness_minutes: 15\n"
        "    max_attempts: 2\n",
        encoding="utf-8",
    )

    assert _probe(explicit) == "(True, 900.0, 2)"
