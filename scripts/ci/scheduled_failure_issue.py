#!/usr/bin/env python3.14
"""Scheduled-failure issue sweeper (plan Task 1.5, restructured).

GitHub statically caps nested reusable-workflow permissions: a job called
from ``ci.yaml`` (which runs PR code) can never hold ``issues: write`` even
if the root workflow granted it. So the alerting layer must be a
standalone top-level workflow that runs OUTSIDE the PR gate and inspects
the scheduled runs of watched workflows after the fact.

For each watched workflow (by file name):
- read the most recent run with ``event=schedule``;
- completed + no failed/cancelled/timed-out job  -> green: close that
  workflow's open ``ci-scheduled-failure`` issue (if any);
- completed + any failure                        -> red: open or update ONE
  tracked issue with the failing-jobs table and run link;
- a run that failed with zero job records (startup-level failure) renders
  an explicit row instead of an empty table;
- in-progress runs are skipped (no decision);
- if the issue API path itself fails, the script exits non-zero so the
  sweeper run shows red (no silent gap, X-11).

Read-only except for issue create/update/close.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

LABEL = "ci-scheduled-failure"
# Run-level conclusions treated as red. startup_failure is GitHub's
# conclusion when the workflow graph itself fails to build (verified live in
# this repo's run data); action_required flags runs blocked on approval —
# both must open/keep the tracked issue, never close it.
BAD_CONCLUSIONS = ("failure", "timed_out", "cancelled", "startup_failure", "action_required")


def gh_json(args: list[str], attempts: int = 2) -> dict | list:
    """Run `gh` and parse stdout as JSON; retry transient 5xx."""
    for i in range(attempts):
        out = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if out.returncode == 0:
            return json.loads(out.stdout)
        transient = "HTTP 5" in out.stderr or "Server Error" in out.stderr
        if i + 1 < attempts and transient:
            continue
        raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {out.stderr.strip()[:200]}")
    raise RuntimeError(f"gh {' '.join(args[:3])}… failed after {attempts} attempts")


def gh_raw(args: list[str]) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {out.stderr.strip()[:200]}")
    return out.stdout


def decide(run: dict | None, job_conclusions: list[str] | None) -> tuple[str, list[str] | None]:
    """Decision core. run: last scheduled run (None = never scheduled);
    job_conclusions: list of job conclusions, or None when unknown.

    Returns (action, failing_jobs): action in {"pending", "close", "report",
    "skip"}; failing_jobs is the list of bad conclusions (None when not
    applicable). Pure and fixture-testable.
    """
    if run is None:
        return "skip", None
    if (run.get("status") or "").lower() != "completed":
        return "pending", None
    run_bad = (run.get("conclusion") or "").lower() in BAD_CONCLUSIONS
    if run_bad:
        # RED outranks job detail: a red run must never close its issue even
        # when the fetched job page looks green (startup failures, rollup
        # artifacts, >page-size truncation can all hide failures).
        bad = [c for c in (job_conclusions or []) if c in BAD_CONCLUSIONS]
        return "report", bad
    if job_conclusions:  # run not bad, usable job detail
        bad = [c for c in job_conclusions if c in BAD_CONCLUSIONS]
        if not bad:
            return "close", []
        return "report", bad
    # No usable job detail and the run itself is not bad: close only on
    # explicit success; anything else makes no decision.
    if (run.get("conclusion") or "").lower() == "success":
        return "close", []
    return "pending", None


def issue_body(workflow_name: str, run: dict, job_rows: list[tuple[str, str]], zero_jobs: bool) -> str:
    """Issue body. job_rows: (name, conclusion) for EVERY fetched job; the
    table lists the failing rows (or the startup-level placeholder)."""
    run_url = run.get("url") or f"https://github.com/THIS/actions/runs/{run.get('databaseId')}"
    failing = [(n, c) for (n, c) in job_rows if c in BAD_CONCLUSIONS]
    if zero_jobs:
        summary = "Startup-level failure (no job records available for this run)."
    else:
        summary = f"Failed: {len(failing)} of {len(job_rows)} job(s)."
    lines = [
        f"Scheduled run of **{workflow_name}** failed.",
        "",
        f"- Run: {run_url}",
        f"- {summary}",
        "",
        "| job | conclusion |",
        "|---|---|",
    ]
    if zero_jobs:
        lines.append("| (no job records available for this run — startup-level failure) | - |")
    else:
        for name, conclusion in failing:
            lines.append(f"| {name} | {conclusion} |")
        if not failing:
            # Run-level red with no identifiable failing job on the fetched
            # pages: say so instead of leaving an empty table.
            lines.append("| (run-level failure; no failing job on fetched job pages) | - |")
    lines.append("")
    lines.append("<!-- ares-scheduled-failure-watch v1; auto-managed: closed on next green scheduled run -->")
    return "\n".join(lines) + "\n"


def ensure_label(repo: str) -> None:
    """Create the tracking label if missing (gh issue create --label fails
    otherwise, and the first real failure would then go unreported)."""
    names: set[str] = set()
    page = 1
    while True:
        out = subprocess.run(
            ["gh", "label", "list", "--repo", repo, "--json", "name",
             "--limit", "100", "--page", str(page)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if out.returncode != 0:
            break
        try:
            batch = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            break
        names.update(n.get("name") for n in batch)
        if len(batch) < 100:
            break
        page += 1
    if LABEL in names:
        return
    proc = subprocess.run(
        ["gh", "label", "create", LABEL, "--force", "--repo", repo,
         "--description", "A scheduled CI run failed (auto-managed by ares-scheduled-failure-watch)",
         "--color", "D73A4A"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0 and "already exists" not in (proc.stderr or "") and "already_exists" not in (proc.stderr or ""):
        raise RuntimeError(f"label create failed: {(proc.stderr or '')[:200]}")


def find_open_issue(repo: str, workflow_name: str) -> str:
    """Deterministic open-issue lookup: REST list by label (NOT the
    eventually-consistent search index, which can miss a just-created issue
    and cause duplicates or missed closes), exact-title match client-side.
    Paginated past the first 100 open labelled issues. NOTE: list-then-create
    is still not atomic across concurrent actors; the watch workflow's own
    concurrency group serializes its runs, which is the practical guard."""
    title = f"[{LABEL}] {workflow_name}"
    page = 1
    while True:
        out = gh_raw(["api", f"/repos/{repo}/issues?labels={LABEL}&state=open&per_page=100&page={page}"])
        try:
            issues = json.loads(out) if out.strip() else []
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"issue list parse failed: {exc}") from exc
        for issue in issues:
            if issue.get("title") == title and "pull_request" not in issue:
                return str(issue.get("number"))
        if len(issues) < 100:
            return ""
        page += 1


def sweep(repo: str, watched: list[str], dry_run: bool = False) -> int:
    failures = 0
    for wf in watched:
        try:
            runs = gh_json(["run", "list", "--repo", repo, f"--workflow={wf}",
                            "--event=schedule", "--limit", "1",
                            "--json", "databaseId,conclusion,status,url,displayTitle"])
            run = runs[0] if runs else None
            # Stable issue-title key: the workflow file name, never the run's
            # displayTitle (a commit subject that changes per run and would
            # fragment the tracked issue across runs).
            wf_name = wf
            job_rows: list[tuple[str, str]] | None = None  # (name, conclusion)
            if run is not None:
                # Paginate: a matrix run can exceed one page; a truncated page
                # could hide a failing job.
                page = 1
                job_list: list[dict] = []
                while True:
                    data = gh_json(["api",
                                    f"/repos/{repo}/actions/runs/{run['databaseId']}/jobs?per_page=100&page={page}"])
                    batch = data.get("jobs", []) if isinstance(data, dict) else data
                    job_list.extend(batch)
                    if len(batch) < 100:
                        break
                    page += 1
                job_rows = [(j.get("name") or "?", j.get("conclusion") or "unknown") for j in job_list]

            action, failing = decide(run, [c for (_, c) in job_rows] if job_rows is not None else None)
            existing = find_open_issue(repo, wf_name) if action in ("report", "close") else ""

            if action == "skip":
                print(f"{wf}: never scheduled; nothing to do")
            elif action == "pending":
                print(f"{wf}: latest scheduled run in progress; no decision")
            elif action == "close":
                if existing:
                    print(f"{wf}: green; closing issue #{existing}")
                    if not dry_run:
                        gh_raw(["issue", "close", existing, "--repo", repo,
                                "--comment", f"Recovered: latest scheduled run {run['databaseId']} is green; closing the tracked failure issue."])
                else:
                    print(f"{wf}: green; no open failure issue")
            elif action == "report":
                zero_jobs = run is not None and job_rows == []
                body = issue_body(wf_name, run or {}, job_rows or [], zero_jobs)
                print(f"{wf}: RED (zero_jobs={zero_jobs})")
                if dry_run:
                    print(body)
                    continue
                ensure_label(repo)
                if existing:
                    proc = subprocess.run(["gh", "issue", "edit", existing, "--repo", repo,
                                           "--body-file", "-"], input=body, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
                    if proc.returncode != 0:
                        raise RuntimeError(f"issue edit failed: {proc.stderr[:200]}")
                    print(f"{wf}: updated issue #{existing}")
                else:
                    proc = subprocess.run(
                        ["gh", "issue", "create", "--repo", repo,
                         "--title", f"[{LABEL}] {wf_name}",
                         "--body-file", "-", "--label", LABEL],
                        input=body, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
                    if proc.returncode != 0:
                        raise RuntimeError(f"issue create failed: {proc.stderr[:200]}")
                    print(f"{wf}: opened new failure issue")
        except Exception as exc:  # noqa: BLE001 — one workflow must not hide the others
            failures += 1
            print(f"{wf}: ERROR {exc}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="RecursiveIntell/Ares")
    ap.add_argument("--watch", required=True,
                    help="comma-separated workflow FILE names to watch (e.g. install-e2e.yml,osv-scanner.yml)")
    ap.add_argument("--dry-run", action="store_true", help="print decisions and bodies; no writes")
    args = ap.parse_args(argv)
    watched = [w.strip() for w in args.watch.split(",") if w.strip()]
    return sweep(args.repo, watched, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
