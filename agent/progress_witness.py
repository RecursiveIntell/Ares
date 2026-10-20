"""Qualified operation-progress witness for the delegation *inactivity* lifetime mode.

Why this is not the shared activity clock
-----------------------------------------
``AIAgent._touch_activity`` has ~30 call sites and stamps every one as
``ActivityProvenance.UNKNOWN``. Roughly two thirds of them are not evidence of
work: a ticker thread refreshing "waiting for non-streaming API response", three
wait-loop heartbeats, "stale call killed" stamps, retry-backoff stamps and
request-start stamps. A hung or failing child refreshes that clock, so an
inactivity deadline read from it can never fire. That is the hazard the plan
calls "heartbeat-only immortality" (B11).

This module is a separate, closed-vocabulary ledger of *observed work*:

* a validated, non-empty model delta;
* a completed (validated) model response;
* a tool's cumulative output strictly growing;
* a tool call completing;
* a named initialization milestone the owner declared up front.

It deliberately does NOT count: request start, tool start, heartbeats, polls of
an unchanged result, retries or backoff, kill/abandon stamps, UI traffic,
parent traffic, or events carrying a stale generation.

Authority
---------
Facts and a pure verdict only. This module cannot stop, kill, retry, admit or
re-budget anything; the delegation owner reads ``evaluate()`` and decides.
Limits have **no defaults**: a number chosen here would be an invented
watchdog constant. Absent limits are a typed refusal.

Each in-flight non-streaming request and each in-flight tool is governed by its
own explicit finite limit, and the stall budget is suspended only while one of
those is open. A streaming request is governed by the stall budget because
chunks are expected to flow. The optional total ceiling is measured from the
single admission instant and has no reset operation.
"""
from __future__ import annotations

import enum
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable


class ProgressKind(str, enum.Enum):
    MODEL_DELTA = "model.delta"
    MODEL_COMPLETE = "model.complete"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETE = "tool.complete"
    INIT_MILESTONE = "init.milestone"


# Only these calls can advance progress. The static guard in
# tests/agent/test_progress_witness.py pins where they may be called from.
PROGRESS_EMITTERS = ("record_model_delta", "complete_request", "record_tool_output",
                     "complete_tool", "record_init_milestone")


class InactivityLimitsError(ValueError):
    """Invalid or absent limits must never widen execution permission."""


