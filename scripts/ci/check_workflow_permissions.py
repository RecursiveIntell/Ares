#!/usr/bin/env python3.14
"""Least-privilege token checker (plan Task 4.2, D-11/E-13).

Rule (plan RED text): workflows triggered by pull_request /
pull_request_target must declare workflow-level permissions within
{contents: read, actions: read, pull-requests: read} plus, only in the SARIF
upload producer (osv-scanner.yml), security-events: write; and no JOB in a
PR-triggered workflow may hold a write scope other than the SARIF exception.
Workflow-level 'pull-requests: write' is refused outright on PR-triggered
flows: PR comments are posted by ci-review-comment.yml (workflow_run,
top-level, same-repo guard) — the sanctioned write context (E-64).

Non-PR-triggered workflows (workflow_run / schedule / dispatch / push) are
the allowed write surfaces and are out of this checker's scope.

Exit 1 with a findings list on violation; exit 0 when clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml required", file=sys.stderr)
    raise

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/ci/ -> repo root
WF_DIR = REPO / ".github" / "workflows"

# SARIF-upload producers allowed workflow-level security-events: write.
SECURITY_EVENTS_ALLOWED = {"osv-scanner.yml"}

PR_EVENTS = {"pull_request", "pull_request_target", "workflow_call"}
WORKFLOW_OK = {"contents", "actions", "pull-requests"}  # read levels only
JOB_WRITE_ALLOWED = {"security-events"}  # only in SECURITY_EVENTS_ALLOWED files

# Workflows that never check out code do not execute PR code (activity-only
# automation such as label-rerun.yml — an API-only rerun actor). The plan's
# rule governs jobs that check out or execute PR code, so those files are
# exempt from the write-scope refusal.
NO_CHECKOUT_EXEMPT = True

# yaml parses a bare `on:` key as boolean True; both spellings must be tried.
_ON_KEYS = ("on", True)


def _triggers(doc: dict) -> set[str]:
    for key in _ON_KEYS:
        triggers = doc.get(key)
        if isinstance(triggers, str):
            return {triggers}  # scalar `on: pull_request`
        if isinstance(triggers, list):
            return {str(t) for t in triggers}
        if isinstance(triggers, dict):
            return {str(t) for t in triggers.keys()}
        if triggers is True:
            return {"push"}  # bare `on: true` is push-equivalent; not PR
    return set()


def _has_real_code_execution(doc: dict) -> bool:
    """True when any job runs foreign code — run-steps with a `uses:` action,
    reusable-workflow jobs, or run-steps that clone/curl-pipe external code.
    Comment-only mentions don't count."""
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str) and job["uses"].strip():
            return True  # reusable workflow job
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("uses"), str) and step["uses"].strip():
                return True
            run_text = str(step.get("run") or "")
            if "git clone" in run_text or "curl" in run_text and ("| sh" in run_text or "| bash" in run_text):
                return True
    return False


def _effective_job_write_scopes(doc: dict) -> dict[str, list[str]]:
    """Job name -> write scopes in effect. Job-level permissions override the
    workflow level entirely (GitHub semantics); jobs without an explicit map
    inherit the workflow-level scopes, which must therefore be counted."""
    wf = doc.get("permissions")
    wf_write = sorted(s for s, l in (wf or {}).items() if l == "write") if isinstance(wf, dict) else []
    result: dict[str, list[str]] = {}
    for name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        jperms = job.get("permissions")
        if jperms is None:
            result[name] = list(wf_write)  # inherited
        elif isinstance(jperms, dict):
            result[name] = sorted(s for s, l in jperms.items() if l == "write")
        else:
            result[name] = [str(jperms)]  # 'write-all' / 'read-all' string
    return result


def check(workflow_dir: Path = WF_DIR) -> list[str]:
    findings: list[str] = []
    for path in sorted(list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))):
        try:
            raw = path.read_text(encoding="utf-8")
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            findings.append(f"{path.name}: unparseable YAML: {exc}")
            continue
        if not isinstance(doc, dict):
            continue
        events = _triggers(doc)
        if not (PR_EVENTS & events):
            continue  # non-PR flows are sanctioned write contexts (out of scope)
        if not _has_real_code_execution(doc):
            continue  # activity-only automation; never executes PR code

        perms = doc.get("permissions")
        if perms is None:
            # Explicit-minimal contract: a PR-triggered workflow that runs
            # code must declare its permissions, not inherit repo defaults.
            findings.append(
                f"{path.name}: PR-triggered workflow that runs code declares no "
                f"workflow-level permissions (inherits repo defaults; declare an explicit minimal map)"
            )
            continue
        if not isinstance(perms, dict):
            findings.append(f"{path.name}: workflow-level permissions '{perms}' is not an explicit minimal map")
            continue
        # The SARIF caller-cap exception must reference the reusable flow
        # structurally (a job `uses:` the osv-scanner workflow), not merely
        # mention the name in a comment.
        sarif_caller = any(
            isinstance(j, dict) and "workflows/osv-scanner.yml" in str(j.get("uses", ""))
            for j in (doc.get("jobs") or {}).values()
        )
        for scope, level in sorted(perms.items()):
            if level != "write":
                continue  # read scopes are fine (incl. pull-requests: read)
            if scope == "security-events":
                # Allowed in the SARIF producer, or in a caller that invokes
                # the reusable osv-scanner flow (GitHub requires callers to
                # hold the scopes the callee uses; the write executes in the
                # callee's job, not on PR code).
                if path.name in SECURITY_EVENTS_ALLOWED or sarif_caller:
                    continue
            findings.append(
                f"{path.name}: workflow-level '{scope}: write' refused on a PR-triggered "
                f"workflow (PR code must run read-only; writes belong in workflow_run flows)"
            )

        # Job-level: effective write scopes (job map overrides workflow level;
        # no map = inherited workflow scopes). PR-code jobs may hold only the
        # SARIF write — in the producer file's scan job, or in a job that
        # invokes the reusable osv-scanner flow (the caller cap GitHub
        # requires; the write executes in the callee's job).
        sarif_caller_jobs = {
            n for n, j in (doc.get("jobs") or {}).items()
            if isinstance(j, dict) and "workflows/osv-scanner.yml" in str(j.get("uses") or "")
        }
        for job_name, writes in _effective_job_write_scopes(doc).items():
            for scope in writes:
                if scope == "security-events" and (
                    job_name in sarif_caller_jobs
                    or (path.name in SECURITY_EVENTS_ALLOWED and job_name == "scan")
                ):
                    continue
                if scope in ("write-all", "read-all") or scope.startswith("'"):
                    findings.append(
                        f"{path.name}: job '{job_name}' uses '{scope}' — explicit minimal maps only"
                    )
                    continue
                findings.append(
                    f"{path.name}: job '{job_name}' holds '{scope}: write' (effective) on a "
                    f"PR-triggered workflow (only security-events: write on the SARIF job is allowed)"
                )
    return findings


def main() -> int:
    findings = check()
    for f in findings:
        print(f"ERROR: {f}", file=sys.stderr)
    if findings:
        print(f"workflow-permissions check: {len(findings)} violation(s)", file=sys.stderr)
        return 1
    print("workflow-permissions check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
