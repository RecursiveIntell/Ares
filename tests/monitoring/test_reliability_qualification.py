"""Qualification consistency is not an effect grant or document PASS."""

import copy
import hashlib
from pathlib import Path
import shutil
import pytest
from scripts.ci.reliability_qualification import validate_dag, validate_qualification
from tests.monitoring.test_reliability_case_bindings import executed_fixture  # noqa: F401

PLAN = "a" * 64
CANDIDATE = "b" * 64


@pytest.fixture
def example(tmp_path, executed_fixture):
    base, relative, file, nodes = executed_fixture
    shutil.copytree(base, tmp_path, dirs_exist_ok=True)
    attempt = {"path": relative,
               "sha256": hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()}
    evidence = tmp_path / "actual-result.log"
    evidence.write_bytes(b"fixture-result\n")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    dag = {
        "plan_sha256": PLAN,
        "task_count": 2,
        "case_count": 2,
        "gate_count": 2,
        "gate_aggregates": {"G0": ["0.1"], "G1": ["1.1"]},
        "case_subchecks": {"A01": ["source"], "A02": ["source"]},
        "tasks": [
            {"id": "0.1", "depends_on": [], "cases": ["A01"], "issues": ["R01"]},
            {"id": "1.1", "depends_on": ["0.1"], "cases": ["A02"], "issues": ["R02"]},
        ],
    }
    receipt = {
        "schema": "AresReliabilityQualificationV2",
        "plan_sha256": PLAN,
        "candidate_sha256": CANDIDATE,
        "tasks": {
            task["id"]: {
                "status": "passed",
                "candidate_sha256": CANDIDATE,
                "cases": {case: "passed" for case in task["cases"]},
                "case_evidence": {case: {"source": [{
                    "candidate_sha256": CANDIDATE, "export_root": "export", "attempt": attempt,
                    "file": file, "classname": nodes["test_source_binding"], "name": "test_source_binding",
                }]} for case in task["cases"]},
                "evidence": [{"path": "actual-result.log", "sha256": digest}],
            }
            for task in dag["tasks"]
        },
        "gates": {
            gate: {
                "status": "passed",
                "candidate_sha256": CANDIDATE,
                "tasks": list(members),
            }
            for gate, members in dag["gate_aggregates"].items()
        },
        "issues": {"R01": {"status": "repaired"}, "R02": {"status": "repaired"}},
        "all_fixed": True,
    }
    return dag, receipt, tmp_path


def check(example):
    dag, receipt, root = example
    return validate_qualification(
        dag,
        receipt,
        expected_plan_sha256=PLAN,
        expected_candidate_sha256=CANDIDATE,
        artifact_root=root,
    )


def test_complete_exact_evidence_fixture_is_consistent(example):
    assert check(example) == []


@pytest.mark.parametrize(
    "mutation, error",
    [
        (lambda d: d["tasks"][0]["depends_on"].append("1.1"), "DAG_CYCLE"),
        (
            lambda d: d["tasks"][1]["depends_on"].append("missing"),
            "DAG_DEPENDENCY_UNKNOWN",
        ),
        (
            lambda d: d["tasks"].append(copy.deepcopy(d["tasks"][0])),
            "DAG_TASK_DUPLICATE",
        ),
        (lambda d: d["tasks"][1]["cases"].append("A01"), "DAG_CASE_DUPLICATE"),
        (lambda d: d.update(task_count=3), "DAG_DECLARED_TOTAL_MISMATCH"),
        (lambda d: d["gate_aggregates"].pop("G1"), "DAG_GATE_COVERAGE_INVALID"),
    ],
)
def test_invalid_dag_is_rejected(example, mutation, error):
    dag, _, _ = example
    mutation(dag)
    assert error in validate_dag(dag)


def with_gate_dependencies(dag, deps):
    dag["gate_dependencies"] = deps
    return dag


@pytest.mark.parametrize(
    "deps, error",
    [
        ({"G0": ["G0"], "G1": ["G0"]}, "DAG_GATE_CYCLE"),
        ({"G0": ["G1"], "G1": ["G0"]}, "DAG_GATE_CYCLE"),
        ({"G0": [], "G1": ["G0", "G0"]}, "DAG_GATE_DEPENDENCY_INVALID"),
        ({"G0": [], "G1": ["GX"]}, "DAG_GATE_DEPENDENCY_UNKNOWN"),
        ({"G0": []}, "DAG_GATE_DEPENDENCY_COVERAGE_INVALID"),
        ({"G0": [], "G1": [], "GX": []}, "DAG_GATE_DEPENDENCY_COVERAGE_INVALID"),
        ({"G0": [], "G1": "G0"}, "DAG_GATE_DEPENDENCY_INVALID"),
        (["G0", "G1"], "DAG_GATE_DEPENDENCY_INVALID"),
    ],
)
def test_cyclic_or_unknown_gate_readiness_is_rejected(example, deps, error):
    dag, _, _ = example
    assert error in validate_dag(with_gate_dependencies(dag, deps))


def test_acyclic_gate_dependencies_and_their_absence_are_both_accepted(example):
    dag, receipt, root = example
    assert validate_dag(dag) == []                      # absent: unchanged legacy shape
    assert validate_dag(with_gate_dependencies(dag, {"G0": [], "G1": ["G0"]})) == []
    assert check(example) == []


def test_cyclic_gate_readiness_cannot_reach_a_positive_qualification(example):
    dag, _, _ = example
    with_gate_dependencies(dag, {"G0": ["G1"], "G1": ["G0"]})
    assert "DAG_GATE_CYCLE" in check(example)


