"""The single owner of context-budget arithmetic.

Every context-budget decision — when to compact, how far to compact, how much
summary to keep, how much head and tail to preserve — derives from one effective
input window here. Nothing else in the host may compute these numbers inline.

Why a module and not a config key per knob: nine independent absolutes
(``token_budget``, ``threshold_tokens``, ``summary_max_chars``, a fixed head cap,
a message-count tail bound, …) could each contradict the others and none of them
scaled with the model. One arithmetic owner with one invariant replaces them.

The invariant is a theorem, not a runtime guess::

    floor/E = f_summary*f_target + f_head + f_tail + f_margin
            = 0.15*0.45 + 0.10 + 0.20 + 0.02
            = 0.3875   <   f_target = 0.45

Because each floor is capped at a ratio *below* the term it guards, no clamp can
push ``floor`` above ``target``. The runtime check below therefore exists to catch
a future ratio edit that breaks the identity, and to give a typed, diagnosable
failure — not to rescue ordinary operation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA = "ares-context-budget/v1"

# Absolute floors/guards. Each is a *cap on itself* relative to E, so a floor can
# never dominate a small window (the failure mode of the first draft, which put
# floor 12,112 against target 3,600 at an 8k window).
SUMMARY_FLOOR_TOKENS = 2_000
SUMMARY_FLOOR_RATIO = 0.01
HEAD_FLOOR_TOKENS = 8_000
HEAD_FLOOR_RATIO = 0.05
# Spend guard only: never the binding constraint at realistic windows.
SUMMARY_COST_GUARD_TOKENS = 256_000
MARGIN_FLOOR_TOKENS = 512
MIN_TARGET_TOKENS = 512

# Response reservation used when the host does not configure one. The host
# forwards ``agent.max_tokens`` (default None = "use the model default"), which
# is not a token count, so the governor previously computed an effective window
# equal to the FULL context window — reserving nothing for the response on the
# path that decides when to compact.
RESERVE_RATIO = 0.05
RESERVE_MAX_TOKENS = 32_768
RESERVE_MIN_TOKENS = 1_024


def resolve_output_reserve(configured: int | None, window: int) -> int:
    """Response reservation for a route, never zero when a window is known.

    An explicit host value always wins. Otherwise reserve a bounded fraction of
    the window: large windows get a fixed 32k (a typical max-output size), small
    windows get 5% with a 1k floor so a short response is still possible.
    """
    if configured is not None:
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            # An explicit reservation always stands; only ensure it cannot
            # consume the entire window (leave at least one input token).
            return min(value, max(1, window - 1)) if window > 0 else value
    if window <= 0:
        return 0
    derived = min(
        RESERVE_MAX_TOKENS,
        max(RESERVE_MIN_TOKENS, int(window * RESERVE_RATIO)),
    )
    # Never reserve more than a quarter of the window: on a tiny route the 1,024
    # floor would otherwise exceed the whole window and leave no input budget,
    # turning a degraded-but-working route into a hard error.
    return min(derived, max(0, window // 4))


class BudgetFloorInfeasible(ValueError):
    """The protected floor cannot fit inside the target.

    Carries the per-term breakdown so a caller can name the offending term
    instead of reporting a generic "cannot meet target".
    """

    def __init__(self, target: int, floor: int, terms: dict[str, int]) -> None:
        self.target = target
        self.floor = floor
        self.terms = dict(terms)
        detail = ", ".join(f"{k}={v}" for k, v in sorted(terms.items()))
        super().__init__(
            f"context budget floor {floor} exceeds target {target} ({detail})"
        )


@dataclass(frozen=True)
class BudgetRatios:
    """Proportional policy. All terms are fractions of the effective window."""

    trigger: float = 0.80
    target: float = 0.45
    summary: float = 0.15
    head: float = 0.10
    tail: float = 0.20
    margin: float = 0.02

    def validate(self) -> None:
        for name in ("trigger", "target", "summary", "head", "tail", "margin"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"ratio {name}={value} must be in (0, 1]")
        if self.trigger <= self.target:
            raise ValueError("trigger ratio must exceed target ratio")
        if self.target <= self.summary:
            raise ValueError("target ratio must exceed summary ratio")
        # The feasibility identity: floors plus margin must fit inside target.
        floor_ratio = self.summary * self.target + self.head + self.tail + self.margin
        if floor_ratio >= self.target:
            raise ValueError(
                f"ratios are infeasible: floor ratio {floor_ratio:.4f} >= "
                f"target ratio {self.target:.4f}"
            )


@dataclass(frozen=True)
class ContextBudget:
    """Resolved budget for one model route."""

    window: int
    reserve: int
    effective: int
    trigger: int
    target: int
    summary: int
    head_cap: int
    tail_cap: int
    margin: int
    floor: int
    feasible: bool
    reclaimed: int
    retention: float
    ratios: BudgetRatios = field(default_factory=BudgetRatios)
    schema: str = SCHEMA

    @classmethod
    def from_window(
        cls,
        window: int,
        reserve: int = 0,
        ratios: BudgetRatios | None = None,
    ) -> "ContextBudget":
        if window <= 0:
            raise ValueError(f"window must be positive, got {window}")
        if reserve < 0 or reserve >= window:
            raise ValueError(f"reserve {reserve} leaves no input budget in {window}")
        policy = ratios or BudgetRatios()
        policy.validate()

        e = window - reserve
        trigger = int(policy.trigger * e)
        target = max(MIN_TARGET_TOKENS, int(policy.target * e))

        summary = min(
            SUMMARY_COST_GUARD_TOKENS,
            max(int(policy.summary * target),
                min(SUMMARY_FLOOR_TOKENS, max(256, int(SUMMARY_FLOOR_RATIO * e)))),
        )
        head = max(int(policy.head * e),
                   min(HEAD_FLOOR_TOKENS, max(256, int(HEAD_FLOOR_RATIO * e))))
        tail = int(policy.tail * e)
        margin = max(MARGIN_FLOOR_TOKENS, int(policy.margin * e))

        floor = summary + head + tail + margin
        feasible = floor <= target
        if not feasible:
            head, tail = _scale_down(target, summary, margin, head, tail)
            floor = summary + head + tail + margin
            feasible = floor <= target
            if not feasible:
                raise BudgetFloorInfeasible(
                    target=target,
                    floor=floor,
                    terms={"summary": summary, "head": head, "tail": tail,
                           "margin": margin},
                )

        reclaimed = max(0, trigger - target)
        retention = (summary / reclaimed) if reclaimed else 0.0
        return cls(
            window=window, reserve=reserve, effective=e, trigger=trigger,
            target=target, summary=summary, head_cap=head, tail_cap=tail,
            margin=margin, floor=floor, feasible=feasible, reclaimed=reclaimed,
            retention=retention, ratios=policy,
        )

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "window": self.window,
            "reserve": self.reserve,
            "effective": self.effective,
            "trigger": self.trigger,
            "target": self.target,
            "summary": self.summary,
            "head_cap": self.head_cap,
            "tail_cap": self.tail_cap,
            "margin": self.margin,
            "floor": self.floor,
            "feasible": self.feasible,
            "reclaimed": self.reclaimed,
            "retention": round(self.retention, 6),
            "ratios": {
                "trigger": self.ratios.trigger, "target": self.ratios.target,
                "summary": self.ratios.summary, "head": self.ratios.head,
                "tail": self.ratios.tail, "margin": self.ratios.margin,
            },
        }


def _scale_down(target: int, summary: int, margin: int,
                head: int, tail: int) -> tuple[int, int]:
    """Shrink the optional protected terms to fit, preserving their proportion."""
    available = target - summary - margin
    if available <= 0 or head + tail <= 0:
        return 0, 0
    factor = available / (head + tail)
    return int(head * factor), int(tail * factor)