def _limit(name: str, value, *, optional: bool = False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InactivityLimitsError(
            f"DELEGATION_INACTIVITY_LIMITS_INVALID: {name} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > threading.TIMEOUT_MAX:
        raise InactivityLimitsError(
            f"DELEGATION_INACTIVITY_LIMITS_INVALID: {name} must be finite, positive and representable")
    return number


@dataclass(frozen=True)
class InactivityLimits:
    stall_seconds: float
    request_seconds: float
    tool_seconds: float
    total_ceiling_seconds: float | None = None

    def __post_init__(self):
        for name in ("stall_seconds", "request_seconds", "tool_seconds"):
            object.__setattr__(self, name, _limit(name, getattr(self, name)))
        object.__setattr__(self, "total_ceiling_seconds",
                           _limit("total_ceiling_seconds", self.total_ceiling_seconds, optional=True))


@dataclass(frozen=True)
class ProgressSnapshot:
    generation: str
    admitted_at: float
    last_progress_at: float | None
    last_kind: ProgressKind | None
    accepted: dict
    rejected: dict
    open_requests: int
    open_tools: int
    closed: bool


@dataclass(frozen=True)
class InactivityVerdict:
    state: str  # ok | stalled | request_limit | tool_limit | ceiling | closed
    detail: dict


class ProgressWitness:
    def __init__(self, *, generation: str, limits: InactivityLimits,
                 allowed_milestones: Iterable[str] = (),
                 clock: Callable[[], float] = time.monotonic):
        if type(generation) is not str or not generation:
            raise InactivityLimitsError("DELEGATION_INACTIVITY_LIMITS_INVALID: generation required")
        if type(limits) is not InactivityLimits:
            raise InactivityLimitsError("DELEGATION_INACTIVITY_LIMITS_INVALID: limits required")
        self._generation = generation
        self._limits = limits
        self._milestones = frozenset(allowed_milestones)
        self._clock = clock
        self._lock = threading.Lock()
        self._admitted_at = clock()
        self._last_at: float | None = None
        self._last_kind: ProgressKind | None = None
        self._accepted: dict = {}
        self._rejected: dict = {}
        self._requests: dict = {}   # op -> (started_at, streaming)
        self._completed_requests: set = set()
        self._tools: dict = {}      # op -> started_at
        self._completed_tools: set = set()
        self._tool_bytes: dict = {}
        self._seen_milestones: set = set()
        self._closed = False

    @property
    def admitted_at(self) -> float:
        return self._admitted_at

    # -- internal -----------------------------------------------------
    def _reject(self, reason: str) -> bool:
        self._rejected[reason] = self._rejected.get(reason, 0) + 1
        return False

    def _gate(self, generation) -> str | None:
        if self._closed:
            return "closed"
        if generation != self._generation:
            return "stale_generation"
        return None

    def _accept(self, kind: ProgressKind) -> bool:
        self._last_at = self._clock()
        self._last_kind = kind
        self._accepted[kind.value] = self._accepted.get(kind.value, 0) + 1
        return True

    # -- non-progress bookkeeping (opening an operation is NOT progress) ----
    def open_request(self, op: str, *, generation: str, streaming: bool) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad or op in self._requests or op in self._completed_requests:
                return self._reject(bad or "duplicate_request")
            self._requests[op] = (self._clock(), bool(streaming))
            return True

    def abandon_request(self, op: str, *, generation: str) -> bool:
        """A failed attempt. Never progress; a retry does not reset the stall clock."""
        with self._lock:
            bad = self._gate(generation)
            if bad or op not in self._requests:
                return self._reject(bad or "unknown_request")
            del self._requests[op]
            return True

    def open_tool(self, op: str, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad or op in self._tools or op in self._completed_tools:
                return self._reject(bad or "duplicate_tool")
            self._tools[op] = self._clock()
            self._tool_bytes[op] = 0
            return True

    def abandon_tool(self, op: str, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad or op not in self._tools:
                return self._reject(bad or "unknown_tool")
            del self._tools[op]
            return True

    # -- the only progress emitters --------------------------------------
    def record_model_delta(self, op: str, nbytes: int, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad:
                return self._reject(bad)
            if op not in self._requests:
                return self._reject("unknown_request")
            if type(nbytes) is not int or nbytes <= 0:
                return self._reject("empty_delta")
            return self._accept(ProgressKind.MODEL_DELTA)

    def complete_request(self, op: str, *, generation: str) -> bool:
        """Caller attests a validated response (not an empty-content retry)."""
        with self._lock:
            bad = self._gate(generation)
            if bad:
                return self._reject(bad)
            if op not in self._requests:
                return self._reject("unknown_request")
            del self._requests[op]
            self._completed_requests.add(op)
            return self._accept(ProgressKind.MODEL_COMPLETE)

    def record_tool_output(self, op: str, cumulative_bytes: int, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad:
                return self._reject(bad)
            if op not in self._tools:
                return self._reject("unknown_tool")
            if type(cumulative_bytes) is not int or cumulative_bytes <= self._tool_bytes[op]:
                return self._reject("unchanged_tool_output")
            self._tool_bytes[op] = cumulative_bytes
            return self._accept(ProgressKind.TOOL_OUTPUT)

    def complete_tool(self, op: str, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad:
                return self._reject(bad)
            if op not in self._tools:
                return self._reject("unknown_tool")
            del self._tools[op]
            self._tool_bytes.pop(op, None)
            self._completed_tools.add(op)
            return self._accept(ProgressKind.TOOL_COMPLETE)

    def record_init_milestone(self, name: str, *, generation: str) -> bool:
        with self._lock:
            bad = self._gate(generation)
            if bad:
                return self._reject(bad)
            if name not in self._milestones:
                return self._reject("undeclared_milestone")
            if name in self._seen_milestones:
                return self._reject("repeated_milestone")
            self._seen_milestones.add(name)
            return self._accept(ProgressKind.INIT_MILESTONE)

    # -- lifecycle / read side ----------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._closed = True

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return ProgressSnapshot(self._generation, self._admitted_at, self._last_at, self._last_kind,
                dict(self._accepted), dict(self._rejected), len(self._requests), len(self._tools), self._closed)

    def evaluate(self, now: float | None = None) -> InactivityVerdict:
        with self._lock:
            now = self._clock() if now is None else now
            lim = self._limits
            if self._closed:
                return InactivityVerdict("closed", {})
            total = now - self._admitted_at
            if lim.total_ceiling_seconds is not None and total >= lim.total_ceiling_seconds:
                return InactivityVerdict("ceiling", {"elapsed": total, "limit": lim.total_ceiling_seconds})
            for op, (started, _streaming) in self._requests.items():
                if now - started >= lim.request_seconds:
                    return InactivityVerdict("request_limit", {"op": op, "elapsed": now - started, "limit": lim.request_seconds})
            for op, started in self._tools.items():
                if now - started >= lim.tool_seconds:
                    return InactivityVerdict("tool_limit", {"op": op, "elapsed": now - started, "limit": lim.tool_seconds})
            # Stall is suspended only while an operation with its own finite limit is in flight.
            governed = bool(self._tools) or any(not s for _, s in self._requests.values())
            since = now - (self._last_at if self._last_at is not None else self._admitted_at)
            if not governed and since >= lim.stall_seconds:
                return InactivityVerdict("stalled", {"since_progress": since, "limit": lim.stall_seconds,
                                                     "last_kind": self._last_kind.value if self._last_kind else None})
            return InactivityVerdict("ok", {"since_progress": since, "governed": governed})
