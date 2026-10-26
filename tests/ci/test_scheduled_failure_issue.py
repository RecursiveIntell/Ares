"""Unit tests for the scheduled-failure issue sweeper decision core.

Run: uv run --python 3.14 --with pytest python -m pytest tests/ci/test_scheduled_failure_issue.py -q
Covers: green-close, red-report, zero-job startup failures, in-progress
runs, never-scheduled workflows, run-conclusion fallback when job detail is
unavailable, and the issue body shape (no empty-table holes).
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


def test_zero_job_startup_failure_reports_with_zero_jobs():
    action, failing = sfi.decide({"status": "completed", "conclusion": "failure"}, [])
    assert action == "report" and failing == []


def test_in_progress_run_is_pending():
    action, _ = sfi.decide({"status": "in_progress", "conclusion": None},
                           ["failure"])  # jobs fetched but run not completed
    assert action == "pending"


def test_never_scheduled_is_skipped():
    action, failing = sfi.decide(None, None)
    assert action == "skip" and failing is None


def test_run_conclusion_fallback_without_jobs():
    action, failing = sfi.decide({"status": "completed", "conclusion": "failure"}, None)
    assert action == "report" and failing == []
    action, _ = sfi.decide({"status": "completed", "conclusion": "success"}, None)
    assert action == "close"
    # unknown non-terminal conclusion -> no decision
    action, _ = sfi.decide({"status": "completed", "conclusion": "neutral"}, None)
    assert action == "pending"


def test_issue_body_renders_startup_failure_row():
    body = sfi.issue_body("OSV-Scanner",
                          {"databaseId": 123, "url": "https://example.com/run/123"},
                          [], True)
    assert "| (no job records available for this run — startup-level failure) | - |" in body
    assert "[ci-scheduled-failure]" not in body  # title is separate from body


def test_issue_body_no_empty_table():
    body = sfi.issue_body("W", {"databaseId": 1, "url": "u"}, ["failure"], False)
    assert body.count("| job |") == 1
    assert "startup-level failure" not in body
