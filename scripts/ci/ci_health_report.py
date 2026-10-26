#!/usr/bin/env python3.14
"""CI health report for RecursiveIntell/Ares (plan Task 0.2, PLAN-FINAL.md).

Reads the last N runs per workflow (via `gh` or a captured JSON fixture) and
emits JSON + Markdown summaries: pass rate, rerun rate, p50/p90 duration,
per-lane green streaks, failure classes and a failure signature table, plus
open Dependabot alert counts as the alert-delta baseline.

Pure core (parse/classify/policy) is unit-testable without network access:
feed `runs` payloads shaped like the `--input` fixture to `build_report()`.

Lane policy implemented here (docs/ci/lane-policy.md source of truth):
- advisory -> required candidate after 10 consecutive green runs AND p90
  duration under the lane budget;
- required lane with >= 3 infra/network|unknown failures inside 14 days ->
  quarantine proposal (owner + 14-day expiry + explicit evaluator exception
  must be added by a human; this tool only reports).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

REPO = "RecursiveIntell/Ares"
REPORT_SCHEMA = "AresCiHealthReportV1"
GRADUATION_STREAK = 10
QUARANTINE_WINDOW_DAYS = 14
QUARANTINE_THRESHOLD = 3
DEFAULT_BUDGET_MIN = 20.0

FAILURE_CLASSES = ("test", "infra/network", "admission", "cancelled", "unknown")


def classify_failure(job: dict) -> str:
    """Classify a failed job from its conclusion and failed step names."""
    if (job.get("conclusion") or "").lower() == "cancelled":
        return "cancelled"
    names = " || ".join(
        str(s.get("name", "")) for s in job.get("steps", []) if s.get("conclusion") == "failure"
    ).lower()
    blob = f"{str(job.get('name', '')).lower()} || {names}"
    if any(
        k in blob
        for k in ("infra", "network", "rate limit", "timed out", "timeout", "runner", "apt-get", "pip download")
    ):
        return "infra/network"
    if any(k in blob for k in ("admission", "evaluator", "classify_changes", "required check")):
        return "admission"
    if any(k in blob for k in ("pytest", "cargo test", "ruff", "lint", "typecheck", "tsc", "test:", "check:", " tests", "e2e")):
        return "test"
    return "unknown"


def failure_signature(job: dict) -> str:
    """First failed step name (or job name) as the grouping signature."""
    for s in job.get("steps", []):
        if s.get("conclusion") == "failure":
            return f"{job.get('name', '?')}:{s.get('name', '?')}"
    return f"{job.get('name', '?')}:(no failed step recorded)"


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_duration_minutes(run: dict) -> float | None:
    started = _parse_ts(run.get("run_started_at") or run.get("startedAt") or run.get("createdAt") or "")
    updated = _parse_ts(run.get("updatedAt") or "")
    if not started or not updated or updated < started:
        return None
    return (updated - started).total_seconds() / 60.0


def percentile(values: list[float], p: float) -> float | None:
    """Interpolated percentile; p in [0, 100]."""
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    k = (len(vs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    frac = k - lo
    return vs[lo] + (vs[hi] - vs[lo]) * frac


def green_streak(runs: list[dict]) -> int:
    """Consecutive successful runs counted from the most recent (list[0])."""
    streak = 0
    for r in runs:
        c = (r.get("conclusion") or "").lower()
        if r.get("status", "completed").lower() != "completed":
            break
        if c == "success":
            streak += 1
        elif c == "skipped":
            continue
        else:
            break
    return streak


def rerun_rate(runs: list[dict]) -> float | None:
    attempts = [r.get("attempt") or r.get("run_attempt") or 1 for r in runs]
    if not attempts:
        return None
    reruns = sum(1 for a in attempts if int(a) > 1)
    return reruns * 100.0 / len(attempts)


def should_graduate(streak: int, p90_min: float | None, budget_min: float) -> bool:
    """Advisory -> required-candidate rule. p90 None (no data) never graduates."""
    return streak >= GRADUATION_STREAK and p90_min is not None and p90_min < budget_min


def should_quarantine(recent_failures: list[tuple[datetime | None, str]], now: datetime | None = None) -> bool:
    """Required lane: >= QUARANTINE_THRESHOLD infra/unknown failures within the window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=QUARANTINE_WINDOW_DAYS)
    hits = [cls for (ts, cls) in recent_failures if cls in ("infra/network", "unknown") and (ts is None or ts >= cutoff)]
    return len(hits) >= QUARANTINE_THRESHOLD


