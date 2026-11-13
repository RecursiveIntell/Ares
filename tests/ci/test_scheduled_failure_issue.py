"""Unit tests for the scheduled-failure issue sweeper decision core.

Run: uv run --python 3.14 --with pytest python -m pytest tests/ci/test_scheduled_failure_issue.py -q
Covers: green-close, red-report, red-outranks-green-job-page (verifier
finding: a red run must never close its issue), zero-job startup failures,
in-progress runs, never-scheduled workflows, explicit-success close with no
job detail, and the issue body shape (failing rows present; never an empty
table).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "scripts" / "ci"))

import scheduled_failure_issue as sfi  # noqa: E402


def test_green_run_closes():
    action, failing = sfi.decide({"status": "completed", "conclusion": "success"},
                                 ["success", "success", "skipped"])
    assert action == "close" and failing == []


def test_red_run_reports():
    action, failing = sfi.decide({"status": "completed", "conclusion": "failure"},
                                 ["success", "failure", "cancelled"])
    assert action == "report" and failing == ["failure", "cancelled"]


def test_red_run_never_closes_even_if_job_page_is_green():
    """Verifier high finding: a red run with a green-looking job page must
    still report (startup failures / truncation can hide failures)."""
    action, failing = sfi.decide({"status": "completed", "conclusion": "failure"},
                                 ["success", "success"])
    assert action == "report"
    action, _ = sfi.decide({"status": "completed", "conclusion": "timed_out"}, ["success"])
    assert action == "report"
    action, _ = sfi.decide({"status": "completed", "conclusion": "cancelled"}, ["success", "skipped"])
    assert action == "report"


def test_startup_failure_conclusion_is_red():
    """GitHub's graph-build failure conclusion is `startup_failure`, not
    `failure` (verified live in this repo's run data). It must be red with
    zero job rows — never 'pending' (the round-2 verifier's HIGH finding)."""
    action, failing = sfi.decide({"status": "completed", "conclusion": "startup_failure"}, [])
    assert action == "report" and failing == []
    action, _ = sfi.decide({"status": "completed", "conclusion": "startup_failure"}, None)
    assert action == "report"
    action, _ = sfi.decide({"status": "completed", "conclusion": "action_required"}, ["success"])
    assert action == "report"


def test_zero_job_startup_failure_reports_with_zero_jobs():
    action, failing = sfi.decide({"status": "completed", "conclusion": "failure"}, [])
    assert action == "report" and failing == []


def test_in_progress_run_is_pending():
    action, _ = sfi.decide({"status": "in_progress", "conclusion": None}, ["failure"])
    assert action == "pending"


def test_never_scheduled_is_skipped():
    action, failing = sfi.decide(None, None)
    assert action == "skip" and failing is None


def test_success_without_job_detail_closes_other_conclusions_pending():
    action, _ = sfi.decide({"status": "completed", "conclusion": "success"}, None)
    assert action == "close"
    # Unknown/neutral run conclusion and no job detail -> no decision.
    action, _ = sfi.decide({"status": "completed", "conclusion": "neutral"}, None)
    assert action == "pending"


def test_issue_body_lists_failing_rows():
    rows = [("build ok", "success"), ("tests broken", "failure"), ("deploy", "cancelled")]
    body = sfi.issue_body("W", {"databaseId": 1, "url": "u"}, rows, False)
    assert "| tests broken | failure |" in body
    assert "| deploy | cancelled |" in body
    assert "| build ok |" not in body
    assert "Failed: 2 of 3 job(s)." in body


def test_issue_body_renders_startup_failure_row():
    body = sfi.issue_body("OSV-Scanner",
                          {"databaseId": 123, "url": "https://example.com/run/123"},
                          [], True)
    assert "| (no job records available for this run — startup-level failure) | - |" in body


def test_issue_body_run_level_red_with_green_jobs_never_empty_table():
    body = sfi.issue_body("W", {"databaseId": 1, "url": "u"},
                          [("all green", "success")], False)
    assert "| (run-level failure; no failing job on fetched job pages) | - |" in body
