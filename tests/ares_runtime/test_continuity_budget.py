from dataclasses import replace

import pytest

from ares_runtime.continuity.budget import (
    BudgetError, Cause, CountMethod, Disposition, InputCount, PressurePolicy,
    RouteBudget, decide_pressure, stateless_payload_token_upper_bound,
)


def count(tokens, route="route:1", method=CountMethod.QUALIFIED_UPPER_BOUND):
    return InputCount(route, "sha256:" + "a" * 64, tokens, method)


def decide(current=600, candidate=150, **kwargs):
    values = dict(policy=PressurePolicy(), next_segment_growth=100,
                  compaction_exhausted=True, no_progress_rebases=0,
                  candidate=None if candidate is None else count(candidate))
    values.update(kwargs)
    return decide_pressure(RouteBudget("route:1", 1000, 1400, 200, 0), count(current), **values)


@pytest.mark.parametrize("limits,expected", [
    ((1000, 1400, 200, 50), 950),
    ((1000, 1100, 200, 50), 850),
    ((1000, None, 900, 50), 950),
    ((None, 1000, 200, 50), 750),
])
def test_caps_only_subtract_output_from_combined_limit(limits, expected):
    assert RouteBudget("r", *limits).usable_input == expected


@pytest.mark.parametrize("limits", [
    (None, None, 0, 0), (0, None, 0, 0), (1000, 1000, 1000, 0),
    (1000, None, 0, 1000), (True, None, 0, 0), (1000, None, -1, 0),
    (1000, None, 0, -1), (float("inf"), None, 0, 0),
])
def test_invalid_limits_refuse_instead_of_falling_back(limits):
    with pytest.raises(BudgetError):
        RouteBudget("r", *limits)


@pytest.mark.parametrize("value", [True, -1, 0.1, float("nan"), 2**63])
def test_counts_require_bounded_integers(value):
    with pytest.raises(BudgetError):
        count(value)


def test_healthy_context_does_not_rotate():
    assert decide(100).disposition is Disposition.CONTINUE


def test_compaction_precedes_rebase():
    assert decide(compaction_exhausted=False).disposition is Disposition.COMPACT


def test_feasible_successor_is_not_an_execution_permit():
    result = decide()
    assert result.disposition is Disposition.REBASE_CANDIDATE
    assert result.current_runway == 400
    assert result.candidate_runway == 850
    assert result.execution_authorized is False


@pytest.mark.parametrize("current,candidate,reason", [
    (600, None, "SUCCESSOR_NOT_MATERIALIZED"),
    (600, 600, "NO_PRESSURE_BENEFIT"),
    (600, 700, "NO_PRESSURE_BENEFIT"),
    (600, 450, "SUCCESSOR_HAS_INSUFFICIENT_RUNWAY"),
    (600, 901, "SUCCESSOR_CANNOT_FIT_SEGMENT"),
    (400, 100, "SEGMENT_REQUIRES_REPLAN"),
])
def test_unsafe_or_pointless_rebases_are_blocked(current, candidate, reason):
    result = decide(current, candidate)
    assert result.disposition is Disposition.BLOCKED
    assert result.reason == reason


def test_growth_can_trigger_before_fifty_percent():
    assert decide(450, 150).disposition is Disposition.REBASE_CANDIDATE


def test_bootstrap_target_is_soft_not_a_truncation_rule():
    budget = RouteBudget("r", 1000000, None, 0, 0)
    assert PressurePolicy().bootstrap_target(budget) == 16000
    assert decide(600, 300).disposition is Disposition.REBASE_CANDIDATE


def test_recovery_counter_is_external_and_never_reset():
    assert decide(no_progress_rebases=2).reason == "NO_PROGRESS_REBASE_LIMIT"


def test_provider_repair_can_have_equal_size_but_needs_both_refs():
    assert decide(600, 600, cause=Cause.PROVIDER_REPAIR).reason == "PROVIDER_REPAIR_EVIDENCE_MISSING"
    result = decide(600, 600, cause=Cause.PROVIDER_REPAIR,
                    provider_fault_ref="fault:1", qualified_reset_ref="reset:1")
    assert result.disposition is Disposition.REBASE_CANDIDATE
    assert result.execution_authorized is False


@pytest.mark.parametrize("which", ["current", "candidate"])
def test_estimates_are_not_labeled_qualified(which):
    a, b = count(600), count(150)
    if which == "current":
        a = replace(a, method=CountMethod.HEURISTIC)
    else:
        b = replace(b, method=CountMethod.HEURISTIC)
    result = decide_pressure(RouteBudget("route:1", 1000, None, 0, 0), a,
        policy=PressurePolicy(), next_segment_growth=100, compaction_exhausted=True,
        no_progress_rebases=0, candidate=b)
    assert result.reason == "UNQUALIFIED_COUNT"


def test_foreign_route_candidate_is_rejected():
    with pytest.raises(BudgetError, match="ROUTE_COUNT_MISMATCH"):
        decide_pressure(RouteBudget("route:1", 1000, None, 0, 0), count(600),
            policy=PressurePolicy(), next_segment_growth=100, compaction_exhausted=True,
            no_progress_rebases=0, candidate=count(150, "other"))


@pytest.mark.parametrize("patch", [
    {"compact_trigger_bps": 10000}, {"rebase_floor_bps": 5000},
    {"bootstrap_target_bps": 2500}, {"max_no_progress_rebases": 0},
    {"bootstrap_target_tokens": True},
])
def test_policy_order_and_types(patch):
    with pytest.raises(BudgetError):
        PressurePolicy(**patch)


def test_stateless_payload_upper_bound_accounts_system_messages_and_tools():
    count = stateless_payload_token_upper_bound(
        route_ref="openrouter:chat_completions:test",
        system_prompt="trusted system " * 50,
        messages=[
            {"role": "user", "content": "do the work " * 100},
            {"role": "assistant", "content": "state " * 80},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "bounded test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ],
    )
    assert count.method is CountMethod.QUALIFIED_UPPER_BOUND
    assert count.tokens > len(("trusted system " * 50).encode())
    assert count.payload_digest.startswith("sha256:")


def test_stateless_payload_upper_bound_rejects_nonserializable_payload():
    with pytest.raises(BudgetError, match="INVALID_STATELESS_PAYLOAD"):
        stateless_payload_token_upper_bound(
            route_ref="test:chat_completions:model",
            system_prompt="system",
            messages=[{"role": "user", "content": object()}],
            tools=None,
        )