def build_report(workflow: str, runs: list[dict], failed_jobs: dict[str, list[dict]], budget_min: float) -> dict:
    """Pure report core. runs: most-recent-first, as `gh run list --json` yields."""
    completed = [r for r in runs if (r.get("status") or "completed").lower() == "completed"]
    durations = [d for r in completed if (d := run_duration_minutes(r)) is not None]
    passed = sum(1 for r in completed if (r.get("conclusion") or "").lower() == "success")
    failed = [r for r in completed if (r.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled")]

    classes: dict[str, int] = {c: 0 for c in FAILURE_CLASSES}
    signatures: dict[str, int] = {}
    per_run_classes: dict[str, list[tuple[datetime | None, str]]] = {}
    for r in failed:
        rid = str(r.get("databaseId"))
        jobs = failed_jobs.get(rid)
        if jobs is None:
            per_run_classes[rid] = []  # job detail not sampled for this run
            continue
        run_cls = "unknown"
        failed_only = [j for j in jobs if (j.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled")]
        for j in failed_only:
            if (j.get("name") or "").strip().lower() == "all required checks pass":
                continue  # aggregate rollup: consequence of lane failures, not a lane
            cls = classify_failure(j)
            classes[cls] += 1
            sig = failure_signature(j)
            signatures[sig] = signatures.get(sig, 0) + 1
            if run_cls == "unknown" and cls != "unknown":
                run_cls = cls
        per_run_classes[rid] = [
            (_parse_ts(r.get("startedAt") or r.get("run_started_at") or ""), c)
            for c in {classify_failure(j) for j in jobs} or {run_cls}
        ]

    streak = green_streak(runs)
    p50 = percentile(durations, 50)
    p90 = percentile(durations, 90)
    quarantine = should_quarantine(
        [item for items in per_run_classes.values() for item in items]
    ) if failed and streak == 0 else False
    return {
        "schema": REPORT_SCHEMA,
        "workflow": workflow,
        "runs_sampled": len(runs),
        "completed": len(completed),
        "pass_rate_pct": round(passed * 100.0 / len(completed), 1) if completed else None,
        "rerun_rate_pct": round(rerun_rate(runs), 1) if runs else None,
        "p50_min": round(p50, 1) if p50 is not None else None,
        "p90_min": round(p90, 1) if p90 is not None else None,
        "budget_min": budget_min,
        "green_streak": streak,
        "failed_runs": len(failed),
        "detail_sampled": len(failed_jobs),
        "failure_classes": classes,
        "failure_signatures": dict(sorted(signatures.items(), key=lambda kv: -kv[1])[:10]),
        "graduation": {
            "rule": f"streak>={GRADUATION_STREAK} and p90<budget",
            "eligible_now": should_graduate(streak, p90, budget_min),
        },
        "quarantine": {
            "rule": f">={QUARANTINE_THRESHOLD} infra/unknown failures in {QUARANTINE_WINDOW_DAYS}d",
            "proposed_now": quarantine,
        },
        "note": "first report = alert-delta baseline; deltas require a previous report",
    }


def to_markdown(reports: list[dict], alert_counts: dict | None) -> str:
    lines = [
        "# CI health report (plan Task 0.2)",
        "",
        "| workflow | sampled | pass% | rerun% | p50 | p90 | budget | streak | classes | graduate? | quarantine? |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        cls = ",".join(f"{k}:{v}" for k, v in r["failure_classes"].items() if v)
        lines.append(
            f"| {r['workflow']} | {r['runs_sampled']} | {r['pass_rate_pct']} | {r['rerun_rate_pct']} "
            f"| {r['p50_min']} | {r['p90_min']} | {r['budget_min']} | {r['green_streak']} | {cls or '-'} "
            f"| {'yes' if r['graduation']['eligible_now'] else 'no'} "
            f"| {'YES' if r['quarantine']['proposed_now'] else 'no'} |"
        )
    lines.append("")
    lines.append("## Top failure signatures")
    lines.append("(job detail sampled from the most recent failed runs only; unlisted runs are not counted)")
    for r in reports:
        if r["failed_runs"] > r["detail_sampled"]:
            lines.append(f"- `{r['workflow']}`: detail sampled for {r['detail_sampled']}/{r['failed_runs']} failed runs")
    sig_any = False
    for r in reports:
        for sig, n in r["failure_signatures"].items():
            sig_any = True
            lines.append(f"- `{r['workflow']}` {sig} (x{n})")
    if not sig_any:
        lines.append("- none in the sampled window")
    if alert_counts is not None:
        lines.append("")
        lines.append("## Open alert counts (baseline for deltas)")
        lines.append(f"- dependabot: open={alert_counts.get('open_total')}, "
                     f"critical={alert_counts.get('open_critical')}, high={alert_counts.get('open_high')}")
        lines.append("- osv: not collected in v1 (recorded in EVIDENCE.md E-06..E-08)")
    return "\n".join(lines) + "\n"


def gh_json(args: list[str], attempts: int = 2) -> dict | list:
    for i in range(attempts):
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
        if out.returncode == 0:
            return json.loads(out.stdout)
        transient = "HTTP 5" in out.stderr or "Server Error" in out.stderr
        if i + 1 < attempts and transient:
            print(f"warn: transient failure on `gh {' '.join(args[:3])}…`, retrying", file=sys.stderr)
            continue
        raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {out.stderr.strip()[:200]}")
    raise RuntimeError(f"gh {' '.join(args[:3])}… failed after {attempts} attempts")


def collect_live(workflows: list[str], limit: int, repo: str) -> tuple[list[dict], dict | None]:
    reports: list[dict] = []
    alerts = None
    for wf in workflows:
        runs = gh_json(["run", "list", "--repo", repo, f"--workflow={wf}", f"--limit", str(limit),
                        "--json", "databaseId,conclusion,createdAt,updatedAt,startedAt,status,attempt,url,event,displayTitle"])
        failed_jobs: dict[str, list[dict]] = {}
        for r in runs:
            if (r.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled"):
                rid = str(r.get("databaseId"))
                if len(failed_jobs) < 10:
                    try:
                        view = gh_json(["api", f"/repos/{repo}/actions/runs/{rid}/jobs?per_page=100"])
                        failed_jobs[rid] = view.get("jobs", []) if isinstance(view, dict) else view
                    except (RuntimeError, json.JSONDecodeError) as exc:
                        print(f"warn: jobs for run {rid}: {exc}", file=sys.stderr)
        reports.append(build_report(wf, runs, failed_jobs, DEFAULT_BUDGET_MIN))
    try:
        raw = gh_json(["api", "--paginate", f"/repos/{repo}/dependabot/alerts?state=open&per_page=100"])
        total = raw if isinstance(raw, list) else []
        crit_high = gh_json(["api", f"/repos/{repo}/dependabot/alerts?state=open&severity=critical&per_page=100"])
        high = gh_json(["api", f"/repos/{repo}/dependabot/alerts?state=open&severity=high&per_page=100"])
        n_crit = len(crit_high) if isinstance(crit_high, list) else 0
        n_high = len(high) if isinstance(high, list) else 0
        alerts = {"open_total": len(total),
                  "open_critical": n_crit,
                  "open_high": n_high,
                  "note": "counts capped at 100 per severity query; exact only if below cap"}
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"warn: dependabot alerts unavailable: {exc}", file=sys.stderr)
    return reports, alerts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workflows", default="ci.yml,install-e2e.yml,osv-scanner.yml,e2e-desktop.yml",
                    help="comma-separated workflow file names or display names")
    ap.add_argument("--limit", type=int, default=50, help="runs per workflow to sample")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--input", help="fixture JSON instead of live gh: {workflow: {runs: [...], failed_jobs: {...}}}")
    ap.add_argument("--out", help="output prefix (writes <prefix>.json and <prefix>.md)")
    args = ap.parse_args(argv)

    if args.input:
        fixture = json.loads(Path(args.input).read_text(encoding="utf-8"))
        reports = [build_report(wf, d.get("runs", []), d.get("failed_jobs", {}), DEFAULT_BUDGET_MIN)
                   for wf, d in fixture.items()]
        alerts = fixture.get("__alerts__")
    else:
        reports, alerts = collect_live([w.strip() for w in args.workflows.split(",") if w.strip()], args.limit, args.repo)

    payload = {"schema": REPORT_SCHEMA, "repo": args.repo,
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "reports": reports, "alert_baseline": alerts}
    text = to_markdown(reports, alerts)
    if args.out:
        Path(f"{args.out}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        Path(f"{args.out}.md").write_text(text, encoding="utf-8")
        print(f"wrote {args.out}.json and {args.out}.md")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
