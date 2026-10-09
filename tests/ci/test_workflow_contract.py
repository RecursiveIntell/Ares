"""Source-level contracts for the Ares CI planner aggregate."""
from __future__ import annotations

import re
from pathlib import Path

from scripts.ci.evaluate_required_checks import KNOWN_JOBS


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
    lint = next(step for step in _workflow()["jobs"]["all-checks-pass"]["steps"]
                if step.get("name") == "Lint GitHub Actions workflows")
    assert ".github/workflows/ci.yaml" in lint["run"]
    assert ".github/workflows/context-continuity-qualification.yml" in lint["run"]
    assert ".github/workflows/current-owner-integration.yml" in lint["run"]
    assert "for attempt in range(1, 4):" in block
    assert "time.sleep(attempt * 2)" in block
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
    needs = set(re.findall(r"^      - ([a-z0-9_-]+)\s*$", block, re.MULTILINE))
    assert KNOWN_JOBS == needs



def _workflow(path=".github/workflows/ci.yaml"):
    import yaml
    # BaseLoader preserves GitHub's "on" and literal false as scalar text.
    return yaml.load((ROOT / path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_native_callers_reuse_owners_with_read_only_tokens_and_no_secrets():
    jobs = _workflow()["jobs"]
    for caller, lane, workflow in [
        ("context-continuity", "context_continuity", "context-continuity-qualification.yml"),
        ("current-owner-integration", "current_owner_integration", "current-owner-integration.yml"),
    ]:
        job = jobs[caller]
        assert job["needs"] == "detect"
        assert job["if"] == f"needs.detect.outputs.{lane} == 'true'"
        assert job["uses"] == f"./.github/workflows/{workflow}"
        assert job["permissions"] == {"contents": "read"}
        assert "secrets" not in job
        assert job.get("continue-on-error", "false") == "false"
        assert lane in jobs["detect"]["outputs"]
        assert caller in jobs["all-checks-pass"]["needs"]


def test_native_workflow_outputs_bind_each_unconditional_owner_job():
    for path, bindings in [
        ("context-continuity-qualification.yml", {
            "native_external_owner_result": "native-external-owner",
            "focused_tests_result": "focused-tests",
        }),
        ("current-owner-integration.yml", {
            "profile_runtime_consumer_result": "profile-runtime-consumer",
        }),
    ]:
        workflow = _workflow(f".github/workflows/{path}")
        assert workflow["permissions"] == {"contents": "read"}
        assert "pull_request" not in workflow["on"]
        outputs = workflow["on"]["workflow_call"]["outputs"]
        for output, job_id in bindings.items():
            assert outputs[output]["value"] == "${{ jobs." + job_id + ".outputs.qualification_result }}"
            job = workflow["jobs"][job_id]
            assert job["outputs"]["qualification_result"] == "${{ job.status }}"
            assert "if" not in job
            assert job.get("continue-on-error", "false") == "false"
            assert all(step.get("continue-on-error", "false") == "false" for step in job["steps"])


def test_native_main_and_pr_orchestration_has_one_owner():
    ci = _workflow()
    assert ci["on"]["push"]["branches"] == ["main"]
    continuity = _workflow(".github/workflows/context-continuity-qualification.yml")
    assert continuity["on"]["push"]["branches"] == ["feat/context-continuity-v4-20260923"]
    owner = _workflow(".github/workflows/current-owner-integration.yml")
    assert set(owner["on"]) == {"workflow_call"}
