"""Behavioral tests for the expectation-aware CI aggregate evaluator."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.ci.evaluate_required_checks import evaluate


CLASSIFIER_KEYS = (
    "python",
    "python_prod",
    "frontend",
    "site",
    "scan",
    "deps",
    "uv_lock",
    "npm_lock",
    "installer",
    "rust",
    "docker_meta",
    "mcp_catalog",
    "ci_review",
    "ci_review_files",
)


def classifier(**overrides: str) -> dict[str, str]:
    result = {key: "false" for key in CLASSIFIER_KEYS}
    result["ci_review_files"] = ""
    result.update(overrides)
    return result


def jobs_for(classifier_values: dict[str, str], *, event: str = "pull_request") -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {"detect": {"result": "success"}}
    conditional = {
        "tests": "python",
        "tests-os": "python",
        "lint": "python",
        "js-tests": "frontend",
        "installer-tests": "installer",
        "rust-tests": "rust",
        "docs-site": "site",
        "contributor-check": "python",
        "uv-lockfile": "uv_lock",
        "lockfile-diff": "npm_lock",
        "docker-lint": "docker_meta",
    }
    for job, key in conditional.items():
        applies = classifier_values[key] == "true"
        if job == "lockfile-diff":
            applies = applies and event == "pull_request"
        result[job] = {"result": "success" if applies else "skipped"}
    result["history-check"] = {"result": "success" if event == "pull_request" else "skipped"}
    result["supply-chain"] = {
        "result": "success"
        if event == "pull_request" and (classifier_values["scan"] == "true" or classifier_values["deps"] == "true")
        else "skipped"
    }
    result["review-labels"] = {
        "result": "success"
        if event == "pull_request" and (classifier_values["ci_review"] == "true" or classifier_values["mcp_catalog"] == "true")
        else "skipped"
    }
    # This job is intentionally disabled in the current workflow and is an
    # explicit applicability exception, not a blanket skipped-is-green rule.
    result["e2e-desktop"] = {"result": "skipped"}
    result["osv-scanner"] = {"result": "success"}
    return result


def run(values: dict[str, str] | None = None, *, event: str = "pull_request", jobs=None):
    values = values or classifier()
    return evaluate(event_name=event, classifier_outputs=values, needs=jobs or jobs_for(values, event=event))


def test_required_success_and_explicit_non_applicable_skip_pass():
    values = classifier(python="true", python_prod="true", frontend="true")
    result = run(values)
    assert result["status"] == "PASS"
    assert result["required_jobs"]
    assert "e2e-desktop" in result["explicit_exceptions"]


@pytest.mark.parametrize("bad_result", ["skipped", "cancelled", "timed_out", "action_required", "failure", "neutral"])
def test_unexpected_or_non_success_result_fails_closed(bad_result: str):
    values = classifier(python="true")
    jobs = jobs_for(values)
    jobs["tests"] = {"result": bad_result}
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any("tests" in failure for failure in result["failures"])


def test_explicit_non_applicable_skip_is_allowed_but_missing_job_is_not():
    values = classifier()
    jobs = jobs_for(values)
    assert run(values, jobs=jobs)["status"] == "PASS"
    del jobs["js-tests"]
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any("js-tests" in failure for failure in result["failures"])


@pytest.mark.parametrize(
    "mutator",
    [
        lambda jobs: jobs.pop("tests"),
        lambda jobs: jobs.__setitem__("tests", None),
        lambda jobs: jobs.__setitem__("tests", {"conclusion": "success"}),
        lambda jobs: jobs.__setitem__("tests", {"result": None}),
        lambda jobs: jobs.__setitem__("unknown-job", {"result": "success"}),
    ],
)
def test_missing_null_unknown_and_wrong_result_shapes_fail(mutator):
    values = classifier(python="true")
    jobs = jobs_for(values)
    mutator(jobs)
    assert run(values, jobs=jobs)["status"] == "FAIL"


def test_classifier_failure_or_missing_output_cannot_make_all_lanes_optional():
    values = classifier(python="not-a-boolean")
    assert run(values)["status"] == "FAIL"

    values = classifier()
    jobs = jobs_for(values)
    values.pop("rust")
    assert run(values, jobs=jobs)["status"] == "FAIL"


def test_classifier_and_needs_are_copied_before_evaluation():
    values = classifier(python="true")
    jobs = jobs_for(values)
    original_values = deepcopy(values)
    original_jobs = deepcopy(jobs)
    run(values, jobs=jobs)
    assert values == original_values
    assert jobs == original_jobs


def test_disabled_desktop_lane_must_remain_an_explicit_skip():
    values = classifier()
    jobs = jobs_for(values)
    jobs["e2e-desktop"] = {"result": "success"}
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
