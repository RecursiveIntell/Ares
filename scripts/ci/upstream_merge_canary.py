#!/usr/bin/env python3.14
"""Upstream merge canary (plan Task 6.3, A-14).

Fetches upstream ``main`` (NousResearch/hermes-agent) and runs
``git merge-tree --write-tree --name-only`` of it against the local ``HEAD``
to measure, without building anything, how large an upstream sync would be:
conflict count and conflicting paths grouped into Ares-owned vs inherited
directories. Read-only: no merge, no push, no working-tree change.

Trend: pass ``--previous`` (a previous report JSON) to include the conflict
delta versus the previous run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPORT_SCHEMA = "AresUpstreamMergeCanaryV1"
DEFAULT_UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"

ARES_OWNED_PREFIXES = (
    "ares_runtime/",
    "scripts/ci/",
    "scripts/release/",
    "docs/ci/",
    ".github/workflows/ares-",
    "release/",
)


def classify(path: str) -> str:
    return "ares-owned" if path.startswith(ARES_OWNED_PREFIXES) else "inherited"


def sh(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=600)


def merge_tree_conflicts(repo: str, upstream: str, ref: str) -> tuple[dict, str | None]:
    """Fetch upstream ref and merge-tree it against HEAD. Returns (report, error)."""
    fetch = sh(["git", "fetch", "--no-tags", upstream, f"refs/heads/{ref}"], cwd=repo)
    if fetch.returncode != 0:
        return {}, f"git fetch failed: {fetch.stderr.strip()[:200]}"

    theirs = sh(["git", "rev-parse", "FETCH_HEAD"], cwd=repo)
    ours = sh(["git", "rev-parse", "HEAD"], cwd=repo)
    if theirs.returncode or ours.returncode:
        return {}, "could not resolve HEAD/FETCH_HEAD"
    theirs_sha, ours_sha = theirs.stdout.strip(), ours.stdout.strip()

    proc = sh(["git", "merge-tree", "--write-tree", "--name-only", ours_sha, theirs_sha], cwd=repo)
    # Exit 0 = clean merge; 1 = conflicts; anything else = canary failure.
    if proc.returncode not in (0, 1):
        return {}, f"git merge-tree failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"

    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    # --write-tree output: first non-empty line is the result tree OID;
    # with --name-only the remaining lines are conflicted paths.
    tree_oid = lines[0] if lines else ""
    conflicts = [l for l in lines[1:] if not l.startswith("Auto-merging")]
    ares_owned = sorted({c for c in conflicts if classify(c) == "ares-owned"})
    inherited = sorted({c for c in conflicts if classify(c) == "inherited"})
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream": upstream,
        "ref": ref,
        "ours": ours_sha,
        "theirs": theirs_sha,
        "clean_merge": proc.returncode == 0,
        "conflict_count": len(conflicts),
        "ares_owned_paths": ares_owned,
        "inherited_paths": inherited,
    }, None


def to_markdown(report: dict, previous: dict | None) -> str:
    lines = [
        "# Upstream merge canary (plan Task 6.3)",
        "",
        f"- upstream: `{report['upstream']}` @{report['theirs'][:12]}",
        f"- Ares HEAD: `{report['ours'][:12]}`",
        f"- clean merge: **{report['clean_merge']}**",
        f"- conflicting paths: **{report['conflict_count']}**",
    ]
    if previous is not None and "conflict_count" in previous:
        delta = report["conflict_count"] - previous["conflict_count"]
        trend = "no change" if delta == 0 else ("+" if delta > 0 else "")
        lines.append(f"- trend vs previous report ({previous.get('generated_at', 'unknown')}): "
                     f"{report['conflict_count']} vs {previous['conflict_count']} ({trend}{delta})")
    if report["ares_owned_paths"]:
        lines += ["", "## Ares-owned conflicting paths", ""] + [f"- `{p}`" for p in report["ares_owned_paths"]]
    if report["inherited_paths"]:
        lines += ["", "## Inherited conflicting paths (first 30)", ""]
        lines += [f"- `{p}`" for p in report["inherited_paths"][:30]]
        if len(report["inherited_paths"]) > 30:
            lines.append(f"- …and {len(report['inherited_paths']) - 30} more")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--repo", default=".", help="Ares checkout to run in")
    ap.add_argument("--previous", help="previous report JSON for the trend line")
    ap.add_argument("--out", help="output prefix (writes <out>.json and <out>.md)")
    args = ap.parse_args(argv)

    report, error = merge_tree_conflicts(args.repo, args.upstream, args.ref)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    previous = json.loads(Path(args.previous).read_text(encoding="utf-8")) if args.previous else None
    text = to_markdown(report, previous)
    if args.out:
        Path(f"{args.out}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        Path(f"{args.out}.md").write_text(text, encoding="utf-8")
        print(f"wrote {args.out}.json and {args.out}.md")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
