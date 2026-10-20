"""Read-only plan/evidence consistency checks, never effect authorization.

The approved DAG and candidate identity are supplied by their canonical owners.
Passing this checker does not prove command provenance or authorize cutover.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET
from collections.abc import Mapping

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_ATTEMPT_FIELDS = {
    "schema", "attempt", "attempt_completed", "command", "cwd", "effect_authorization",
    "file", "junit", "junit_error", "parsed_summary", "pytest_return_code",
    "runner_return_code", "scope", "stdout", "stdout_encoding", "test_sha256_after",
    "test_sha256_before", "timed_out", "wall_seconds",
}
_BINDING_FIELDS = {"candidate_sha256", "export_root", "attempt", "file", "classname", "name"}


def _inside_root(root: Path, name: str) -> Path | None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    try:
        resolved = (root / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(root) else None


def _read_evidence(root: Path, reference: Mapping, errors: list[str], *, scope: Path | None = None) -> bytes | None:
    if (not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}
            or not isinstance(reference.get("path"), str) or not reference["path"]
            or not isinstance(reference.get("sha256"), str)
            or not _HASH.fullmatch(reference["sha256"])):
        errors.append("EVIDENCE_IDENTITY_INVALID")
        return None
    path = _inside_root(root, reference["path"])
    if path is None or scope is not None and not path.is_relative_to(scope):
        errors.append("EVIDENCE_PATH_OUTSIDE_ROOT")
        return None
    try:
        # Reject nonregular artifacts without blocking on a substituted FIFO.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                errors.append("EVIDENCE_UNREADABLE")
                return None
            if info.st_size > _MAX_ARTIFACT_BYTES:
                errors.append("EVIDENCE_TOO_LARGE")
                return None
            raw = stream.read(_MAX_ARTIFACT_BYTES + 1)
    except (OSError, ValueError):
        errors.append("EVIDENCE_UNREADABLE")
        return None
    if len(raw) > _MAX_ARTIFACT_BYTES:
        errors.append("EVIDENCE_TOO_LARGE")
        return None
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        errors.append("EVIDENCE_DIGEST_MISMATCH")
        return None
    return raw


def _finite_wall_seconds(value) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _validate_test_binding(root: Path, binding: Mapping, candidate: str, errors: list[str]) -> None:
    if (not isinstance(binding, Mapping) or set(binding) != _BINDING_FIELDS
            or any(not isinstance(binding.get(k), str) or not binding[k]
                   for k in ("file", "classname", "name", "export_root"))):
        errors.append("CASE_TEST_BINDING_INVALID")
        return
    if binding["candidate_sha256"] != candidate:
        errors.append("CASE_CANDIDATE_MISMATCH")
        return
    export = _inside_root(root, binding["export_root"])
    if export is None:
        errors.append("EVIDENCE_PATH_OUTSIDE_ROOT")
        return
    raw = _read_evidence(root, binding["attempt"], errors, scope=export)
    if raw is None:
        return

    try:
        attempt = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        errors.append("CASE_ATTEMPT_SCHEMA_INVALID")
        return
    if (not isinstance(attempt, Mapping) or set(attempt) != _ATTEMPT_FIELDS
            or attempt["schema"] != "hermes.test-attempt-evidence/v1"
            or not isinstance(attempt["command"], list) or not attempt["command"]
            or any(not isinstance(x, str) for x in attempt["command"])
            or any(not isinstance(attempt[k], str) or not attempt[k]
                   for k in ("file", "cwd", "scope", "stdout_encoding"))
            or not isinstance(attempt["parsed_summary"], Mapping)
            or not _finite_wall_seconds(attempt["wall_seconds"])
            or attempt["effect_authorization"] is not False):
        errors.append("CASE_ATTEMPT_SCHEMA_INVALID")
        return
    if attempt["file"] != binding["file"]:
        errors.append("CASE_TEST_FILE_MISMATCH")
        return
    if type(attempt["attempt"]) is not int or attempt["attempt"] != 1:
        errors.append("CASE_RETRY_EVIDENCE_NOT_QUALIFIED")
    if (any(type(attempt[k]) is not int or attempt[k] != 0
            for k in ("pytest_return_code", "runner_return_code"))
            or attempt["timed_out"] is not False or attempt["attempt_completed"] is not True
            or attempt["junit_error"] is not None):
        errors.append("CASE_ATTEMPT_NOT_PASSED")
    if (any(not isinstance(attempt[k], str) or not _HASH.fullmatch(attempt[k])
            for k in ("test_sha256_before", "test_sha256_after"))
            or attempt["test_sha256_before"] != attempt["test_sha256_after"]):
        errors.append("CASE_TEST_SOURCE_DRIFT")
    _read_evidence(export, attempt["stdout"], errors)
    xml = _read_evidence(export, attempt["junit"], errors)
    if xml is None:
        return
    try:
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError("DTD is not a runner JUnit artifact")
        tree = ET.fromstring(xml)
        if tree.tag not in {"testsuites", "testsuite"}:
            raise ValueError("unknown JUnit root")
    except (ET.ParseError, ValueError):
        errors.append("CASE_JUNIT_SCHEMA_INVALID")
        return
    matches = [node for node in tree.iter("testcase")
               if node.get("classname") == binding["classname"] and node.get("name") == binding["name"]]
    if not matches:
        errors.append("CASE_TEST_NODE_MISSING")
    elif len(matches) != 1:
        errors.append("CASE_TEST_NODE_AMBIGUOUS")
    elif any(child.tag in {"failure", "error", "skipped"} for child in matches[0]):
        errors.append("CASE_TEST_NODE_NOT_PASSED")
    elif any(child.tag not in {"properties", "system-out", "system-err"} for child in matches[0]):
        errors.append("CASE_JUNIT_SCHEMA_INVALID")


def _validate_case_evidence(dag: Mapping, task: Mapping, record: Mapping, root: Path,
                            candidate: str, errors: list[str]) -> None:
    evidence = record.get("case_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(task["cases"]):
        errors.append("CASE_EVIDENCE_COVERAGE_MISMATCH")
        return
    for case in task["cases"]:
        declared = evidence[case]
        if not isinstance(declared, Mapping) or set(declared) != set(dag["case_subchecks"][case]):
            errors.append("SUBCHECK_EVIDENCE_COVERAGE_MISMATCH")
            continue
        for bindings in declared.values():
            if not isinstance(bindings, list) or not bindings:
                errors.append("SUBCHECK_EVIDENCE_MISSING")
                continue
            seen = set()
            for binding in bindings:
                if (isinstance(binding, Mapping)
                        and all(isinstance(binding.get(k), str) for k in ("file", "classname", "name"))):
                    key = (binding["file"], binding["classname"], binding["name"])
                    if key in seen:
                        errors.append("CASE_TEST_BINDING_DUPLICATE")
                    seen.add(key)
                _validate_test_binding(root, binding, candidate, errors)


def validate_dag(dag: Mapping) -> list[str]:
    errors: list[str] = []
    tasks = dag.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return ["DAG_TASKS_MISSING"]
    by_id: dict[str, Mapping] = {}
    cases: set[str] = set()
    for task in tasks:
        if not isinstance(task, Mapping) or not isinstance(task.get("id"), str):
            errors.append("DAG_TASK_INVALID")
            continue
        task_id = task["id"]
        if task_id in by_id:
            errors.append("DAG_TASK_DUPLICATE")
        by_id[task_id] = task
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or any(
            not isinstance(x, str) for x in dependencies
        ):
            errors.append("DAG_DEPENDENCIES_INVALID")
        elif len(dependencies) != len(set(dependencies)):
            errors.append("DAG_DEPENDENCY_DUPLICATE")
        declared = task.get("cases")
        if (
            not isinstance(declared, list)
            or not declared
            or any(not isinstance(x, str) for x in declared)
        ):
            errors.append("DAG_CASES_INVALID")
        else:
            for case in declared:
                if case in cases:
                    errors.append("DAG_CASE_DUPLICATE")
                cases.add(case)
    if errors:
        return errors
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append("DAG_CYCLE")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id]["depends_on"]:
            if dependency not in by_id:
                errors.append("DAG_DEPENDENCY_UNKNOWN")
            else:
                visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
    if dag.get("task_count") != len(tasks) or dag.get("case_count") != len(cases):
        errors.append("DAG_DECLARED_TOTAL_MISMATCH")
    subchecks = dag.get("case_subchecks")
    if not isinstance(subchecks, Mapping) or set(subchecks) != cases:
        errors.append("DAG_SUBCHECK_COVERAGE_INVALID")
    else:
        for values in subchecks.values():
            if (not isinstance(values, list) or not values
                    or any(not isinstance(x, str) or not x for x in values)
                    or len(values) != len(set(values))):
                errors.append("DAG_SUBCHECKS_INVALID")
    aggregates = dag.get("gate_aggregates")
    if not isinstance(aggregates, Mapping) or not aggregates:
        errors.append("DAG_GATES_MISSING")
    else:
        members: list[str] = []
        for group in aggregates.values():
            if not isinstance(group, list) or any(
                not isinstance(x, str) for x in group
            ):
                errors.append("DAG_GATE_INVALID")
            else:
                members.extend(group)
        if set(members) != set(by_id) or len(members) != len(set(members)):
            errors.append("DAG_GATE_COVERAGE_INVALID")
        if dag.get("gate_count") != len(aggregates):
            errors.append("DAG_GATE_TOTAL_MISMATCH")
        if "gate_dependencies" in dag:
            errors.extend(_validate_gate_dependencies(dag["gate_dependencies"], set(aggregates)))
    return sorted(set(errors))


def _validate_gate_dependencies(deps, gates: set[str]) -> list[str]:
    """Gate readiness is owned by the approved DAG; reject cyclic or unbound edges.

    Optional so existing approved DAGs without the field keep their meaning. When the
    owner supplies it, it must describe every gate exactly once, name only known gates,
    and be acyclic (a gate that is its own prerequisite can never be ready).
    """
    if not isinstance(deps, Mapping):
        return ["DAG_GATE_DEPENDENCY_INVALID"]
    errors: list[str] = []
    if set(deps) != gates:
        errors.append("DAG_GATE_DEPENDENCY_COVERAGE_INVALID")
    edges: dict[str, list[str]] = {}
    for gate, group in deps.items():
        if (not isinstance(group, list) or any(not isinstance(x, str) for x in group)
                or len(group) != len(set(group))):
            errors.append("DAG_GATE_DEPENDENCY_INVALID")
            continue
        if any(x not in gates for x in group):
            errors.append("DAG_GATE_DEPENDENCY_UNKNOWN")
            continue
        edges[gate] = group
    if errors:
        return errors
    state: dict[str, int] = {}

    def visit(gate: str) -> bool:
        if state.get(gate) == 1:
            return False
        if state.get(gate) == 2:
            return True
        state[gate] = 1
        ok = all(visit(dependency) for dependency in edges[gate])
        state[gate] = 2
        return ok

    if not all(visit(gate) for gate in edges):
        errors.append("DAG_GATE_CYCLE")
    return errors


def validate_qualification(
    dag: Mapping,
    receipt: Mapping,
    *,
    expected_plan_sha256: str,
    expected_candidate_sha256: str,
    artifact_root: Path,
    optional_issue_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Check V2 statuses and literal pytest-backed subchecks, not grants.

    Caller must bind this DAG to its approved digest before invoking. Unknown,
    skipped or failed required evidence cannot be turned into a positive result.
    """
    errors = validate_dag(dag)
    if errors:
        return errors
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "AresReliabilityQualificationV2":
        return ["QUALIFICATION_SCHEMA_INVALID"]
    if not isinstance(expected_plan_sha256, str) or not isinstance(
        expected_candidate_sha256, str
    ):
        return ["EXPECTED_IDENTITY_INVALID"]
    if not _HASH.fullmatch(expected_plan_sha256) or not _HASH.fullmatch(
        expected_candidate_sha256
    ):
        return ["EXPECTED_IDENTITY_INVALID"]
    if (
        dag.get("plan_sha256") != expected_plan_sha256
        or receipt.get("plan_sha256") != expected_plan_sha256
    ):
        errors.append("PLAN_IDENTITY_MISMATCH")
    if receipt.get("candidate_sha256") != expected_candidate_sha256:
        errors.append("CANDIDATE_IDENTITY_MISMATCH")
    records = receipt.get("tasks")
    if not isinstance(records, Mapping) or set(records) != {
        t["id"] for t in dag["tasks"]
    }:
        return sorted(set(errors + ["TASK_COVERAGE_MISMATCH"]))
    root = artifact_root.resolve()
    gates = receipt.get("gates")
    aggregates = dag["gate_aggregates"]
    if not isinstance(gates, Mapping) or set(gates) != set(aggregates):
        errors.append("GATE_COVERAGE_MISMATCH")
    else:
        for gate_id, members in aggregates.items():
            gate = gates[gate_id]
            if not isinstance(gate, Mapping):
                errors.append("GATE_RECORD_INVALID")
                continue
            if gate.get("status") != "passed":
                errors.append("REQUIRED_GATE_NOT_PASSED")
            if gate.get("candidate_sha256") != expected_candidate_sha256:
                errors.append("GATE_CANDIDATE_MISMATCH")
            # The DAG owns aggregation; the receipt may report it, not retarget it.
            if gate.get("tasks") != members:
                errors.append("GATE_TASK_BINDING_MISMATCH")
            if any(
                not isinstance(records[task_id], Mapping)
                or records[task_id].get("status") != "passed"
                for task_id in members
            ):
                errors.append("GATE_AGGREGATE_UNSATISFIED")
    for task in dag["tasks"]:
        record = records[task["id"]]
        if not isinstance(record, Mapping):
            errors.append("TASK_RECORD_INVALID")
            continue
        if record.get("status") != "passed":
            errors.append("REQUIRED_TASK_NOT_PASSED")
        if record.get("candidate_sha256") != expected_candidate_sha256:
            errors.append("TASK_CANDIDATE_MISMATCH")
        statuses = record.get("cases")
        if not isinstance(statuses, Mapping) or set(statuses) != set(task["cases"]):
            errors.append("CASE_COVERAGE_MISMATCH")
        elif any(status != "passed" for status in statuses.values()):
            errors.append("REQUIRED_CASE_NOT_PASSED")
        _validate_case_evidence(dag, task, record, root, expected_candidate_sha256, errors)
        for dependency in task["depends_on"]:
            if (
                not isinstance(records[dependency], Mapping)
                or records[dependency].get("status") != "passed"
            ):
                errors.append("PREREQUISITE_NOT_PASSED")
        artifacts = record.get("evidence")
        if not isinstance(artifacts, list) or not artifacts:
            errors.append("EVIDENCE_MISSING")
            continue
        for artifact in artifacts:
            _read_evidence(root, artifact, errors)
    issues = receipt.get("issues")
    expected_issues = {
        issue for task in dag["tasks"] for issue in task.get("issues", [])
    }
    if not optional_issue_ids <= expected_issues:
        errors.append("OPTIONAL_ISSUE_NOT_IN_DAG")
    if not isinstance(issues, Mapping) or set(issues) != expected_issues:
        errors.append("ISSUE_COVERAGE_MISMATCH")
    else:
        for issue_id, issue in issues.items():
            if not isinstance(issue, Mapping) or issue.get("status") not in {
                "repaired",
                "not_reproduced",
                "deferred",
                "blocked",
                "unknown",
            }:
                errors.append("ISSUE_DISPOSITION_INVALID")
                continue
            if issue.get("status") != "repaired":
                if not issue.get("owner") or not issue.get("reason"):
                    errors.append("ISSUE_OWNER_REASON_MISSING")
                if receipt.get("all_fixed"):
                    errors.append("ALL_FIXED_CONTRADICTS_DISPOSITION")
            if issue.get("required") is False and issue_id not in optional_issue_ids:
                errors.append("UNAPPROVED_ISSUE_WAIVER")
            if (
                issue.get("status") in {"deferred", "blocked", "unknown"}
                and issue_id not in optional_issue_ids
            ):
                errors.append("REQUIRED_ISSUE_UNRESOLVED")
    if receipt.get("document_only"):
        errors.append("DOCUMENT_IS_NOT_IMPLEMENTATION")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dag", type=Path, required=True)
    parser.add_argument("--approved-dag-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--optional-issue",
        action="append",
        default=[],
        help="Caller-approved optional issue; never inferred from receipt",
    )
    args = parser.parse_args()
    if not _HASH.fullmatch(args.approved_dag_sha256):
        errors = ["APPROVED_DAG_IDENTITY_INVALID"]
    else:
        raw = args.dag.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.approved_dag_sha256:
            errors = ["APPROVED_DAG_IDENTITY_MISMATCH"]
        else:
            errors = validate_qualification(
                json.loads(raw),
                json.loads(args.receipt.read_text(encoding="utf-8")),
                expected_plan_sha256=args.plan_sha256,
                expected_candidate_sha256=args.candidate_sha256,
                artifact_root=args.artifact_root,
                optional_issue_ids=frozenset(args.optional_issue),
            )
    print(
        json.dumps({
            "consistency": "failed" if errors else "passed",
            "errors": errors,
            "execution_provenance_verified": False,
            "effect_authorization": False,
        })
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
