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
    "context_continuity",
    "current_owner_integration",
)


def classifier(**overrides: str) -> dict[str, str]:
    result = {key: "false" for key in CLASSIFIER_KEYS}
    result["ci_review_files"] = ""
    result.update(overrides)
    return result


NATIVE_CALLS = (
    ("context-continuity", "context_continuity", ("native_external_owner_result", "focused_tests_result")),
    ("current-owner-integration", "current_owner_integration", ("profile_runtime_consumer_result",)),
)


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
    result["infographic-check"] = {"result": "success"}
    supply_chain_applies = event == "pull_request" and (
        classifier_values["scan"] == "true" or classifier_values["deps"] == "true"
    )
    result["supply-chain"] = {
        "result": "success" if supply_chain_applies else "skipped",
        "outputs": {"critical_findings": "false"} if supply_chain_applies else {},
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
    for job, lane, outputs in NATIVE_CALLS:
        applies = event != "pull_request" or classifier_values[lane] == "true"
        result[job] = {
            "result": "success" if applies else "skipped",
            "outputs": {key: "success" for key in outputs} if applies else {},
        }
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


def test_critical_supply_chain_finding_requires_review_label_gate():
    values = classifier(scan="true")
    jobs = jobs_for(values)
    jobs["supply-chain"]["outputs"] = {"critical_findings": "true"}
    jobs["review-labels"] = {"result": "success"}
    assert run(values, jobs=jobs)["status"] == "PASS"

    jobs["review-labels"] = {"result": "skipped"}
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any("review-labels" in failure for failure in result["failures"])


@pytest.mark.parametrize(
    "outputs",
    [None, {"critical_findings": None}, {"critical_findings": "unknown"}],
)
def test_missing_or_malformed_critical_findings_fails_closed(outputs):
    values = classifier(scan="true")
    jobs = jobs_for(values)
    if outputs is None:
        jobs["supply-chain"].pop("outputs")
    else:
        jobs["supply-chain"]["outputs"] = outputs
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any("critical_findings" in failure for failure in result["failures"])


@pytest.mark.parametrize("lanes", [
    {"context_continuity": "true"},
    {"current_owner_integration": "true"},
    {"context_continuity": "true", "current_owner_integration": "true"},
])
def test_applicable_native_call_and_each_inner_owner_success_pass(lanes):
    values = classifier(**lanes)
    result = run(values)
    assert result["status"] == "PASS"
    for job, lane, _ in NATIVE_CALLS:
        assert (job in result["required_jobs"]) == (values[lane] == "true")


def test_applicable_native_calls_missing_from_existing_graph_fail_closed():
    values = classifier(context_continuity="true", current_owner_integration="true")
    jobs = jobs_for(values)
    for job, _, _ in NATIVE_CALLS:
        jobs.pop(job)
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert all(any(job in failure for failure in result["failures"]) for job, _, _ in NATIVE_CALLS)


@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
@pytest.mark.parametrize("bad_result", ["failure", "cancelled", "skipped", "neutral", "", None, True])
def test_applicable_native_caller_non_success_fails_closed(job, lane, outputs, bad_result):
    values = classifier(**{lane: "true"})
    jobs = jobs_for(values)
    jobs[job]["result"] = bad_result
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any(job in failure for failure in result["failures"])


@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
@pytest.mark.parametrize("shape", ["missing", "null", "no-result", "wrong-result-key"])
def test_applicable_native_caller_missing_or_malformed_fails_closed(job, lane, outputs, shape):
    values = classifier(**{lane: "true"})
    jobs = jobs_for(values)
    if shape == "missing":
        jobs.pop(job)
    elif shape == "null":
        jobs[job] = None
    elif shape == "no-result":
        jobs[job] = {}
    else:
        jobs[job] = {"conclusion": "success"}
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("job,lane,key", [
    (job, lane, key) for job, lane, outputs in NATIVE_CALLS for key in outputs
])
@pytest.mark.parametrize("bad_result", ["failure", "cancelled", "skipped", "neutral", "", None, True, {}, []])
def test_successful_call_cannot_hide_non_success_inner_owner(job, lane, key, bad_result):
    values = classifier(**{lane: "true"})
    jobs = jobs_for(values)
    jobs[job]["outputs"][key] = bad_result
    result = run(values, jobs=jobs)
    assert result["status"] == "FAIL"
    assert any(key in failure for failure in result["failures"])


@pytest.mark.parametrize("job,lane,key", [
    (job, lane, key) for job, lane, outputs in NATIVE_CALLS for key in outputs
])
def test_successful_call_cannot_hide_missing_inner_owner_result(job, lane, key):
    values = classifier(**{lane: "true"})
    jobs = jobs_for(values)
    del jobs[job]["outputs"][key]
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
@pytest.mark.parametrize("shape", [None, "", [], {}])
def test_applicable_call_requires_native_output_object_and_bindings(job, lane, outputs, shape):
    values = classifier(**{lane: "true"})
    jobs = jobs_for(values)
    jobs[job]["outputs"] = shape
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
def test_non_applicable_native_call_must_be_present_and_skipped(job, lane, outputs):
    values = classifier()
    jobs = jobs_for(values)
    assert jobs[job]["result"] == "skipped"
    jobs[job].pop("outputs")
    assert run(values, jobs=jobs)["status"] == "PASS"
    jobs.pop(job)
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
@pytest.mark.parametrize("bad_result", ["success", "failure", "cancelled"])
def test_non_applicable_native_call_rejects_unexpected_result(job, lane, outputs, bad_result):
    values = classifier()
    jobs = jobs_for(values)
    jobs[job]["result"] = bad_result
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize("job,lane,outputs", NATIVE_CALLS)
def test_postmerge_and_dispatch_require_native_owners_even_with_false_flags(event, job, lane, outputs):
    values = classifier()
    jobs = jobs_for(values, event=event)
    assert run(values, event=event, jobs=jobs)["status"] == "PASS"
    jobs[job] = {"result": "skipped"}
    assert run(values, event=event, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("lane", ["context_continuity", "current_owner_integration"])
@pytest.mark.parametrize("bad_value", ["missing", "", "unknown", None, True])
def test_native_classifier_missing_or_malformed_cannot_authorize_skip(lane, bad_value):
    values = classifier()
    jobs = jobs_for(values)
    if bad_value == "missing":
        del values[lane]
    else:
        values[lane] = bad_value
    assert run(values, jobs=jobs)["status"] == "FAIL"


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_failed_detect_cannot_authorize_native_optional_results(result):
    values = classifier()
    jobs = jobs_for(values)
    jobs["detect"]["result"] = result
    assert run(values, jobs=jobs)["status"] == "FAIL"
