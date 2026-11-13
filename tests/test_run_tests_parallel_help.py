"""Task 6.4 guard: the runner honors --help before any discovery or pytest dispatch.

The help handling was repaired in e9a4a32b (audit L7). These tests pin it, so a later
change that routes --help into discovery or execution fails here.

Discovery and the per-file pytest dispatch are patched at module level to record calls.
A help request must return 0 with usage text and make zero such calls.
"""

import importlib.util
import pathlib
import sys

import pytest

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_tests_parallel.py"


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("run_tests_parallel_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = {"discover": 0, "run": 0}

    def discover(*args, **kwargs):
        calls["discover"] += 1
        return []

    def run_one(*args, **kwargs):
        calls["run"] += 1
        raise AssertionError("help must not launch a per-file pytest run")

    for name in ("_discover_files",):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, discover)
    for name in ("_run_file_with_retries", "_run_one_file", "_run_file"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, run_one)
    return module, calls


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_returns_zero_without_discovery_or_execution(runner, monkeypatch, capsys, flag):
    module, calls = runner
    monkeypatch.setattr(sys, "argv", ["run_tests_parallel.py", flag])

    rc = module.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out
    assert calls == {"discover": 0, "run": 0}, f"help reached discovery/execution: {calls}"


def test_help_after_passthrough_separator_is_not_intercepted(runner, monkeypatch, capsys):
    """Pytest's own -h after '--' must reach pytest, not the runner's help."""
    module, calls = runner
    monkeypatch.setattr(sys, "argv", ["run_tests_parallel.py", "--", "-h"])
    # The runner must not return help here; it proceeds past the help check. Discovery is
    # patched to an empty list, so the run ends without executing any test file.
    out_before = None
    rc = module.main()
    out = capsys_out = None
    # The early help return prints usage and exits 0 without discovery. Passthrough
    # reaches the normal path, which with no discovered files runs nothing and does not
    # print the runner's usage block at all.
    assert calls["discover"] >= 1, "passthrough -h must reach the normal discovery path"
    assert calls["run"] == 0, "no test file may run when discovery is empty"
