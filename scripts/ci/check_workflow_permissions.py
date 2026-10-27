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

import re
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

PR_EVENTS = {
    "pull_request", "pull_request_target", "pull_request_review",
    "pull_request_review_comment", "workflow_call",
}
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
            # Fetching external code — any git verb that pulls a remote tree,
            # with flags/config that may precede the verb:
            #   git clone/fetch/pull, git -C <dir> fetch, git -c k=v clone,
            #   gh pr checkout, gh repo clone.
            if re.search(r"\bgit\b[\w\-.=/\"'${}~ ]*?\s(?:clone|fetch|pull)\b", run_text):
                return True
            if "gh pr checkout" in run_text or "gh repo clone" in run_text:
                return True
            # Remote execution:
            #   <fetcher> piped into any interpreter — bare or path-qualified
            #   (sh/bash/zsh/dash/ksh, /bin/sh), with flags (-E, -l, -) or env/sudo
            #   wrappers, plus script interpreters (python*/node/ruby/perl/deno/bun);
            #   process substitution (bash <(curl), source <(curl));
            #   download-then-run (…&& sh x.sh, …; sh x.sh, including across lines);
            #   and command substitution via combined flags (bash -lc "$(curl …)").
            _interp = (
                r"(?:[\"']?[\w./-]+/)?(?:ba|z|da|k)?sh\b"
                r"|(?:[\"']?[\w./-]+/)?python[\d.]*\b|(?:[\"']?[\w./-]+/)?(?:node|ruby|perl|php|lua|deno|bun)\b"
            )
            # Post-pipe token wrapper: sudo (with any flag/arg sequence incl. --),
            # env, nice, busybox, xargs — before the interpreter.
            _wrapper = (
                r"(?:sudo\s+(?:-\S+(?:\s+\S+)*\s+)*|--\s+)*"
                r"(?:env\s+|nice\s+|busybox\s+|xargs\s+)*"
            )
            # Post-pipe detection: once a fetcher pipes anywhere, an
            # interpreter appearing ANYWHERE downstream counts — wrappers with
            # flags (env -i sh, xargs -I{} sh -c), filters (base64 -d | sh),
            # path-qualified sudo (/usr/bin/sudo bash), and quoting ('bash')
            # all covered. Command substitution into any interpreter
            # (python3 -c "$(curl …)") is the same class as bash -c.
            interp_any = (
                r"(?:[\w./-]+/)?(?:ba|z|da|k)?sh\b"
                r"|(?:[\w./-]+/)?python[\d.]*\b|(?:[\w./-]+/)?(?:node|ruby|perl|php|lua|deno|bun)\b"
            )
            for fetcher in (r"\b(?:curl|wget)\b", r"\bgh\s+api\b"):
                if re.search(rf"{fetcher}[^|\n]*\|", run_text):
                    post = run_text.split("|", 1)[1]
                    if re.search(rf"(?:^|[\s;&|(])['\"]?{interp_any}", post, re.S):
                        return True
                    if re.search(rf"(?:-\w+\s+)*-c\s*['\"]?\$\(?\s*(?:curl|wget)\b", post):
                        return True
                    if re.search(r"\b(?:source|\.)\s", post):
                        return True
            if re.search(r"\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*<\s*[(<]\s*\(?\s*(?:curl|wget)\b", run_text):
                return True
            if re.search(r"\b(?:source|\.)\s*<(?:\(|<)\s*(?:curl|wget)\b", run_text):
                return True
            # -\w*c\b also matches combined flag clusters whose last letter is
            # c (bash -lc "$(curl …)").
            if re.search(r"\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\b\s*['\"]?\$\(\s*(?:curl|wget)\b", run_text):
                return True
            # download-then-run: the fetcher appears anywhere in the command and
            # the interpreter follows a separator (&&, ;) or a bare newline —
            # greedy + re.S so multi-line run blocks are covered. Executing the
            # downloaded file by path (chmod +x && ./s.sh) is the same class.
            if re.search(r"\b(?:curl|wget)\b.*&&\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", run_text, re.S):
                return True
            if re.search(r"\b(?:curl|wget)\b.*;\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", run_text, re.S):
                return True
            if re.search(r"\b(?:curl|wget)\b.*\n\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", run_text, re.S):
                return True
            if re.search(r"\b(?:curl|wget)\b.*(&&|;|\n).*(?:\./[\w.-]+|chmod\s+\+x)", run_text, re.S):
                return True
            # Arbitrary package execution: npx -y / pnpm dlx / yarn dlx / bunx /
            # uvx / pipx run fetch and execute a package; pip/uv-pip installs
            # from a remote VCS (any scheme) or URL execute setup code (URL may
            # be quoted, as in pip install --editable 'git+https://…'); npm i
            # github:owner/repo is the same class.
            if re.search(r"\bnpx\b(?:\s+-\w+)*\s+(?:-y\b|--yes\b)", run_text):
                return True
            if re.search(r"\b(?:pnpm|yarn)\s+dlx\b|\bbunx\b|\buvx\b(?:\s+--from\b)?|\bpipx\s+run\b", run_text):
                return True
            if re.search(r"\b(?:pip[0-9.]*|uv\s+pip)\s+install\b[^&|;]*['\"]?\s*(?:git\+(?:https?|ssh|git)://|https?://)", run_text):
                return True
            if re.search(r"\bnpm\s+(?:i|install)\b[^&|;\n]*(?:github:|https?://|git\+|[\w.-]+/[\w.-]+)", run_text):
                return True
            # eval "$(curl …)" / gh api piped to a shell
            if re.search(r"\beval\s+['\"]?\$\(\s*(?:curl|wget)\b", run_text):
                return True
            if re.search(r"\bgh\s+api\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", run_text):
                return True
            # Legacy literal shapes (kept for robustness)
            if "curl" in run_text and ("| sh" in run_text or "| bash" in run_text):
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
            # Activity-only automation (e.g. label reruns) never executes PR
            # code, but it still holds a trusted token: an explicit minimal
            # map and the write-scope refusal apply regardless. Code-exec
            # gates only the FOREIGN-code rules (remote fetch, SARIF caller
            # caps, package executors).
            perms0 = doc.get("permissions")
            if perms0 is None:
                findings.append(
                    f"{path.name}: PR-triggered workflow declares no workflow-level "
                    f"permissions (inherits repo defaults; declare an explicit minimal map)"
                )
            elif isinstance(perms0, dict):
                for scope in sorted(perms0):
                    if scope not in (WORKFLOW_OK | JOB_WRITE_ALLOWED):
                        findings.append(
                            f"{path.name}: workflow-level scope '{scope}' is outside the minimal map "
                            f"{sorted(WORKFLOW_OK | JOB_WRITE_ALLOWED)}"
                        )
                # Activity-only flows may keep operational write scopes
                # (actions: write for run reruns, checks: write for statuses) —
                # they never touch PR code. contents: write is refused on EVERY
                # PR-family workflow: it can push to the PR branch.
                for scope, level in sorted(perms0.items()):
                    if level == "write" and scope in ("contents", "security-events"):
                        findings.append(
                            f"{path.name}: workflow-level '{scope}: write' refused on a PR-triggered "
                            f"workflow (PR flows must never hold {scope} writes; "
                            f"writes belong in workflow_run flows)"
                        )
            else:
                findings.append(f"{path.name}: workflow-level permissions '{perms0}' is not an explicit minimal map")
            continue

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
        # Scope-key allowlist: the body's contract is an explicit minimal map
        # within {contents, actions, pull-requests} (+ security-events under
        # its own exception). Unknown read-only scope keys are refused too.
        for scope in sorted(perms):
            if scope not in (WORKFLOW_OK | JOB_WRITE_ALLOWED):
                findings.append(
                    f"{path.name}: workflow-level scope '{scope}' is outside the minimal map "
                    f"{sorted(WORKFLOW_OK | JOB_WRITE_ALLOWED)}"
                )
        # The SARIF caller-cap exception must reference the LOCAL reusable
        # flow exactly (./.github/workflows/osv-scanner.yml) — a substring
        # match would let a foreign fork's osv-scanner.yml exempt writes.
        _local_osv = re.compile(r"\./\.github/workflows/osv-scanner\.yml(?:@.*)?$")
        sarif_caller = any(
            isinstance(j, dict) and _local_osv.fullmatch(str(j.get("uses", "")).strip())
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
            if isinstance(j, dict) and re.fullmatch(r"\./\.github/workflows/osv-scanner\.yml(?:@.*)?", str(j.get("uses") or "").strip())
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
