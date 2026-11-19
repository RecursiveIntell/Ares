"""TESTOP card (P4): disposable test execution as a fixed graph operation.

A named test operation binds fixed argv/deps and a captured project cwd.
Not general command execution: only registered suites in isolated worktrees.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tools.delegate_tool import (
    register_graph_test_operation,
    run_graph_test_operation,
)


@pytest.fixture
def suite(tmp_path):
    """A disposable project with one passing and one failing test file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_pass.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    (proj / "test_fail.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n")
    return proj


def _parent():
    from types import SimpleNamespace
    return SimpleNamespace(session_id="testop-owner", enabled_toolsets=["terminal"], _delegate_depth=0)


def test_registered_passing_suite_exits_zero(suite):
    register_graph_test_operation(
        "suite-pass", argv=[sys.executable, "-m", "pytest", "-q", "test_pass.py"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-pass", suite="suite-pass",
        parent_agent=_parent(), timeout_seconds=60,
    )
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["exit_state"] == "observed"


def test_deliberately_failing_suite_is_a_failure_not_a_pass(suite):
    register_graph_test_operation(
        "suite-fail", argv=[sys.executable, "-m", "pytest", "-q", "test_fail.py"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-fail", suite="suite-fail",
        parent_agent=_parent(), timeout_seconds=60,
    )
    assert result["status"] == "failed"
    assert result["exit_code"] != 0
    assert result["exit_state"] == "observed"


def test_unregistered_suite_is_refused(suite):
    result = run_graph_test_operation(
        operation_id="op-test-unknown", suite="not-registered",
        parent_agent=_parent(), timeout_seconds=60,
    )
    assert result["status"] == "blocked"
    assert result["code"] == "test_suite_unregistered"


def test_arbitrary_argv_cannot_be_injected(suite):
    # The model/caller cannot supply argv at run time — only a registered name.
    result = run_graph_test_operation(
        operation_id="op-test-inject", suite="suite-pass",
        parent_agent=_parent(), timeout_seconds=60,
        argv=["/bin/sh", "-c", "echo pwned"],
    )
    assert result["status"] == "blocked"
    assert result["code"] == "test_argv_not_caller_supplied"


def test_expired_grant_does_not_launch(suite):
    register_graph_test_operation(
        "suite-exp", argv=[sys.executable, "-m", "pytest", "-q", "test_pass.py"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-exp", suite="suite-exp",
        parent_agent=_parent(), timeout_seconds=0,
    )
    assert result["status"] == "blocked"
    assert result["code"] == "test_deadline_expired"
    assert result.get("exit_code") is None


def test_timeout_is_unknown_not_pass(suite):
    register_graph_test_operation(
        "suite-slow", argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-slow", suite="suite-slow",
        parent_agent=_parent(), timeout_seconds=1,
    )
    assert result["status"] == "unknown"
    assert result["exit_state"] == "timeout"
    assert result.get("exit_code") is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_timeout_kills_delayed_write_descendant_before_reporting_closed(suite, tmp_path):
    marker = tmp_path / "late-child-write.txt"
    child_code = (
        "import time; from pathlib import Path; time.sleep(1.0); "
        f"Path({str(marker)!r}).write_text('late')"
    )
    suite_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    register_graph_test_operation(
        "suite-tree-timeout",
        argv=[sys.executable, "-c", suite_code],
        cwd=suite,
    )

    result = run_graph_test_operation(
        operation_id="op-test-tree-timeout", suite="suite-tree-timeout",
        parent_agent=_parent(), timeout_seconds=0.25,
    )

    assert result["status"] == "unknown"
    assert result["exit_state"] == "timeout"
    assert result["cleanup_state"] == "closed"
    time.sleep(1.2)
    assert not marker.exists(), "descendant wrote after timeout cleanup was reported closed"


def test_missing_owner_is_refused(suite):
    register_graph_test_operation(
        "suite-noowner", argv=[sys.executable, "-m", "pytest", "-q", "test_pass.py"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-noowner", suite="suite-noowner",
        parent_agent=None, timeout_seconds=60,
    )
    assert result["status"] == "blocked"
    assert result["code"] == "policy_owner_required"


def test_output_is_bounded_and_cleanup_confirmed(suite):
    register_graph_test_operation(
        "suite-bounded", argv=[sys.executable, "-c", "print('x' * 200000)"],
        cwd=suite,
    )
    result = run_graph_test_operation(
        operation_id="op-test-bounded", suite="suite-bounded",
        parent_agent=_parent(), timeout_seconds=30,
    )
    assert result["status"] == "completed"
    assert len(result["stdout_tail"]) <= 8192
    assert result["cleanup_state"] in ("closed", "not_required")
