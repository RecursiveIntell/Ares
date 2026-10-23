#!/usr/bin/env python3
"""Verify a profile-panel receipt without rereading profile report contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import uuid
from pathlib import Path
from datetime import datetime, timezone

from run_panel import (
    GLOBAL_ADMISSION_POLICY,
    GLOBAL_RESOURCE_UNIT_LIMIT,
    GLOBAL_WORKER_LIMIT,
    MAX_CAPTURE_BYTES,
    MAX_RESOURCE_WEIGHT,
    PANEL_CHILD_ENVIRONMENT_POLICY,
    PROFILES,
    READONLY_REVIEW_POLICY,
    RESOURCE_WEIGHT_POLICY,
    SESSION_WORKER_LIMIT,
    VALID_REASONING_EFFORTS,
    atomic_write_manifest,
    reported_operational_block,
    runtime_revision,
    sha256_file,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fail(checks: list[dict[str, object]], name: str, detail: str) -> None:
    checks.append({"check": name, "status": "failed", "detail": detail})


def pass_check(checks: list[dict[str, object]], name: str, detail: str) -> None:
    checks.append({"check": name, "status": "passed", "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Ares profile-panel receipt artifacts")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--require-read-only-review", action="store_true")
    parser.add_argument(
        "--runtime",
        type=Path,
        default=Path.home() / ".ares" / "runtime" / "current",
    )
    args = parser.parse_args()
    receipt_dir = args.receipt.expanduser().resolve()
    panel_path = receipt_dir / "panel.json"
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        panel = json.loads(panel_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read panel.json: {exc}")
        panel = {}

    if panel.get("schema") != "AresProfilePanelReceiptV2":
        errors.append("unexpected receipt schema")
    required_profiles = panel.get("required_profiles")
    if not isinstance(required_profiles, list) or not required_profiles:
        errors.append("required profile set is empty or invalid")
        required_profiles = []
    else:
        required_set = set(required_profiles)
        canonical_subset = [profile for profile in PROFILES if profile in required_set]
        if (
            len(required_profiles) != len(required_set)
            or not required_set.issubset(PROFILES)
            or required_profiles != canonical_subset
        ):
            errors.append("required profiles are duplicated, unknown, or out of canonical order")
    results = panel.get("results")
    if not isinstance(results, list):
        errors.append("results is not a list")
        results = []
    result_profiles = [item.get("profile") for item in results if isinstance(item, dict)]
    if result_profiles != required_profiles:
        errors.append("results are missing, duplicated, or out of admitted order")

    if panel.get("dry_run") is not False:
        errors.append("receipt is not an executed (non-dry-run) panel")
    if panel.get("execution_complete") is not True:
        errors.append("panel execution is not complete")
    terminal_state = panel.get("terminal_state")
    if terminal_state is not None:
        allowed_terminal_states = {
            "completed",
            "completed_with_failures",
            "timed_out",
            "controller_incomplete",
        }
        if terminal_state not in allowed_terminal_states:
            errors.append("panel terminal state is missing, running, or unknown")
        elif terminal_state != "completed":
            errors.append(f"panel terminal state is not successful: {terminal_state}")
        elif panel.get("execution_complete") is not True:
            errors.append("completed terminal state does not match execution_complete")
    if panel.get("runtime_worktree_clean") is False:
        errors.append("panel was executed from a dirty runtime worktree")
    preflight = panel.get("verifier_preflight")
    if preflight is not None and (
        not isinstance(preflight, dict)
        or preflight.get("status") != "passed"
        or preflight.get("safe_path") is not True
        or preflight.get("verifier_sha256") != sha256_file(Path(__file__).resolve())
    ):
        errors.append("verifier preflight is missing, failed or no longer bound to this verifier")

    max_workers = panel.get("max_workers")
    if not isinstance(max_workers, int) or not 1 <= max_workers <= SESSION_WORKER_LIMIT:
        errors.append("panel worker count exceeds or omits the per-session limit")
    if panel.get("session_worker_limit") != SESSION_WORKER_LIMIT:
        errors.append("receipt per-session worker limit does not match the runner")
    if panel.get("global_worker_limit") != GLOBAL_WORKER_LIMIT:
        errors.append("receipt global worker limit does not match the runner")
    if panel.get("global_resource_unit_limit") != GLOBAL_RESOURCE_UNIT_LIMIT:
        errors.append("receipt resource-unit limit does not match the runner")
    if panel.get("max_resource_weight") != MAX_RESOURCE_WEIGHT:
        errors.append("receipt maximum resource weight does not match the runner")
    if panel.get("resource_weight_policy") != RESOURCE_WEIGHT_POLICY:
        errors.append("receipt resource-weight policy does not match the runner")
    if panel.get("global_admission_policy") != GLOBAL_ADMISSION_POLICY:
        errors.append("receipt does not declare the enforced global admission policy")
    if panel.get("child_environment_policy") != PANEL_CHILD_ENVIRONMENT_POLICY:
        errors.append("receipt does not declare the target-profile child environment policy")

    resource_plans = panel.get("resource_plans")
    if not isinstance(resource_plans, dict) or list(resource_plans) != required_profiles:
        errors.append("resource plans are missing or out of admitted order")
        resource_plans = {}
    else:
        planned_units = 0
        for profile, plan in resource_plans.items():
            if not isinstance(plan, dict):
                errors.append(f"{profile}: resource plan is not an object")
                continue
            weight = plan.get("weight")
            if not isinstance(weight, int) or not 1 <= weight <= MAX_RESOURCE_WEIGHT:
                errors.append(f"{profile}: invalid planned resource weight")
                continue
            planned_units += weight
            if plan.get("policy") != RESOURCE_WEIGHT_POLICY:
                errors.append(f"{profile}: resource plan policy mismatch")
            if plan.get("reasoning_effort") not in VALID_REASONING_EFFORTS:
                errors.append(f"{profile}: invalid profile reasoning effort")
            timeout_floor = plan.get("timeout_floor_seconds")
            if timeout_floor is not None and (
                isinstance(timeout_floor, bool)
                or not isinstance(timeout_floor, (int, float))
                or not math.isfinite(float(timeout_floor))
                or float(timeout_floor) <= 0
            ):
                errors.append(f"{profile}: invalid profile timeout floor")
            digest = plan.get("config_sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                errors.append(f"{profile}: invalid config digest in resource plan")
        if panel.get("requested_resource_units") != planned_units:
            errors.append("requested resource-unit total does not match profile plans")

    effective_timeouts = panel.get("effective_timeout_seconds_by_profile")
    if effective_timeouts is not None and (
        not isinstance(effective_timeouts, dict)
        or list(effective_timeouts) != required_profiles
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in effective_timeouts.values()
        )
    ):
        errors.append("effective per-profile timeouts are missing or invalid")
        effective_timeouts = None
    effective_brief_hashes = panel.get("effective_brief_sha256_by_profile")
    if effective_brief_hashes is not None and (
        not isinstance(effective_brief_hashes, dict)
        or list(effective_brief_hashes) != required_profiles
        or any(not isinstance(value, str) or len(value) != 64 for value in effective_brief_hashes.values())
    ):
        errors.append("effective per-profile brief hashes are missing or invalid")
        effective_brief_hashes = None

    output_policy = panel.get("output_policy")
    if not isinstance(output_policy, dict) or output_policy.get("mode") != "redacted_utf8_bounded_v1":
        errors.append("receipt does not declare the redacted bounded output policy")
    elif output_policy.get("max_bytes_per_stream") != MAX_CAPTURE_BYTES:
        errors.append("receipt output limit does not match the runner")

    expected_revision = runtime_revision(args.runtime.expanduser().resolve())
    recorded_revision = panel.get("runtime_revision")
    if expected_revision and recorded_revision == expected_revision:
        pass_check(checks, "runtime_identity", expected_revision)
    else:
        errors.append(
            f"runtime identity mismatch: recorded={recorded_revision!r}, current={expected_revision!r}"
        )

    for result in results:
        if not isinstance(result, dict):
            errors.append("non-object profile result")
            continue
        profile = result.get("profile")
        if args.require_read_only_review or panel.get("read_only_review") is True:
            containment = result.get("containment")
            if not isinstance(containment, dict) or containment.get("policy") != READONLY_REVIEW_POLICY:
                errors.append(f"{profile}: required filesystem containment missing")
            else:
                for key in ("state_home", "scratch"):
                    try:
                        Path(containment[key]).resolve().relative_to(receipt_dir)
                    except (KeyError, TypeError, ValueError):
                        errors.append(f"{profile}: containment {key} escapes run directory")
                if containment.get("network_isolation") != "none" or containment.get("secret_isolation") != "not_provided":
                    errors.append(f"{profile}: containment overstates isolation")
                protected_entries = containment.get("protected_profile_entries")
                run_owned_entries = containment.get("run_owned_profile_entries")
                if (
                    not isinstance(protected_entries, list)
                    or "auth.json" not in protected_entries
                    or "auth.lock" in protected_entries
                    or run_owned_entries != ["auth.lock"]
                ):
                    errors.append(
                        f"{profile}: contained review is missing the run-owned auth lock contract"
                    )
                if result.get("session_archive_outcome") != "archived":
                    errors.append(f"{profile}: contained session was not archived")
        if result.get("outcome") != "returned" or result.get("exit_code") != 0:
            errors.append(f"{profile}: profile did not return successfully")
        resource_weight = result.get("resource_weight")
        plan = resource_plans.get(profile) if isinstance(profile, str) else None
        if not isinstance(resource_weight, int) or not 1 <= resource_weight <= MAX_RESOURCE_WEIGHT:
            errors.append(f"{profile}: invalid or missing resource weight")
        elif not isinstance(plan, dict) or plan.get("weight") != resource_weight:
            errors.append(f"{profile}: result resource weight does not match its plan")
        if result.get("resource_weight_policy") != RESOURCE_WEIGHT_POLICY:
            errors.append(f"{profile}: result resource-weight policy mismatch")
        if result.get("environment_policy") != PANEL_CHILD_ENVIRONMENT_POLICY:
            errors.append(f"{profile}: result child environment policy mismatch")
        if isinstance(plan, dict) and result.get("reasoning_effort") != plan.get("reasoning_effort"):
            errors.append(f"{profile}: result reasoning effort does not match its plan")
        if isinstance(effective_timeouts, dict):
            expected_timeout = effective_timeouts.get(profile)
            if result.get("timeout_seconds") != expected_timeout:
                errors.append(f"{profile}: result timeout does not match its effective timeout")
        admission_slots = result.get("admission_slots")
        if (
            not isinstance(admission_slots, list)
            or len(admission_slots) != resource_weight
            or len(set(admission_slots)) != len(admission_slots)
            or any(
                not isinstance(slot, int) or not 0 <= slot < GLOBAL_RESOURCE_UNIT_LIMIT
                for slot in admission_slots
            )
        ):
            errors.append(f"{profile}: invalid or missing weighted admission slots")
        basis = result.get("resource_weight_basis")
        if not isinstance(basis, dict) or not isinstance(plan, dict) or basis.get("config_sha256") != plan.get("config_sha256"):
            errors.append(f"{profile}: resource-weight basis does not match its plan")
        admission_wait = result.get("admission_wait_seconds")
        if not isinstance(admission_wait, (int, float)) or admission_wait < 0:
            errors.append(f"{profile}: invalid or missing global admission wait")
        if panel.get("input_manifest_policy") == "first_pass_no_peer_edges_v1":
            input_rel = result.get("input_manifest_path")
            if not isinstance(input_rel, str):
                errors.append(f"{profile}: missing input manifest")
            else:
                input_path = (receipt_dir / input_rel).resolve()
                try:
                    input_path.relative_to(receipt_dir)
                    input_bytes = input_path.read_bytes()
                    input_document = json.loads(input_bytes.decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(f"{profile}: invalid input manifest: {exc}")
                else:
                    if len(input_bytes) != result.get("input_manifest_bytes"):
                        errors.append(f"{profile}: input manifest byte count changed")
                    if hashlib.sha256(input_bytes).hexdigest() != result.get("input_manifest_sha256"):
                        errors.append(f"{profile}: input manifest digest changed")
                    if input_document.get("schema") != "AresProfileInputManifestV1":
                        errors.append(f"{profile}: input manifest schema mismatch")
                    if input_document.get("profile") != profile or input_document.get("session_id") != result.get("session_id"):
                        errors.append(f"{profile}: input manifest identity mismatch")
                    if input_document.get("workspace") != panel.get("workspace"):
                        errors.append(f"{profile}: input manifest workspace mismatch")
                    if input_document.get("environment_policy") != PANEL_CHILD_ENVIRONMENT_POLICY:
                        errors.append(f"{profile}: input manifest child environment policy mismatch")
                    if input_document.get("containment", {"policy": "none"}) != result.get("containment", {"policy": "none"}):
                        errors.append(f"{profile}: input manifest containment mismatch")
                    expected_effective_brief_hash = (
                        effective_brief_hashes.get(profile)
                        if isinstance(effective_brief_hashes, dict)
                        else panel.get("effective_brief_sha256")
                    )
                    if (
                        input_document.get("brief_sha256") != panel.get("brief_sha256")
                        or input_document.get("effective_brief_sha256") != expected_effective_brief_hash
                    ):
                        errors.append(f"{profile}: input manifest brief binding mismatch")
                    if isinstance(effective_timeouts, dict) and input_document.get("timeout_seconds") != effective_timeouts.get(profile):
                        errors.append(f"{profile}: input manifest timeout does not match its effective timeout")
                    if isinstance(plan, dict) and input_document.get("reasoning_effort") != plan.get("reasoning_effort"):
                        errors.append(f"{profile}: input manifest reasoning effort does not match its plan")
                    if input_document.get("edge_policy") != "controller_to_first_pass_profile_only_v1" or input_document.get("review_barrier") != "panel_complete" or input_document.get("peer_outputs_admissible") is not False:
                        errors.append(f"{profile}: input edge policy mismatch")
                    required_exclusions = {
                        "peer_conclusion",
                        "peer_report",
                        "peer_stdout",
                        "peer_stderr",
                        "peer_transcript",
                    }
                    exclusions = input_document.get("excluded_input_kinds")
                    if not isinstance(exclusions, list) or set(exclusions) != required_exclusions:
                        errors.append(f"{profile}: peer-output exclusions are incomplete")
                    input_kinds = {
                        item.get("kind")
                        for item in input_document.get("input_objects", [])
                        if isinstance(item, dict)
                    }
                    if input_kinds != {"controller_brief", "assigned_workspace"}:
                        errors.append(f"{profile}: undeclared first-pass input edge")
        stdout_rel = result.get("stdout_path")
        stderr_rel = result.get("stderr_path")
        if not isinstance(stdout_rel, str) or not isinstance(stderr_rel, str):
            errors.append(f"{profile}: missing artifact paths")
            continue
        for label, relative, digest_key, bytes_key in (
            ("stdout", stdout_rel, "stdout_sha256", "stdout_bytes"),
            ("stderr", stderr_rel, "stderr_sha256", "stderr_bytes"),
        ):
            path = (receipt_dir / relative).resolve()
            try:
                path.relative_to(receipt_dir)
                actual_size = path.stat().st_size
                actual_digest = sha256_file(path)
            except (OSError, ValueError) as exc:
                errors.append(f"{profile}: invalid {label} artifact: {exc}")
                continue
            if actual_size != result.get(bytes_key):
                errors.append(f"{profile}: {label} byte count changed")
            if actual_digest != result.get(digest_key):
                errors.append(f"{profile}: {label} digest changed")
            if label == "stdout":
                block = reported_operational_block(path.read_bytes(), b"")
                if block is not None:
                    errors.append(f"{profile}: operationally blocked report: {block}")
        if result.get("stdout_truncated") or result.get("stderr_truncated"):
            errors.append(f"{profile}: truncated report or diagnostics")

    if not errors:
        pass_check(checks, "profile_artifacts", f"{len(required_profiles)} selected profile reports and diagnostics verified")
    else:
        for error in errors:
            fail(checks, "receipt_integrity", error)

    verification = {
        "schema": "AresProfilePanelVerificationV2",
        "verified_at": utc_now(),
        "receipt": str(panel_path),
        "runtime": str(args.runtime.expanduser().resolve()),
        "runtime_revision": expected_revision,
        "checks": checks,
        "controller_verified": not errors,
        "execution_artifacts_verified": not errors,
        "semantic_review_complete": False,
        "semantic_review_required": True,
        "evidence_state": "execution_artifacts_verified" if not errors else "blocked_unknown",
        "remaining_delta": [
            "Controller must review every selected domain report for evidence, dissent, and relevance.",
            "Desktop recovery remains a separate acceptance gate.",
        ],
    }
    output_path = receipt_dir / "verification.json"
    attempts = receipt_dir / "verifications"
    attempts.mkdir(exist_ok=True)
    if output_path.exists():
        previous = output_path.read_bytes()
        previous_path = attempts / ("prior-" + hashlib.sha256(previous).hexdigest() + ".json")
        if not previous_path.exists():
            with previous_path.open("xb") as handle:
                handle.write(previous)
    attempt = attempts / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex + ".json")
    verification["attempt_path"] = str(attempt)
    with attempt.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(verification, indent=2) + "\n")
    atomic_write_manifest(output_path, verification)
    print(json.dumps(verification, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
