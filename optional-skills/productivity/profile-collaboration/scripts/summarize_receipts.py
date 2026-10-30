#!/usr/bin/env python3
"""Summarize profile-collaboration receipts without reading report contents."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 3)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": nearest_rank(values, 0.50),
        "p95": nearest_rank(values, 0.95),
        "max": round(max(values), 3) if values else None,
    }


def summarize(paths: Iterable[Path], diagnose: bool = False) -> dict[str, Any]:
    schema_counts: Counter[str] = Counter()
    invalid_receipts: list[dict[str, str]] = []
    lanes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parsed_receipts = 0
    receipt_by_run: dict[str, dict[str, Any]] = {}

    for path in paths:
        try:
            panel = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            invalid_receipts.append({"path": str(path), "error": type(exc).__name__})
            continue
        if not isinstance(panel, dict):
            invalid_receipts.append({"path": str(path), "error": "root_not_object"})
            continue
        parsed_receipts += 1
        schema_counts[str(panel.get("schema", "missing"))] += 1
        receipt_by_run[str(panel.get("run_id") or path.parent.name)] = panel
        for result in panel.get("results", []):
            if isinstance(result, dict) and isinstance(result.get("profile"), str):
                lanes[result["profile"]].append(result)

    profiles: dict[str, dict[str, Any]] = {}
    for profile in sorted(lanes):
        rows = lanes[profile]
        outcomes = Counter(str(row.get("outcome", "missing")) for row in rows)
        durations = [
            float(row["duration_seconds"])
            for row in rows
            if isinstance(row.get("duration_seconds"), (int, float))
        ]
        waits = [
            float(row["admission_wait_seconds"])
            for row in rows
            if isinstance(row.get("admission_wait_seconds"), (int, float))
        ]
        weights = sorted(
            {
                int(row["resource_weight"])
                for row in rows
                if isinstance(row.get("resource_weight"), int)
            }
        )
        # Failure-shape dimensions added 2026-10-20: these distinguish busy
        # scope exhaustion from provider failure in aggregates.
        shapes = Counter(
            str(row.get("termination_reason") or "unrecorded") for row in rows
            if str(row.get("outcome", "missing")) not in ("returned",)
        )
        review_modes = Counter(
            str(row.get("review_mode") or "unrecorded") for row in rows
        )
        profiles[profile] = {
            "lanes": len(rows),
            "returned": outcomes.get("returned", 0),
            "return_rate": round(outcomes.get("returned", 0) / len(rows), 4) if rows else None,
            "outcomes": dict(sorted(outcomes.items())),
            "duration_seconds": distribution(durations),
            "admission_wait_seconds": distribution(waits),
            "observed_resource_weights": weights,
            "termination_reasons_nonreturned": dict(sorted(shapes.items())),
            "review_modes": dict(sorted(review_modes.items())),
        }

    report: dict[str, Any] = {
        "schema": "AresProfilePanelTelemetryV2",
        "generated_at": utc_now(),
        "receipt_count": parsed_receipts,
        "invalid_receipt_count": len(invalid_receipts),
        "invalid_receipts": invalid_receipts,
        "receipt_schemas": dict(sorted(schema_counts.items())),
        "lane_count": sum(item["lanes"] for item in profiles.values()),
        "profiles": profiles,
        "interpretation_boundary": [
            "Duration and admission wait are scheduling telemetry, not memory attribution.",
            "Resource weights are configuration-derived until synchronized process metrics exist.",
            "A returned lane is not semantic approval; controller review remains required.",
            "review_mode/termination_reason are failure-shape dimensions recorded from 2026-10-20; "
            "older receipts legitimately read 'unrecorded'.",
        ],
    }
    if diagnose:
        report["timeout_diagnosis"] = timeout_diagnosis(receipt_by_run)
    return report


def timeout_diagnosis(receipt_by_run: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Failure-shape breakdown over timed-out lanes, ready for one-command triage."""
    workspace_runs: dict[str, list[dict[str, str]]] = defaultdict(list)
    modes: Counter[str] = Counter()
    toolsets: Counter[str] = Counter()
    mixed_eras: dict[str, Counter[str]] = defaultdict(Counter)
    for run_id, panel in receipt_by_run.items():
        ws = str(panel.get("workspace") or "unknown")
        mode = str(panel.get("review_mode") or "unrecorded")
        toolset = str(panel.get("lane_toolsets") or "unrecorded")
        for lane in panel.get("results", []):
            if not isinstance(lane, dict) or lane.get("outcome") != "timed_out":
                continue
            profile = lane.get("profile", "?")
            workspace_runs[ws].append({"run": run_id, "profile": profile})
            modes[f"{profile}:{mode}" if False else mode] += 1
            toolsets[toolset] += 1
            key = f"{ws}|{profile}"
            mixed_eras[key][mode] += 1
    return {
        "timed_out_lanes": sum(sum(c.values()) for c in mixed_eras.values()),
        "note": (
            "These are receipt-only signals. To confirm busy-scope exhaustion vs idle provider, "
            "follow the SKILL.md pitfall: read the exact session_id in the profile's state.db "
            "(read-only) and check billing_provider/input_tokens/output_tokens plus message timestamps."
        ),
        "workspace_hotspots": dict(
            sorted(
                ((ws, len(entries)) for ws, entries in workspace_runs.items()),
                key=lambda item: item[1],
                reverse=True,
            )
        ),
        "review_mode_distribution_on_timeouts": dict(modes),
        "lane_toolsets_distribution_on_timeouts": dict(toolsets),
        "pre_2026_10_20_receipts_have_unrecorded_dimensions": (
            any(mode == "unrecorded" for mode in modes)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize profile-collaboration receipt telemetry")
    parser.add_argument(
        "--receipts",
        type=Path,
        default=Path.home() / ".ares" / "profile-collaboration" / "receipts",
    )
    parser.add_argument("--limit", type=int, default=0, help="Newest N receipts; 0 means all")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--diagnose-timeouts",
        action="store_true",
        help=(
            "Add a failure-shape breakdown for timed-out lanes: workspace hotspots, review"
            " modes, and lane toolsets, so a single command distinguishes scope exhaustion"
            " from provider failure."
        ),
    )
    args = parser.parse_args()
    root = args.receipts.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"receipt directory does not exist: {root}")
    if args.limit < 0:
        raise SystemExit("limit must be non-negative")
    paths = sorted(root.glob("*/panel.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if args.limit:
        paths = paths[: args.limit]
    report = summarize(paths, diagnose=args.diagnose_timeouts)
    payload = json.dumps(report, indent=2) + "\n"
    if args.out:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
