#!/usr/bin/env python3
"""Fail-closed evaluation of the Ares GitHub Actions needs graph.

The workflow passes GitHub's ``needs`` object and the trusted classifier outputs
as JSON. This module deliberately evaluates ``needs.result`` rather than REST
check conclusions; those are different contracts and are handled separately.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

SCHEMA = "AresRequiredChecksEvaluationV1"
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
CLASSIFIER_BOOLEAN_KEYS = frozenset(CLASSIFIER_KEYS) - {"ci_review_files"}

# This is the complete job map wired into the current ci.yaml. Every entry must
# be present in needs, including a conditionally skipped job. Presence is part
# of the aggregate contract; silently missing a job is not non-applicability.
KNOWN_JOBS = frozenset(
    {
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
)

# The current workflow intentionally disables this lane with a literal false.
# It is an explicit, reviewable exception, not a general skipped-is-green rule.
DISABLED_JOBS = frozenset({"e2e-desktop"})
VALID_RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})


def _failure(message: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "FAIL",
        "required_jobs": [],
        "explicit_exceptions": [],
        "failures": [message],
    }


def _classifier_bool(values: Mapping[str, Any], key: str) -> bool:
    if key not in values:
        raise ValueError(f"classifier output missing: {key}")
    value = values[key]
    if value not in ("true", "false"):
        raise ValueError(f"classifier output {key} is not true/false")
    return value == "true"


def _job_result(needs: Mapping[str, Any], job: str) -> str:
    if job not in needs:
        raise ValueError(f"mandatory job missing from needs: {job}")
    info = needs[job]
    if not isinstance(info, Mapping) or "result" not in info:
        raise ValueError(f"job {job} has no needs.result")
    result = info["result"]
    if not isinstance(result, str) or result not in VALID_RESULTS:
        raise ValueError(f"job {job} has unsupported needs.result: {result!r}")
    return result


def _output_bool(needs: Mapping[str, Any], job: str, key: str) -> bool:
    info = needs.get(job)
    if not isinstance(info, Mapping):
        return False
    outputs = info.get("outputs")
    if not isinstance(outputs, Mapping):
        return False
    return outputs.get(key) == "true"


def _applicable(event_name: str, flags: Mapping[str, bool], needs: Mapping[str, Any], job: str) -> bool:
    if job in DISABLED_JOBS or job in {"detect", "osv-scanner"}:
        return True
    if job in {"tests", "tests-os", "lint", "contributor-check"}:
        return flags["python"]
    if job == "js-tests":
        return flags["frontend"]
    if job == "installer-tests":
        return flags["installer"]
    if job == "rust-tests":
        return flags["rust"]
    if job == "docs-site":
        return flags["site"]
    if job == "history-check":
        return event_name == "pull_request"
    if job == "uv-lockfile":
        return flags["uv_lock"]
    if job == "lockfile-diff":
        return event_name == "pull_request" and flags["npm_lock"]
    if job == "docker-lint":
        return flags["docker_meta"]
    if job == "supply-chain":
        return event_name == "pull_request" and (flags["scan"] or flags["deps"])
    if job == "review-labels":
        return event_name == "pull_request" and (
            flags["ci_review"]
            or flags["mcp_catalog"]
            or _output_bool(needs, "supply-chain", "critical_findings")
        )
    raise ValueError(f"no applicability rule for known job: {job}")


def evaluate(
    *,
    event_name: str,
    classifier_outputs: Mapping[str, Any],
    needs: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a complete workflow needs graph without mutating its inputs."""
    failures: list[str] = []
    if not isinstance(event_name, str) or event_name not in {"pull_request", "push", "workflow_dispatch"}:
        failures.append(f"unsupported event_name: {event_name!r}")
    if not isinstance(classifier_outputs, Mapping):
        failures.append("classifier outputs must be an object")
        flags: dict[str, bool] = {}
    else:
        flags = {}
        for key in CLASSIFIER_KEYS:
            try:
                if key in CLASSIFIER_BOOLEAN_KEYS:
                    flags[key] = _classifier_bool(classifier_outputs, key)
                else:
                    if key not in classifier_outputs or not isinstance(classifier_outputs[key], str):
                        raise ValueError(f"classifier output {key} is not a string")
                    # This output is diagnostic metadata; it does not control
                    # applicability, but it must still be present and typed.
                    flags[key] = False
            except ValueError as exc:
                failures.append(str(exc))
    if not isinstance(needs, Mapping):
        failures.append("needs graph must be an object")
        needs_map: Mapping[str, Any] = {}
    else:
        needs_map = needs

    unknown = sorted(set(needs_map) - KNOWN_JOBS)
    if unknown:
        failures.append("unknown jobs in needs: " + ", ".join(unknown))

    required_jobs: list[str] = []
    explicit_exceptions: list[str] = []
    for job in sorted(KNOWN_JOBS):
        try:
            result = _job_result(needs_map, job)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        if job in DISABLED_JOBS:
            explicit_exceptions.append(job)
            if result != "skipped":
                failures.append(f"disabled job {job} must be skipped, got {result}")
            continue
        if job == "detect":
            required_jobs.append(job)
            if result != "success":
                failures.append(f"classifier job detect must succeed, got {result}")
            continue
        try:
            applies = _applicable(event_name, flags, needs_map, job)
        except (KeyError, ValueError) as exc:
            failures.append(str(exc))
            continue
        if applies:
            required_jobs.append(job)
            if result != "success":
                failures.append(f"required job {job} must succeed, got {result}")
        elif result != "skipped":
            failures.append(f"non-applicable job {job} must be explicitly skipped, got {result}")

    return {
        "schema": SCHEMA,
        "status": "PASS" if not failures else "FAIL",
        "event_name": event_name,
        "required_jobs": required_jobs,
        "explicit_exceptions": explicit_exceptions,
        "failures": failures,
    }


def _load_json(raw: str, name: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--needs-json", default=os.environ.get("NEEDS_JSON"))
    parser.add_argument("--classifier-json", default=os.environ.get("CLASSIFIER_JSON"))
    parser.add_argument("--event", default=os.environ.get("EVENT_NAME"))
    args = parser.parse_args(argv)
    try:
        if args.needs_json is None or args.classifier_json is None or args.event is None:
            raise ValueError("NEEDS_JSON, CLASSIFIER_JSON, and EVENT_NAME are required")
        result = evaluate(
            event_name=args.event,
            classifier_outputs=_load_json(args.classifier_json, "classifier JSON"),
            needs=_load_json(args.needs_json, "needs JSON"),
        )
        try:
            needs_value = _load_json(args.needs_json, "needs JSON")
            if isinstance(needs_value, Mapping):
                compact = {
                    name: info["result"]
                    for name, info in sorted(needs_value.items())
                    if isinstance(info, Mapping) and isinstance(info.get("result"), str)
                }
                output_path = os.environ.get("GITHUB_OUTPUT")
                if output_path:
                    with open(output_path, "a", encoding="utf-8") as output:
                        output.write("needs-json=" + json.dumps(compact, separators=(",", ":")) + chr(10))
        except (OSError, ValueError):
            # A missing output file must not turn a failed aggregate into green;
            # the evaluator result remains the authoritative exit status.
            pass
    except ValueError as exc:
        result = _failure(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
