"""Tests for the least-privilege workflow-permission checker (Task 4.2)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "scripts" / "ci"))

import check_workflow_permissions as cwp  # noqa: E402


def _write(tmp_path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


CHECKOUT_JOB = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


def test_pull_requests_write_is_refused(tmp_path):
    _write(tmp_path, "sample.yml", """
on: [pull_request, push]
permissions:
  contents: read
  pull-requests: write
""" + CHECKOUT_JOB)
    findings = cwp.check(tmp_path)
    assert any("pull-requests: write" in f and "sample.yml" in f for f in findings)


def test_pull_requests_read_is_fine(tmp_path):
    _write(tmp_path, "sample.yml", """
on: pull_request
permissions:
  contents: read
  pull-requests: read
""" + CHECKOUT_JOB)
    assert cwp.check(tmp_path) == []


def test_security_events_write_only_in_allowlist(tmp_path):
    _write(tmp_path, "other.yml", """
on: [pull_request, schedule]
permissions:
  security-events: write
""" + CHECKOUT_JOB)
    findings = cwp.check(tmp_path)
    assert any("security-events: write" in f and "other.yml" in f for f in findings)


def test_unexpected_write_scope_refused(tmp_path):
    _write(tmp_path, "sample.yml", """
on: [pull_request, push]
permissions:
  contents: write
""" + CHECKOUT_JOB)
    findings = cwp.check(tmp_path)
    assert any("contents" in f for f in findings)


def test_no_checkout_activity_workflow_is_exempt(tmp_path):
    """label-rerun-style automation: PR-triggered, holds a write scope, but
    never checks out code — sanctioned write context."""
    _write(tmp_path, "activity.yml", """
on:
  pull_request:
    types: [labeled]
permissions:
  actions: write
  pull-requests: read
jobs:
  act:
    runs-on: ubuntu-latest
    steps:
      - run: echo api-only
""")
    assert cwp.check(tmp_path) == []


def test_current_repo_workflows_are_clean():
    """GREEN condition: after Task 4.2 edits, the live workflows pass."""
    findings = cwp.check()
    assert findings == [], findings
