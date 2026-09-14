"""Source-level contracts for the Ares CI planner aggregate."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yaml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_desktop_e2e_disabled_by_literal_false_only():
    text = workflow_text()
    match = re.search(r"(?ms)^  e2e-desktop:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert match is not None
    block = match.group(0)
    assert re.search(r"^    if: false\s*$", block, re.MULTILINE)
    assert "false &&" not in block


def test_disabled_desktop_e2e_remains_an_explicit_aggregate_need():
    text = workflow_text()
    aggregate = re.search(r"(?ms)^  all-checks-pass:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert aggregate is not None
    assert re.search(r"^      - e2e-desktop\s*$", aggregate.group(0), re.MULTILINE)


def test_aggregate_invokes_expectation_aware_evaluator():
    text = workflow_text()
    aggregate = re.search(r"(?ms)^  all-checks-pass:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert aggregate is not None
    block = aggregate.group(0)
    assert "scripts/ci/evaluate_required_checks.py" in block
    assert "toJSON(needs)" in block
    assert "toJSON(needs.detect.outputs)" in block


def test_aggregate_runs_checksum_pinned_actionlint_before_evaluator():
    text = workflow_text()
    aggregate = re.search(r"(?ms)^  all-checks-pass:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert aggregate is not None
    block = aggregate.group(0)
    assert "ACTIONLINT_VERSION: 1.7.11" in block
    assert "ACTIONLINT_SHA256: 900919a84f2229bac68ca9cd4103ea297abc35e9689ebb842c6e34a3d1b01b0a" in block
    assert "archive=\"actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz\"" in block
    assert "Lint GitHub Actions workflows" in block
    assert "scripts/ci/evaluate_required_checks.py" in block


def test_aggregate_is_job_scoped_to_read_only_permissions():
    text = workflow_text()
    aggregate = re.search(r"(?ms)^  all-checks-pass:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert aggregate is not None
    block = aggregate.group(0)
    assert "    permissions:\n      contents: read" in block
    assert "pull-requests: write" not in block
    assert "security-events: write" not in block


def test_aggregate_has_always_and_complete_known_need_set():
    text = workflow_text()
    aggregate = re.search(r"(?ms)^  all-checks-pass:.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    assert aggregate is not None
    block = aggregate.group(0)
    assert re.search(r"^    if: always\(\)\s*$", block, re.MULTILINE)
    expected = {
        "detect",
        "tests",
        "tests-os",
        "lint",
        "js-tests",
        "installer-tests",
        "rust-tests",
        "e2e-desktop",
        "docs-site",
        "history-check",
        "contributor-check",
        "uv-lockfile",
        "lockfile-diff",
        "docker-lint",
        "supply-chain",
        "review-labels",
        "osv-scanner",
    }
    needs = set(re.findall(r"^      - ([a-z0-9_-]+)\s*$", block, re.MULTILINE))
    assert expected <= needs
