"""Route-specific pressure and successor feasibility, without effect admission.

All counts cover the complete rendered input of the same route. A caller must
supply a qualified upper bound; this module cannot turn a text heuristic into
one. Policy percentages are configurable engineering candidates, not measured
model-quality limits. No state, quotas, credentials or provider calls live here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BudgetError(ValueError):
    """Stable, payload-free reason for rejecting invalid budget observations."""


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise BudgetError(name)


def _identity(value: str, name: str) -> None:
    if type(value) is not str or not value or len(value) > 256 or any(ord(c) < 33 for c in value):
        raise BudgetError(name)


class CountMethod(str, Enum):
    EXACT_SERIALIZED = "exact_serialized"
    QUALIFIED_UPPER_BOUND = "qualified_upper_bound"
    HEURISTIC = "heuristic"


class Cause(str, Enum):
    PRESSURE = "pressure"
    PROVIDER_REPAIR = "provider_repair"


class Disposition(str, Enum):
    CONTINUE = "continue"
    COMPACT = "compact"
    REBASE_CANDIDATE = "rebase_candidate"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RouteBudget:
    route_ref: str
    input_limit: int | None
    total_limit: int | None
    output_reserve: int
    uncertainty_reserve: int

    def __post_init__(self) -> None:
        _identity(self.route_ref, "INVALID_ROUTE")
        if self.input_limit is None and self.total_limit is None:
            raise BudgetError("MISSING_ROUTE_LIMIT")
        for name in ("input_limit", "total_limit"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, "INVALID_ROUTE_LIMIT", 1)
        _integer(self.output_reserve, "INVALID_OUTPUT_RESERVE")
        _integer(self.uncertainty_reserve, "INVALID_UNCERTAINTY_RESERVE")
        if self.usable_input <= 0:
            raise BudgetError("NO_USABLE_INPUT")

    @property
    def usable_input(self) -> int:
        caps = []
        if self.input_limit is not None:
            caps.append(self.input_limit)
        if self.total_limit is not None:
            caps.append(self.total_limit - self.output_reserve)
        return min(caps) - self.uncertainty_reserve


@dataclass(frozen=True)
class InputCount:
    route_ref: str
    payload_digest: str
    tokens: int
    method: CountMethod

    def __post_init__(self) -> None:
        _identity(self.route_ref, "INVALID_ROUTE")
        if (type(self.payload_digest) is not str or len(self.payload_digest) != 71
                or not self.payload_digest.startswith("sha256:")
                or any(c not in "0123456789abcdef" for c in self.payload_digest[7:])):
            raise BudgetError("INVALID_PAYLOAD_DIGEST")
        _integer(self.tokens, "INVALID_INPUT_COUNT")
        if type(self.method) is not CountMethod:
            raise BudgetError("INVALID_COUNT_METHOD")


@dataclass(frozen=True)
class PressurePolicy:
    compact_trigger_bps: int = 5000
    rebase_floor_bps: int = 4000
    compaction_target_bps: int = 2000
    bootstrap_target_bps: int = 1500
    bootstrap_target_tokens: int = 16000
    max_no_progress_rebases: int = 2

    def __post_init__(self) -> None:
        for name in ("compact_trigger_bps", "rebase_floor_bps", "compaction_target_bps", "bootstrap_target_bps"):
            _integer(getattr(self, name), "INVALID_POLICY_RATIO", 1)
        if not (self.bootstrap_target_bps <= self.compaction_target_bps
                < self.rebase_floor_bps < self.compact_trigger_bps < 10000):
            raise BudgetError("INVALID_POLICY_ORDER")
        _integer(self.bootstrap_target_tokens, "INVALID_BOOTSTRAP_TARGET", 1)
        _integer(self.max_no_progress_rebases, "INVALID_REBASE_LIMIT", 1)

    def bootstrap_target(self, budget: RouteBudget) -> int:
        return min(self.bootstrap_target_tokens, budget.usable_input * self.bootstrap_target_bps // 10000)


@dataclass(frozen=True)
class PressureDecision:
    disposition: Disposition
    reason: str
    usable_input: int
    current_runway: int
    candidate_runway: int | None
    # This is a pure recommendation, never a session/provider/tool permit.
    execution_authorized: bool = field(default=False, init=False)


def decide_pressure(
    budget: RouteBudget,
    current: InputCount,
    *,
    policy: PressurePolicy,
    next_segment_growth: int,
    compaction_exhausted: bool,
    no_progress_rebases: int,
    candidate: InputCount | None = None,
    cause: Cause = Cause.PRESSURE,
    provider_fault_ref: str | None = None,
    qualified_reset_ref: str | None = None,
) -> PressureDecision:
    """Evaluate a bounded segment, requiring benefit before recommending rebase.

    Candidate identity, mandatory coverage, policy, effects and control state
    are checked by their owners separately. The returned decision grants none
    of them. A heuristic observation cannot qualify hard admission or switching.
    """
    if type(budget) is not RouteBudget or type(policy) is not PressurePolicy:
        raise BudgetError("INVALID_BUDGET_INPUT")
    if type(compaction_exhausted) is not bool or type(cause) is not Cause:
        raise BudgetError("INVALID_DECISION_INPUT")
    _integer(next_segment_growth, "INVALID_GROWTH")
    _integer(no_progress_rebases, "INVALID_REBASE_COUNT")
    if type(current) is not InputCount or current.route_ref != budget.route_ref:
        raise BudgetError("ROUTE_COUNT_MISMATCH")
    if candidate is not None and (type(candidate) is not InputCount or candidate.route_ref != budget.route_ref):
        raise BudgetError("ROUTE_COUNT_MISMATCH")
    usable = budget.usable_input
    runway = usable - current.tokens
    candidate_runway = None if candidate is None else usable - candidate.tokens

    def result(disposition: Disposition, reason: str) -> PressureDecision:
        return PressureDecision(disposition, reason, usable, runway, candidate_runway)

    if current.method is CountMethod.HEURISTIC or (candidate and candidate.method is CountMethod.HEURISTIC):
        return result(Disposition.BLOCKED, "UNQUALIFIED_COUNT")
    trigger = usable * policy.compact_trigger_bps // 10000
    floor = usable * policy.rebase_floor_bps // 10000
    if cause is Cause.PROVIDER_REPAIR:
        if provider_fault_ref is None or qualified_reset_ref is None:
            return result(Disposition.BLOCKED, "PROVIDER_REPAIR_EVIDENCE_MISSING")
        _identity(provider_fault_ref, "INVALID_PROVIDER_FAULT_REF")
        _identity(qualified_reset_ref, "INVALID_RESET_REF")
        needs_rebase = True
    else:
        if provider_fault_ref is not None or qualified_reset_ref is not None:
            raise BudgetError("UNEXPECTED_REPAIR_EVIDENCE")
        pressure = current.tokens >= trigger or current.tokens + next_segment_growth >= trigger
        if not pressure:
            return result(Disposition.CONTINUE, "WITHIN_PRESSURE_ENVELOPE")
        if not compaction_exhausted:
            return result(Disposition.COMPACT, "COMPACTION_AVAILABLE")
        needs_rebase = current.tokens >= trigger or (current.tokens > floor and current.tokens + next_segment_growth >= trigger)
    if not needs_rebase:
        return result(Disposition.BLOCKED, "SEGMENT_REQUIRES_REPLAN")
    if no_progress_rebases >= policy.max_no_progress_rebases:
        return result(Disposition.BLOCKED, "NO_PROGRESS_REBASE_LIMIT")
    if candidate is None:
        return result(Disposition.BLOCKED, "SUCCESSOR_NOT_MATERIALIZED")
    if candidate.tokens + next_segment_growth > usable:
        return result(Disposition.BLOCKED, "SUCCESSOR_CANNOT_FIT_SEGMENT")
    if cause is Cause.PRESSURE:
        if candidate.tokens >= current.tokens:
            return result(Disposition.BLOCKED, "NO_PRESSURE_BENEFIT")
        if candidate.tokens + next_segment_growth >= trigger:
            return result(Disposition.BLOCKED, "SUCCESSOR_HAS_INSUFFICIENT_RUNWAY")
    return result(Disposition.REBASE_CANDIDATE, "CANDIDATE_REQUIRES_OWNER_VALIDATION")
