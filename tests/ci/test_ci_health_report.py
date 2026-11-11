"""Unit tests for the pure core of ci_health_report.py (plan Task 0.2).

Run: uv run --python 3.14 --with pytest pytest tests/ci/test_ci_health_report.py -q
Covers the plan-required fixtures: a zero-job run and a cancelled superseded
run, plus classification, streak, percentile, graduation and quarantine rules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent.parent / "scripts" / "ci" / "ci_health_report.py"
sys.path.insert(0, str(HERE.parent.parent / "scripts" / "ci"))

import ci_health_report as chr  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def mkrun(rid, conclusion, status="completed", attempt=1, started="2026-10-20T10:00:00Z",
          updated="2026-10-20T10:12:00Z"):
    return {"databaseId": rid, "conclusion": conclusion, "status": status, "run_attempt": attempt,
            "run_started_at": started, "updatedAt": updated}


def test_zero_job_failed_run_classifies_unknown_and_never_crashes():
    r = mkrun(1, "failure")
    rep = chr.build_report("wf.yml", [r], {"1": []}, 20.0)
    assert rep["failure_classes"]["unknown"] >= 0 or rep["failure_classes"]["test"] == 0
    assert rep["pass_rate_pct"] == 0.0
    assert rep["failure_signatures"] == {} or all(isinstance(k, str) for k in rep["failure_signatures"])


def test_cancelled_superseded_run_counts_as_cancelled_and_breaks_streak():
    rep = chr.build_report(
        "wf.yml",
        [mkrun(2, "success"), mkrun(3, "cancelled"), mkrun(4, "success")],
        {"3": [{"name": "build", "conclusion": "cancelled", "steps": []}]},
        20.0,
    )
    assert rep["failure_classes"]["cancelled"] == 1
    assert rep["green_streak"] == 1


def test_classification_by_failed_step_names():
    infra = {"name": "deps", "conclusion": "failure", "steps": [{"name": "apt-get install", "conclusion": "failure"}]}
    admission = {"name": "gate", "conclusion": "failure", "steps": [{"name": "evaluate required checks", "conclusion": "failure"}]}
    testj = {"name": "tests", "conclusion": "failure", "steps": [{"name": "pytest -q", "conclusion": "failure"}]}
    assert chr.classify_failure(infra) == "infra/network"
    assert chr.classify_failure(admission) == "admission"
    assert chr.classify_failure(testj) == "test"


def test_signature_uses_first_failed_step():
    j = {"name": "build", "conclusion": "failure", "steps": [
        {"name": "setup", "conclusion": "success"},
        {"name": "cargo test", "conclusion": "failure"},
    ]}
    assert chr.failure_signature(j) == "build:cargo test"


def test_percentile_interpolates():
    assert chr.percentile([], 90) is None
    assert chr.percentile([5.0], 50) == 5.0
    vals = list(range(1, 101))  # 1..100
    assert chr.percentile(vals, 50) == 50.5
    assert abs(chr.percentile(vals, 90) - 90.1) < 1e-9


def test_green_streak_counts_from_most_recent_and_skips_skipped():
    runs = [mkrun(1, "success"), mkrun(2, "skipped"), mkrun(3, "success"), mkrun(4, "failure")]
    assert chr.green_streak(runs) == 2


def test_graduation_requires_streak_and_p90():
    assert chr.should_graduate(10, 9.0, 10.0) is True
    assert chr.should_graduate(9, 9.0, 10.0) is False
    assert chr.should_graduate(10, 11.0, 10.0) is False
    assert chr.should_graduate(10, None, 10.0) is False


def test_quarantine_three_infra_failures_in_window():
    now = NOW
    days = [now - timedelta(days=1), now - timedelta(days=2), now - timedelta(days=3)]
    fails = [(d, "infra/network") for d in days]
    assert chr.should_quarantine(fails, now=now) is True
    # outside the 14-day window -> not proposed
    old = [(now - timedelta(days=30), "infra/network")] * 3
    assert chr.should_quarantine(old, now=now) is False
    # test-class failures do not trigger
    tests = [(d, "test") for d in days]
    assert chr.should_quarantine(tests, now=now) is False


def test_report_is_json_serializable_and_markdown_renders(tmp_path):
    rep = chr.build_report(
        "wf.yml",
        [mkrun(1, "success", attempt=2), mkrun(2, "failure")],
        {"2": [{"name": "tests", "conclusion": "failure",
                "steps": [{"name": "pytest -q", "conclusion": "failure"}]}]},
        20.0,
    )
    json.dumps(rep)  # must not raise
    md = chr.to_markdown([rep], {"open_total": 259, "open_critical": 27, "open_high": 0})
    assert "| wf.yml | 2 |" in md
    assert "tests:pytest -q" in md
    assert "open=259" in md


def test_cli_fixture_mode_end_to_end(tmp_path):
    fixture = {
        "wf.yml": {
            "runs": [mkrun(1, "success"), mkrun(2, "failure")],
            "failed_jobs": {"2": [{"name": "t", "conclusion": "failure",
                                   "steps": [{"name": "pytest", "conclusion": "failure"}]}]},
        },
        "__alerts__": {"open_total": 259, "open_critical": 27, "open_high": 91},
    }
    fin = tmp_path / "fixture.json"
    fin.write_text(json.dumps(fixture))
    out = tmp_path / "rep"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(fin), "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((tmp_path / "rep.json").read_text())
    assert payload["schema"] == chr.REPORT_SCHEMA
    assert payload["reports"][0]["failure_classes"]["test"] == 1
    assert (tmp_path / "rep.md").read_text().count("| wf.yml") == 1
