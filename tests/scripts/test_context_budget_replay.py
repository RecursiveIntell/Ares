"""The replay gate must be able to say "no".

These tests pin the adoption rule so it cannot be quietly satisfied by a
favourable-looking report.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "context_budget_replay.py"


def _load():
    spec = importlib.util.spec_from_file_location("context_budget_replay", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load()

ROWS = [
    {"session": "s", "original": 74_000, "compacted": 66_000, "reduction": 0.108},
    {"session": "s", "original": 405_000, "compacted": 22_000, "reduction": 0.946},
    {"session": "s", "original": 10_000, "compacted": 9_000, "reduction": 0.100},
]


def test_before_policy_reproduces_the_absolute_knobs():
    before = replay.before_policy(1_000_000)
    assert before["target"] == 200_000
    assert before["summary"] == 8_000
    assert before["head_cap"] == 75_000


def test_after_policy_uses_the_canonical_budget():
    after = replay.after_policy(1_000_000)
    assert after["target"] > 200_000
    assert after["summary"] > 8_000
    assert after["feasible"] is True
    assert after["head_cap"] != 75_000, "head cap must scale with the window"


def test_replay_reports_both_policies_and_observed_churn():
    report = replay.replay(ROWS, 1_000_000)
    assert report["passes"] == 3
    assert report["summary_gain_x"] > 7
    assert report["observed"]["near_no_op_passes"] == 0
    assert report["after"]["summary_share_of_reclaim"] > report["before"]["summary_share_of_reclaim"]


def test_adoption_rejects_any_infeasible_window():
    reports = [replay.replay(ROWS, 1_000_000)]
    reports[0]["after"]["feasible"] = False
    verdict = replay.adoption_verdict(reports)
    assert verdict["adopted"] is False
    assert verdict["infeasible_windows"] == [1_000_000]


def test_adoption_rejects_fewer_avoided_passes():
    """More compactions required is a regression; more avoided ones is not."""
    reports = [replay.replay(ROWS, 128_000)]
    reports[0]["after"]["passes_avoided"] = reports[0]["before"]["passes_avoided"] - 1
    verdict = replay.adoption_verdict(reports)
    assert verdict["adopted"] is False
    assert verdict["windows_with_fewer_passes_avoided"] == [128_000]


def test_adoption_accepts_more_avoided_passes_as_an_improvement():
    reports = [replay.replay(ROWS, 128_000)]
    reports[0]["after"]["passes_avoided"] = reports[0]["before"]["passes_avoided"] + 1
    assert replay.adoption_verdict(reports)["adopted"] is True


def test_adoption_accepts_a_clean_comparison():
    reports = [replay.replay(ROWS, 1_000_000), replay.replay(ROWS, 128_000)]
    verdict = replay.adoption_verdict(reports)
    assert verdict["infeasible_windows"] == []
    assert verdict["retention_improved_everywhere"] is True
