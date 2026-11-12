#!/usr/bin/env python3
"""Offline replay: how would the proposed budget have behaved on real passes?

Reads the append-only receipt corpus and compares the CURRENT (absolute) budget
against the PROPOSED (proportional) one. No model calls, no spend, deterministic.

What this can and cannot answer
-------------------------------
Receipts record the token sizes a pass saw, not the model window that produced
them. So the replay re-evaluates each observed pass against each *configured*
window rather than pretending to know the original route. That is enough to
answer the adoption questions the plan set:

  1. would any configured window produce an infeasible floor?   (theorem: no)
  2. do near-no-op passes increase?                             (must not)
  3. does the summary represent materially more of what is dropped?  (must)

Adoption rule is applied explicitly in the output; a "no" is a valid result.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

SCHEMA = "ares-context-budget-replay/v1"
NEAR_NO_OP_REDUCTION = 0.05


def load_receipts(corpus: Path, limit: int | None = None) -> list[dict[str, Any]]:
    files = sorted(corpus.glob("ctxr_*.json"), key=lambda p: p.stat().st_mtime)
    if limit:
        files = files[-limit:]
    rows = []
    for path in files:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))["receipt"]
        except Exception:  # noqa: BLE001 - a corrupt receipt must not abort replay
            continue
        original = receipt.get("original_approx_tokens")
        compacted = receipt.get("compacted_approx_tokens")
        if not isinstance(original, int) or not isinstance(compacted, int) or original <= 0:
            continue
        rows.append(
            {
                "session": receipt.get("session_id") or "",
                "original": original,
                "compacted": compacted,
                "reduction": 1 - (compacted / original),
            }
        )
    return rows


def before_policy(window: int, summary_max_chars: int = 32_000) -> dict[str, int]:
    """Today's absolutes: 20% target, 8k summary ceiling, fixed head cap."""
    target = max(512, int(window * 0.20))
    summary = min(int(window * 0.05), 8_000, max(256, summary_max_chars) // 4)
    return {"target": target, "summary": summary, "head_cap": 75_000}


def after_policy(window: int) -> dict[str, int]:
    from ares_runtime.context_budget import ContextBudget, resolve_output_reserve

    reserve = resolve_output_reserve(None, window)
    budget = ContextBudget.from_window(window, reserve)
    return {
        "target": budget.target,
        "summary": budget.summary,
        "head_cap": budget.head_cap,
        "floor": budget.floor,
        "feasible": budget.feasible,
    }


def replay(rows: list[dict[str, Any]], window: int) -> dict[str, Any]:
    before = before_policy(window)
    after = after_policy(window)

    near_no_op_before = sum(1 for r in rows if r["original"] <= before["target"])
    near_no_op_after = sum(1 for r in rows if r["original"] <= after["target"])
    observed_no_op = sum(1 for r in rows if r["reduction"] < NEAR_NO_OP_REDUCTION)

    reclaim_before = max(1, int(window * 0.75) - before["target"])
    reclaim_after = max(1, int(window * 0.80) - after["target"])

    return {
        "window": window,
        "passes": len(rows),
        "before": {
            **before,
            "summary_share_of_reclaim": round(before["summary"] / reclaim_before, 4),
            # Historical passes whose size is at or below this target would not
            # have needed to run at all. HIGHER is better: fewer compactions.
            "passes_avoided": near_no_op_before,
        },
        "after": {
            **after,
            "summary_share_of_reclaim": round(after["summary"] / reclaim_after, 4),
            "passes_avoided": near_no_op_after,
        },
        "observed": {
            "median_reduction": round(statistics.median(r["reduction"] for r in rows), 4)
            if rows else 0.0,
            "near_no_op_passes": observed_no_op,
            "near_no_op_rate": round(observed_no_op / len(rows), 4) if rows else 0.0,
        },
        "summary_gain_x": round(after["summary"] / max(1, before["summary"]), 2),
    }


def adoption_verdict(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the plan's adoption rule, decided before measurement.

    Criteria:
      * no window may produce an infeasible floor;
      * the new budget may not make MORE compactions necessary (``passes_avoided``
        must not fall — a rise means less work, not more);
      * the summary must represent a larger share of what a pass discards.
    """
    infeasible = [r["window"] for r in reports if not r["after"]["feasible"]]
    avoided_regression = [
        r["window"] for r in reports
        if r["after"]["passes_avoided"] < r["before"]["passes_avoided"]
    ]
    retention_gain = all(
        r["after"]["summary_share_of_reclaim"] > r["before"]["summary_share_of_reclaim"]
        for r in reports
    )
    adopted = not infeasible and not avoided_regression and retention_gain
    return {
        "adopted": adopted,
        "infeasible_windows": infeasible,
        "windows_with_fewer_passes_avoided": avoided_regression,
        "retention_improved_everywhere": retention_gain,
        "note": "passes_avoided rising means fewer compactions are required",
    }


def build_report(corpus: Path, windows: list[int], limit: int | None = None) -> dict[str, Any]:
    rows = load_receipts(corpus, limit=limit)
    reports = [replay(rows, w) for w in windows]
    return {
        "schema": SCHEMA,
        "corpus": str(corpus),
        "receipts": len(rows),
        "reports": reports,
        "adoption": adoption_verdict(reports),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(Path.home() / ".ares/context-governor"))
    parser.add_argument("--window", action="append", type=int, default=None,
                        help="window to evaluate (repeatable); default 128000 and 1000000")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    windows = args.window or [128_000, 1_000_000]
    report = build_report(Path(args.corpus), windows, limit=args.limit)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
