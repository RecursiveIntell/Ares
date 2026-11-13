"""ContextBudget is the only arithmetic owner; these tests are its specification.

The properties here are the plan's Phase-1 gate: feasibility, positivity,
ordering, and monotonicity across the full window range the host can serve.
"""
from __future__ import annotations

import pytest

from ares_runtime.context_budget import (
    SCHEMA,
    BudgetFloorInfeasible,
    BudgetRatios,
    ContextBudget,
)

WINDOWS = [8_000, 16_000, 32_000, 64_000, 128_000, 200_000, 400_000,
           1_000_000, 2_000_000]
RESERVES = [0, 8_192, 32_768]


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("reserve", RESERVES)
def test_floor_always_fits_target(window, reserve):
    """The plan's core invariant, for every supported route."""
    if reserve >= window:
        pytest.skip("reserve leaves no input budget")
    budget = ContextBudget.from_window(window, reserve)
    assert budget.feasible is True
    assert budget.floor <= budget.target, (
        f"W={window} R={reserve}: floor {budget.floor} > target {budget.target}"
    )


@pytest.mark.parametrize("window", WINDOWS)
def test_every_term_is_positive_and_ordered(window):
    budget = ContextBudget.from_window(window)
    for name in ("trigger", "target", "summary", "head_cap", "tail_cap", "margin",
                 "floor", "effective"):
        assert getattr(budget, name) > 0, f"{name} must be positive at W={window}"
    assert budget.target < budget.trigger
    assert budget.summary <= budget.target


def test_summary_never_shrinks_as_the_window_grows():
    """Monotonicity: a bigger model must never get a smaller summary."""
    previous = 0
    for window in WINDOWS:
        budget = ContextBudget.from_window(window)
        assert budget.summary >= previous, f"summary regressed at W={window}"
        previous = budget.summary


def test_retention_is_stable_across_windows():
    """Proportional terms give a constant summary:reclaimed ratio."""
    ratios = [
        ContextBudget.from_window(w).retention
        for w in (32_000, 128_000, 1_000_000, 2_000_000)
    ]
    assert max(ratios) - min(ratios) < 0.01, ratios
    assert all(0.15 < r < 0.25 for r in ratios), ratios


def test_one_million_matches_the_designed_numbers():
    budget = ContextBudget.from_window(1_000_000)
    assert budget.trigger == 800_000
    assert budget.target == 450_000
    assert budget.summary == 67_500
    assert budget.head_cap == 100_000
    assert budget.tail_cap == 200_000
    assert budget.margin == 20_000
    assert budget.floor == 387_500
    assert budget.reclaimed == 350_000


def test_reserve_is_honoured():
    budget = ContextBudget.from_window(1_000_000, reserve=65_536)
    assert budget.effective == 934_464
    assert budget.trigger == int(0.80 * 934_464)
    assert budget.feasible is True


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        ContextBudget.from_window(0)
    with pytest.raises(ValueError):
        ContextBudget.from_window(1_000, reserve=1_000)


def test_infeasible_ratios_are_rejected_by_validation():
    """A ratio edit that breaks the identity must fail loudly, not silently."""
    with pytest.raises(ValueError, match="infeasible|must exceed|must be in"):
        BudgetRatios(target=0.30, head=0.20, tail=0.20).validate()
    with pytest.raises(ValueError):
        BudgetRatios(trigger=0.40, target=0.45).validate()


def test_runtime_infeasibility_is_typed_when_scale_down_cannot_rescue():
    """A tiny window trips the typed error, never a silently unsafe plan.

    The margin floor (512 tokens) is absolute, so below roughly 1.4k effective
    tokens it exceeds its 2% ratio and cannot be scaled down. That is the one
    reachable runtime-infeasible case, and it must surface as a typed breakdown
    rather than an over-budget plan.
    """
    with pytest.raises(BudgetFloorInfeasible) as excinfo:
        ContextBudget.from_window(1_000)
    err = excinfo.value
    assert err.floor > err.target
    assert set(err.terms) == {"summary", "head", "tail", "margin"}
    assert "floor" in str(err) and "target" in str(err)


def test_to_dict_is_stable_and_self_describing():
    doc = ContextBudget.from_window(128_000).to_dict()
    assert doc["schema"] == SCHEMA
    assert doc["window"] == 128_000
    assert doc["feasible"] is True
    assert doc["ratios"]["target"] == 0.45


def test_output_reserve_is_derived_when_the_host_supplies_none():
    """`agent.max_tokens: None` means 'model default', not 'reserve nothing'."""
    from ares_runtime.context_budget import resolve_output_reserve

    assert resolve_output_reserve(None, 1_000_000) == 32_768
    assert resolve_output_reserve(None, 128_000) == 6_400
    assert resolve_output_reserve(None, 8_000) == 1_024
    assert resolve_output_reserve(0, 128_000) == 6_400, "0 is not a reservation"
    assert resolve_output_reserve(None, 0) == 0

    # An explicit host value wins and is clamped to leave an input budget.
    assert resolve_output_reserve(65_536, 1_000_000) == 65_536
    assert resolve_output_reserve(10_000_000, 1_000_000) == 1_000_000 - 1


def test_reserve_shrinks_the_effective_window():
    from ares_runtime.context_budget import resolve_output_reserve

    reserve = resolve_output_reserve(None, 1_000_000)
    budget = ContextBudget.from_window(1_000_000, reserve)
    assert budget.effective == 1_000_000 - 32_768
    assert budget.target < 450_000, "target must scale with the effective window"
    assert budget.feasible is True
