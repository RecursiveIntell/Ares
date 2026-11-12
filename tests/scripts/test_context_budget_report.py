"""Phase-0 truth artifact: the context-budget report must describe real behaviour.

These tests pin the *current* semantics the reporter claims to describe. If the
live plugin changes its target/trigger/summary arithmetic, this test fails and
forces the baseline to be re-frozen rather than silently mis-documented.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "context_budget_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("context_budget_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load()

REQUIRED_ROW_KEYS = {
    "provider", "model", "window", "reserve", "effective", "trigger", "target",
    "summary", "head_cap", "tail_cap", "margin", "floor", "feasible",
}


def test_report_declares_schema_and_inputs():
    doc = report.build_report("before")
    assert doc["schema"] == "ares-context-budget-report/v1"
    assert doc["policy"] == "before"
    inputs = doc["inputs"]
    assert inputs["target_ratio"] == 0.20
    assert inputs["head_cap_tokens"] == 75_000
    assert inputs["tail_messages"] == 20


def test_every_configured_model_gets_a_full_row():
    doc = report.build_report("before")
    assert doc["models"], "expected at least one configured model"
    for row in doc["models"]:
        assert REQUIRED_ROW_KEYS <= set(row), row
        assert row["provider"] and row["model"]


def test_one_million_window_reproduces_the_documented_baseline():
    """1M window with today's config must yield trigger 750k, target 200k, summary 8k."""
    row = report.before_row(
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        window=1_000_000,
        output_reserve=None,
        threshold_percent=0.75,
        summary_max_chars=32_000,
    )
    assert row["effective"] == 1_000_000, "reserve is never wired, so effective == window"
    assert row["trigger"] == 750_000
    assert row["target"] == 200_000
    assert row["summary"] == 8_000, "8000-token literal must bind at 32k chars"
    assert row["head_cap"] == 75_000
    assert row["tail_cap"] is None and row["floor"] is None and row["feasible"] is None


def test_smaller_window_scales_trigger_and_target_but_not_the_literals():
    """The absolutes are exactly what fails to scale; the report must show it."""
    row = report.before_row(
        provider="ollama-launch", model="some-128k", window=128_000,
        output_reserve=None, threshold_percent=0.75, summary_max_chars=32_000,
    )
    assert row["trigger"] == 96_000
    assert row["target"] == 25_600
    assert row["head_cap"] == 75_000, "fixed head cap exceeds a third of a 128k window"
    assert row["head_cap"] > row["target"], "this is the D-002 defect made visible"


def test_unresolved_window_is_reported_not_crashed():
    row = report.before_row(
        provider="opencode-go", model="unknown", window=None,
        output_reserve=None, threshold_percent=0.75, summary_max_chars=8_000,
    )
    assert row["window"] is None
    assert "window unresolved" in row["notes"]


def test_cli_emits_parseable_json(capsys):
    code = report.main(["--json", "--policy", "before"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "ares-context-budget-report/v1"


def test_after_policy_delegates_to_the_canonical_owner():
    """No arithmetic may be duplicated in the reporter (plan invariant 7)."""
    from ares_runtime.context_budget import ContextBudget, resolve_output_reserve

    for window in (128_000, 1_000_000, 2_000_000):
        row = report.after_row("p", "m", window, None)
        reserve = resolve_output_reserve(None, window)
        expected = ContextBudget.from_window(window, reserve).to_dict()
        for key, value in expected.items():
            assert row[key] == value, f"{key} diverged at W={window}"
        assert row["provider"] == "p" and row["model"] == "m"


def test_after_policy_improves_retention_over_before():
    """The whole point: the same window must retain far more meaning."""
    from ares_runtime.context_budget import resolve_output_reserve

    before = report.before_row("p", "m", 1_000_000, None, 0.75, 32_000)
    after = report.after_row("p", "m", 1_000_000, None)
    reserve = resolve_output_reserve(None, 1_000_000)
    before_retention = before["summary"] / before["reclaim"]

    assert before["summary"] == 8_000
    assert after["reserve"] == reserve
    assert after["summary"] == 65_288, "0.15 x 0.45 x (1M - 32768)"
    assert after["retention"] > 7 * before_retention
    assert after["feasible"] is True and before["feasible"] is None