@pytest.mark.parametrize(
    "mutation, error",
    [
        (
            lambda r: r["tasks"]["1.1"].update(status="not_run"),
            "REQUIRED_TASK_NOT_PASSED",
        ),
        (
            lambda r: r["tasks"]["1.1"]["cases"].update(A02="skipped"),
            "REQUIRED_CASE_NOT_PASSED",
        ),
        (lambda r: r["tasks"]["1.1"]["cases"].clear(), "CASE_COVERAGE_MISMATCH"),
        (lambda r: r["tasks"].pop("0.1"), "TASK_COVERAGE_MISMATCH"),
        (lambda r: r.update(candidate_sha256="c" * 64), "CANDIDATE_IDENTITY_MISMATCH"),
        (lambda r: r.update(plan_sha256="c" * 64), "PLAN_IDENTITY_MISMATCH"),
        (lambda r: r["tasks"]["0.1"].update(evidence=[]), "EVIDENCE_MISSING"),
        (
            lambda r: r["tasks"]["0.1"]["evidence"][0].update(sha256="c" * 64),
            "EVIDENCE_DIGEST_MISMATCH",
        ),
        (
            lambda r: r["tasks"]["0.1"]["evidence"][0].update(
                sha256="sha256:" + "a" * 64
            ),
            "EVIDENCE_IDENTITY_INVALID",
        ),
        (
            lambda r: r["tasks"]["0.1"]["evidence"][0].update(
                path="../actual-result.log"
            ),
            "EVIDENCE_PATH_OUTSIDE_ROOT",
        ),
        (
            lambda r: r["tasks"]["0.1"]["evidence"][0].update(path="/etc/shadow"),
            "EVIDENCE_PATH_OUTSIDE_ROOT",
        ),
        (
            lambda r: r["tasks"]["0.1"]["evidence"][0].update(path="missing.log"),
            "EVIDENCE_UNREADABLE",
        ),
        (lambda r: r["issues"].pop("R02"), "ISSUE_COVERAGE_MISMATCH"),
        (
            lambda r: r["issues"]["R02"].update(
                status="deferred", owner="operator", reason="authorization needed"
            ),
            "ALL_FIXED_CONTRADICTS_DISPOSITION",
        ),
        (
            lambda r: r["issues"]["R02"].update(
                status="unknown", owner="operator", reason="not tested"
            ),
            "REQUIRED_ISSUE_UNRESOLVED",
        ),
        (lambda r: r.update(document_only=True), "DOCUMENT_IS_NOT_IMPLEMENTATION"),
    ],
)
def test_invalid_qualification_cannot_be_positive(example, mutation, error):
    _, receipt, _ = example
    mutation(receipt)
    assert error in check(example)


def test_deferred_optional_service_is_not_called_fixed(example):
    dag, receipt, root = example
    receipt["all_fixed"] = False
    receipt["issues"]["R02"] = {
        "status": "deferred",
        "required": False,
        "owner": "operator",
        "reason": "credential action",
    }
    assert (
        validate_qualification(
            dag,
            receipt,
            expected_plan_sha256=PLAN,
            expected_candidate_sha256=CANDIDATE,
            artifact_root=root,
            optional_issue_ids=frozenset({"R02"}),
        )
        == []
    )


@pytest.mark.parametrize("status", ["deferred", "blocked", "unknown"])
def test_receipt_cannot_grant_its_own_issue_waiver(example, status):
    _, receipt, _ = example
    receipt["all_fixed"] = False
    receipt["issues"]["R02"] = {
        "status": status,
        "required": False,
        "owner": "operator",
        "reason": "self-declared waiver",
    }
    assert "UNAPPROVED_ISSUE_WAIVER" in check(example)


def test_symlink_cannot_escape_evidence_root(example, tmp_path):
    _, receipt, root = example
    outside = tmp_path.parent / (tmp_path.name + "-outside.log")
    outside.write_bytes(b"outside")
    (root / "link.log").symlink_to(outside)
    receipt["tasks"]["0.1"]["evidence"][0] = {
        "path": "link.log",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    assert "EVIDENCE_PATH_OUTSIDE_ROOT" in check(example)


@pytest.mark.parametrize("status", ["failed", "blocked", "not_run", "skipped", "deferred", None, True])
def test_required_gate_cannot_disagree_with_passing_task_records(example, status):
    _, receipt, _ = example
    receipt["gates"]["G1"]["status"] = status
    assert "REQUIRED_GATE_NOT_PASSED" in check(example)


@pytest.mark.parametrize("mutation, error", [
    (lambda r: r.pop("gates"), "GATE_COVERAGE_MISMATCH"),
    (lambda r: r["gates"].pop("G1"), "GATE_COVERAGE_MISMATCH"),
    (lambda r: r["gates"].update(unknown={}), "GATE_COVERAGE_MISMATCH"),
    (lambda r: r["gates"].update(G1="passed"), "GATE_RECORD_INVALID"),
    (lambda r: r["gates"]["G1"].update(candidate_sha256="c" * 64), "GATE_CANDIDATE_MISMATCH"),
    (lambda r: r["gates"]["G1"].update(tasks=[]), "GATE_TASK_BINDING_MISMATCH"),
    (lambda r: r["gates"]["G1"].update(tasks=["0.1"]), "GATE_TASK_BINDING_MISMATCH"),
    (lambda r: r["gates"]["G1"].update(tasks=["1.1", "1.1"]), "GATE_TASK_BINDING_MISMATCH"),
])
def test_missing_or_rebound_gate_cannot_qualify(example, mutation, error):
    _, receipt, _ = example
    mutation(receipt)
    assert error in check(example)


def test_gate_positive_cannot_hide_failed_aggregate_member(example):
    _, receipt, _ = example
    receipt["tasks"]["1.1"]["status"] = "failed"
    assert "GATE_AGGREGATE_UNSATISFIED" in check(example)
