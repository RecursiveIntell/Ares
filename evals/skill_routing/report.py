"""Summarize a skill-routing eval label.

Reports, per arm, the mean score and the mean number of skills loaded, split
by task family ("relevant" vs "abstain"). The claim under test is specific:

  * ``relevant`` scores should be ~equal across arms (the task-fit prose must
    not cause a directly-applicable skill to go unloaded), and
  * ``abstain`` load counts should DROP in the candidate arm (the over-eager
    "err on the side of loading" baseline is expected to load skills on tasks
    where none apply).

Usage:
  python3 evals/skill_routing/report.py --labels pre-fix task-fit
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

RESULTS = Path(__file__).resolve().parent / "results"


def load(label: str) -> list[dict]:
    root = RESULTS / label
    if not root.is_dir():
        raise SystemExit(f"no results for label '{label}' under {root}")
    rows: list[dict] = []
    for f in sorted(root.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def summarize(rows: list[dict]) -> dict:
    """-> {arm: {family: {n, mean_score, mean_loads}}} plus overall."""
    agg: dict = defaultdict(lambda: defaultdict(lambda: {"scores": [], "loads": []}))
    for r in rows:
        arm = r["arm"]
        fam = r.get("family", "?")
        for key in (fam, "all"):
            agg[arm][key]["scores"].append(r["score"])
            agg[arm][key]["loads"].append(r["n_loads"])
    out: dict = {}
    for arm, fams in agg.items():
        out[arm] = {}
        for fam, d in fams.items():
            out[arm][fam] = {
                "n": len(d["scores"]),
                "mean_score": round(mean(d["scores"]), 3) if d["scores"] else 0.0,
                "mean_loads": round(mean(d["loads"]), 3) if d["loads"] else 0.0,
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = {label: summarize(load(label)) for label in args.labels}
    if args.json:
        print(json.dumps(result, indent=2))
        return

    for label, summ in result.items():
        print(f"\n=== {label} ===")
        print(f"  {'arm':<8} {'family':<10} {'n':>4} {'score':>7} {'loads':>7}")
        for arm in summ:
            for fam in ("relevant", "abstain", "all"):
                if fam not in summ[arm]:
                    continue
                s = summ[arm][fam]
                print(f"  {arm:<6} {fam:<10} {s['n']:>4} "
                      f"{s['mean_score']:>7.3f} {s['mean_loads']:>7.3f}")

    if len(result) == 2:
        labels = list(result)
        a, b = result[labels[0]], result[labels[1]]
        print(f"\n=== delta ({labels[1]} - {labels[0]}) ===")
        for arm in ("base", "cand"):
            for fam in ("relevant", "abstain"):
                if arm in a and arm in b and fam in a[arm] and fam in b[arm]:
                    ds = b[arm][fam]["mean_score"] - a[arm][fam]["mean_score"]
                    dl = b[arm][fam]["mean_loads"] - a[arm][fam]["mean_loads"]
                    print(f"  {arm:<6} {fam:<10} score {ds:+.3f}  loads {dl:+.3f}")


if __name__ == "__main__":
    main()
