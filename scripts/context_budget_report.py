#!/usr/bin/env python3
"""Emit the resolved context-budget table for every configured model.

This is the Phase-0 truth artifact for the context-budget programme. It reports
what the system decides TODAY, including every place where a term is an absolute
constant or is simply unbounded — which is the point: the "before" table must show
that no admission invariant exists, not paper over it.

Later phases replace this arithmetic with ``ares_runtime.context_budget``; this
module stays as the reporter and gains a ``--policy after`` mode.

Usage:
    python scripts/context_budget_report.py --json
    python scripts/context_budget_report.py --json --policy before > budget-before.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "ares-context-budget-report/v1"

# Current (pre-fix) semantics, transcribed from the live plugin and crate.
# Each is asserted by tests/scripts/test_context_budget_report.py so this
# snapshot cannot silently drift from the behaviour it claims to describe.
BEFORE_TARGET_RATIO = 0.20          # _target_tokens: 20% of context_length
BEFORE_HEAD_CAP_TOKENS = 75_000     # AGGRESSIVE_HEAD_KEEP_MAX_TOKENS
BEFORE_SUMMARY_CEILING_TOKENS = 8_000   # min(5% window, 8000, chars//4)
BEFORE_SUMMARY_WINDOW_RATIO = 0.05
BEFORE_TAIL_MESSAGES = 20           # protect_last_n: message count, not tokens


def _config() -> dict[str, Any]:
    import yaml

    path = Path.home() / ".ares/config.yaml"
    return yaml.safe_load(path.read_text()) or {}


def configured_models(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (provider, model) pair the host can actually select."""
    pairs: set[tuple[str, str]] = set()
    model_cfg = cfg.get("model") or {}
    if model_cfg.get("default") and model_cfg.get("provider"):
        pairs.add((model_cfg["provider"], model_cfg["default"]))

    for provider, block in (cfg.get("providers") or {}).items():
        if not isinstance(block, dict):
            continue
        if block.get("default_model"):
            pairs.add((provider, block["default_model"]))
        for name in (block.get("models") or {}):
            pairs.add((provider, name))

    for provider, block in (cfg.get("model_overrides") or {}).items():
        if isinstance(block, dict):
            for name in block:
                if name != "_default":
                    pairs.add((provider, name))
    return sorted(pairs)


def resolve_window(provider: str, model: str) -> int | None:
    try:
        from agent.model_metadata import get_model_context_length

        value = get_model_context_length(model, provider=provider)
        return int(value) if value else None
    except Exception:  # noqa: BLE001 - a report must not die on one model
        return None


def before_row(provider: str, model: str, window: int | None,
               output_reserve: int | None, threshold_percent: float,
               summary_max_chars: int) -> dict[str, Any]:
    """Rows describing CURRENT behaviour, including unbounded/absent terms."""
    if not window:
        return {"provider": provider, "model": model, "window": None,
                "notes": ["window unresolved"]}

    reserve = output_reserve or 0
    effective = window - reserve
    trigger = int(effective * threshold_percent) if effective > 0 else 0
    target = max(512, int(window * BEFORE_TARGET_RATIO))
    if effective > 0:
        target = min(target, effective)
    summary = min(
        int(window * BEFORE_SUMMARY_WINDOW_RATIO) if window else 4_000,
        BEFORE_SUMMARY_CEILING_TOKENS,
        max(256, summary_max_chars) // 4,
    )
    return {
        "provider": provider,
        "model": model,
        "window": window,
        "reserve": output_reserve,
        "effective": effective,
        "trigger": trigger,
        "target": target,
        "summary": summary,
        "head_cap": BEFORE_HEAD_CAP_TOKENS,
        "tail_cap": None,
        "margin": None,
        "floor": None,
        "feasible": None,
        "reclaim": trigger - target,
        "notes": [
            "tail bounded by message count (%d), not tokens" % BEFORE_TAIL_MESSAGES,
            "no admission invariant: head+tail+summary vs target is unchecked",
            "output reserve is never passed by any caller, so effective == window",
        ],
    }


def after_row(provider: str, model: str, window: int | None,
              output_reserve: int | None) -> dict[str, Any]:
    """Rows from the canonical budget owner. No arithmetic lives here."""
    if not window:
        return {"provider": provider, "model": model, "window": None,
                "notes": ["window unresolved"]}
    from ares_runtime.context_budget import (
        BudgetFloorInfeasible,
        ContextBudget,
        resolve_output_reserve,
    )

    try:
        reserve = resolve_output_reserve(output_reserve, window)
        budget = ContextBudget.from_window(window, reserve)
    except BudgetFloorInfeasible as exc:
        return {
            "provider": provider, "model": model, "window": window,
            "reserve": output_reserve, "feasible": False,
            "target": exc.target, "floor": exc.floor, "terms": exc.terms,
            "notes": ["floor infeasible; caller must refuse or shrink the route"],
        }
    row = {"provider": provider, "model": model}
    row.update(budget.to_dict())
    return row


def build_report(policy: str = "before") -> dict[str, Any]:
    cfg = _config()
    compression = cfg.get("compression") or {}
    governor = (cfg.get("context") or {}).get("governor") or {}
    threshold_percent = float(compression.get("threshold") or 0.50)
    summary_max_chars = int(governor.get("summary_max_chars") or 8_000)

    rows = []
    for provider, model in configured_models(cfg):
        window = resolve_window(provider, model)
        if policy == "after":
            rows.append(after_row(provider, model, window, None))
        else:
            rows.append(
                before_row(provider, model, window, None, threshold_percent,
                           summary_max_chars)
            )

    return {
        "schema": SCHEMA,
        "policy": policy,
        "inputs": {
            "threshold_percent": threshold_percent,
            "summary_max_chars": summary_max_chars,
            "target_ratio": BEFORE_TARGET_RATIO,
            "head_cap_tokens": BEFORE_HEAD_CAP_TOKENS,
            "tail_messages": BEFORE_TAIL_MESSAGES,
        },
        "models": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON (default)")
    parser.add_argument("--policy", choices=("before", "after"), default="before")
    args = parser.parse_args(argv)

    report = build_report(args.policy)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
