"""Pure durable-record contracts for operator-authored WorkItems and Watches.

These classes validate and serialize; they never touch storage, providers,
models, or the clock (callers pass ``now_ms``).  Decisions encoded here:

* A WorkItem is provider-neutral.  Provider/account scope exists only on a
  Watch, and only as the opaque fields already defined by ``WatchSpecV1``.
* Only the current record plus a monotonic ``revision`` is kept; there is no
  transition history.
* Expiry is *derived* from ``expires_at_ms`` (``effective_state``); it is never
  stored, and resuming a Watch can never extend its original window.
* Authored fields (goal, criteria, watch scope) are immutable after creation.
  ``transition`` is the only mutator and changes state/revision/updated time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from .contracts import (
    AttentionContractError,
    EventFactV1,
    WatchSpecV1,
    WatchState,
    _canonical_facts,
    _text,
    _timestamp,
)

WORK_ITEM_SCHEMA = "ares.attention.work_item.v1"
WATCH_SCHEMA = "ares.attention.watch.v1"

MAX_ID_CHARS = 256
MAX_GOAL_CHARS = 4096
MAX_CRITERIA = 32
MAX_CRITERION_CHARS = 2048
MAX_FACTS = 64
MAX_FACT_CHARS = 1024
MAX_SCOPE_CHARS = 256

_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]+")


class WorkItemState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class EffectiveWatchState(StrEnum):
    """Read-time view of a Watch; ``EXPIRED`` is derived and never persisted."""

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def canonical_json(value: Mapping[str, Any]) -> str:
    """Deterministic JSON used both for storage and for byte-exact CAS."""
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise AttentionContractError("RECORD_NOT_CANONICAL") from exc


def identifier(value: object, field: str) -> str:
    """Opaque ID safe to embed in a key: no separators, wildcards, or control."""
    _text(value, field)
    assert isinstance(value, str)
    if len(value) > MAX_ID_CHARS or not _IDENTIFIER.fullmatch(value):
        raise AttentionContractError("INVALID_IDENTIFIER", field)
    return value


def _bounded(value: object, field: str, limit: int) -> str:
    text = _text(value, field)
    if len(text) > limit:
        raise AttentionContractError("TEXT_TOO_LONG", field)
    return text


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
        raise AttentionContractError("INVALID_REVISION")
    return value


def _exact_keys(raw: object, expected: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AttentionContractError("INVALID_RECORD")
    data: dict[str, Any] = {str(key): value for key, value in raw.items()}
    if set(data) != expected:
        raise AttentionContractError(
            "INVALID_RECORD_FIELDS",
            ",".join(sorted(set(data) ^ expected)),
        )
    return data


def _criteria(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > MAX_CRITERIA:
        raise AttentionContractError("INVALID_CRITERIA")
    cleaned = tuple(_bounded(item, "criteria", MAX_CRITERION_CHARS) for item in value)
    if len(set(cleaned)) != len(cleaned):
        raise AttentionContractError("DUPLICATE_CRITERION")
    return cleaned


def _check_times(created: int, updated: int) -> None:
    _timestamp(created, "created_at_ms")
    _timestamp(updated, "updated_at_ms")
    if updated < created:
        raise AttentionContractError("TIME_REGRESSION", "updated_at_ms")


def _check_transition_time(now_ms: object, updated_at_ms: int) -> int:
    _timestamp(now_ms, "now_ms")
    assert isinstance(now_ms, int)
    if now_ms < updated_at_ms:
        raise AttentionContractError("TIME_REGRESSION", "now_ms")
    return now_ms


@dataclass(frozen=True)
class WorkItemRecordV1:
    """Operator-authored expectation; criteria are immutable after creation."""

    work_item_id: str
    goal: str
    criteria: tuple[str, ...]
    state: WorkItemState
    revision: int
    created_at_ms: int
    updated_at_ms: int

    _FIELDS = frozenset({
        "schema",
        "work_item_id",
        "goal",
        "criteria",
        "state",
        "revision",
        "created_at_ms",
        "updated_at_ms",
    })

    def __post_init__(self) -> None:
        identifier(self.work_item_id, "work_item_id")
        _bounded(self.goal, "goal", MAX_GOAL_CHARS)
        object.__setattr__(self, "criteria", _criteria(self.criteria))
        if type(self.state) is not WorkItemState:
            raise AttentionContractError("INVALID_WORK_ITEM_STATE")
        _revision(self.revision)
        _check_times(self.created_at_ms, self.updated_at_ms)

    @classmethod
    def create(
        cls,
        *,
        work_item_id: str,
        goal: str,
        criteria: tuple[str, ...],
        now_ms: int,
    ) -> WorkItemRecordV1:
        _timestamp(now_ms, "now_ms")
        return cls(
            work_item_id=work_item_id,
            goal=goal,
            criteria=criteria,
            state=WorkItemState.ACTIVE,
            revision=1,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_ITEM_SCHEMA,
            "work_item_id": self.work_item_id,
            "goal": self.goal,
            "criteria": list(self.criteria),
            "state": self.state.value,
            "revision": self.revision,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def parse(cls, raw: object) -> WorkItemRecordV1:
        data = _exact_keys(raw, cls._FIELDS)
        if data["schema"] != WORK_ITEM_SCHEMA:
            raise AttentionContractError("UNKNOWN_SCHEMA")
        if type(data["criteria"]) is not list:
            raise AttentionContractError("INVALID_CRITERIA")
        try:
            state = WorkItemState(data["state"])
        except ValueError as exc:
            raise AttentionContractError("INVALID_WORK_ITEM_STATE") from exc
        return cls(
            work_item_id=data["work_item_id"],
            goal=data["goal"],
            criteria=tuple(data["criteria"]),
            state=state,
            revision=data["revision"],
            created_at_ms=data["created_at_ms"],
            updated_at_ms=data["updated_at_ms"],
        )

    def transition(self, new_state: WorkItemState, *, now_ms: int) -> WorkItemRecordV1:
        if type(new_state) is not WorkItemState:
            raise AttentionContractError("INVALID_WORK_ITEM_STATE")
        if new_state is self.state:
            raise AttentionContractError("ALREADY_IN_STATE", self.state.value)
        if self.state is WorkItemState.CANCELLED:
            raise AttentionContractError("ILLEGAL_TRANSITION", "cancelled is terminal")
        stamp = _check_transition_time(now_ms, self.updated_at_ms)
        return replace(
            self,
            state=new_state,
            revision=self.revision + 1,
            updated_at_ms=stamp,
        )


@dataclass(frozen=True)
class WatchRecordV1:
    """Durable form of ``WatchSpecV1`` plus lifecycle metadata."""

    watch_id: str
    expectation_id: str
    provider_id: str
    account_id: str
    object_type: str
    event_type: str
    required_facts: tuple[EventFactV1, ...]
    not_before_ms: int
    expires_at_ms: int
    state: WatchState
    revision: int
    created_at_ms: int
    updated_at_ms: int

    _FIELDS = frozenset({
        "schema",
        "watch_id",
        "expectation_id",
        "provider_id",
        "account_id",
        "object_type",
        "event_type",
        "required_facts",
        "not_before_ms",
        "expires_at_ms",
        "state",
        "revision",
        "created_at_ms",
        "updated_at_ms",
    })

    def __post_init__(self) -> None:
        identifier(self.watch_id, "watch_id")
        identifier(self.expectation_id, "expectation_id")
        # Re-run the P01 validator so a stored record can never be looser than
        # the in-memory contract the matcher consumes.
        spec = WatchSpecV1(
            watch_id=self.watch_id,
            expectation_id=self.expectation_id,
            provider_id=self.provider_id,
            account_id=self.account_id,
            object_type=self.object_type,
            event_type=self.event_type,
            required_facts=self.required_facts,
            not_before_ms=self.not_before_ms,
            expires_at_ms=self.expires_at_ms,
            state=self.state,
        )
        for name in ("provider_id", "account_id", "object_type", "event_type"):
            _bounded(getattr(spec, name), name, MAX_SCOPE_CHARS)
        if len(spec.required_facts) > MAX_FACTS:
            raise AttentionContractError("TOO_MANY_FACTS")
        for fact in spec.required_facts:
            _bounded(fact.key, "fact.key", MAX_FACT_CHARS)
            _bounded(fact.value, "fact.value", MAX_FACT_CHARS)
        object.__setattr__(self, "required_facts", spec.required_facts)
        _revision(self.revision)
        _check_times(self.created_at_ms, self.updated_at_ms)

    @classmethod
    def create(cls, spec: WatchSpecV1, *, now_ms: int) -> WatchRecordV1:
        if type(spec) is not WatchSpecV1:
            raise AttentionContractError("INVALID_WATCH_SPEC")
        _timestamp(now_ms, "now_ms")
        if spec.state is not WatchState.ACTIVE:
            raise AttentionContractError("WATCH_MUST_START_ACTIVE")
        if now_ms >= spec.expires_at_ms:
            raise AttentionContractError("WATCH_ALREADY_EXPIRED")
        return cls(
            watch_id=spec.watch_id,
            expectation_id=spec.expectation_id,
            provider_id=spec.provider_id,
            account_id=spec.account_id,
            object_type=spec.object_type,
            event_type=spec.event_type,
            required_facts=spec.required_facts,
            not_before_ms=spec.not_before_ms,
            expires_at_ms=spec.expires_at_ms,
            state=spec.state,
            revision=1,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )

    def to_spec(self) -> WatchSpecV1:
        return WatchSpecV1(
            watch_id=self.watch_id,
            expectation_id=self.expectation_id,
            provider_id=self.provider_id,
            account_id=self.account_id,
            object_type=self.object_type,
            event_type=self.event_type,
            required_facts=self.required_facts,
            not_before_ms=self.not_before_ms,
            expires_at_ms=self.expires_at_ms,
            state=self.state,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WATCH_SCHEMA,
            "watch_id": self.watch_id,
            "expectation_id": self.expectation_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "object_type": self.object_type,
            "event_type": self.event_type,
            "required_facts": [
                {"key": fact.key, "value": fact.value} for fact in self.required_facts
            ],
            "not_before_ms": self.not_before_ms,
            "expires_at_ms": self.expires_at_ms,
            "state": self.state.value,
            "revision": self.revision,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def parse(cls, raw: object) -> WatchRecordV1:
        data = _exact_keys(raw, cls._FIELDS)
        if data["schema"] != WATCH_SCHEMA:
            raise AttentionContractError("UNKNOWN_SCHEMA")
        facts_raw = data["required_facts"]
        if type(facts_raw) is not list:
            raise AttentionContractError("INVALID_FACTS", "watch.required_facts")
        facts: list[EventFactV1] = []
        for item in facts_raw:
            if not isinstance(item, dict) or set(item) != {"key", "value"}:
                raise AttentionContractError("INVALID_FACTS", "watch.required_facts")
            facts.append(EventFactV1(item["key"], item["value"]))
        _canonical_facts(tuple(facts), "watch.required_facts")
        try:
            state = WatchState(data["state"])
        except ValueError as exc:
            raise AttentionContractError("INVALID_WATCH_STATE") from exc
        return cls(
            watch_id=data["watch_id"],
            expectation_id=data["expectation_id"],
            provider_id=data["provider_id"],
            account_id=data["account_id"],
            object_type=data["object_type"],
            event_type=data["event_type"],
            required_facts=tuple(facts),
            not_before_ms=data["not_before_ms"],
            expires_at_ms=data["expires_at_ms"],
            state=state,
            revision=data["revision"],
            created_at_ms=data["created_at_ms"],
            updated_at_ms=data["updated_at_ms"],
        )

    def effective_state(self, now_ms: int) -> EffectiveWatchState:
        _timestamp(now_ms, "now_ms")
        if self.state is WatchState.CANCELLED:
            return EffectiveWatchState.CANCELLED
        if now_ms >= self.expires_at_ms:
            return EffectiveWatchState.EXPIRED
        if self.state is WatchState.PAUSED:
            return EffectiveWatchState.PAUSED
        return EffectiveWatchState.ACTIVE

    def transition(self, new_state: WatchState, *, now_ms: int) -> WatchRecordV1:
        if type(new_state) is not WatchState:
            raise AttentionContractError("INVALID_WATCH_STATE")
        if new_state is self.state:
            raise AttentionContractError("ALREADY_IN_STATE", self.state.value)
        if self.state is WatchState.CANCELLED:
            raise AttentionContractError("ILLEGAL_TRANSITION", "cancelled is terminal")
        stamp = _check_transition_time(now_ms, self.updated_at_ms)
        if new_state is WatchState.ACTIVE and stamp >= self.expires_at_ms:
            # Resume never extends or revives an elapsed window.
            raise AttentionContractError("WATCH_EXPIRED")
        return replace(
            self,
            state=new_state,
            revision=self.revision + 1,
            updated_at_ms=stamp,
        )


__all__ = [
    "EffectiveWatchState",
    "MAX_FACTS",
    "WatchRecordV1",
    "WorkItemRecordV1",
    "WorkItemState",
    "canonical_json",
    "identifier",
]
