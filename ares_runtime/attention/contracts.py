"""Closed in-memory contracts for synthetic Ares attention-event replay.

These records do not read provider data, persist work, call models, or grant
external effects. Provider adapters and durable owners are separate phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AttentionContractError(ValueError):
    """Typed refusal from the offline attention contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (f": {detail}" if detail else ""))


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise AttentionContractError("INVALID_TEXT", field)
    return value


def _timestamp(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise AttentionContractError("INVALID_TIMESTAMP", field)
    return value


@dataclass(frozen=True, order=True)
class SourceIdentityV1:
    """Exact source/object/version identity; cursor state is separate."""

    provider_id: str
    account_id: str
    object_type: str
    remote_id: str
    revision: str

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "account_id",
            "object_type",
            "remote_id",
            "revision",
        ):
            _text(getattr(self, name), name)


@dataclass(frozen=True)
class EventFactV1:
    """Opaque key/value metadata compared literally by the matcher."""

    key: str
    value: str

    def __post_init__(self) -> None:
        _text(self.key, "fact.key")
        _text(self.value, "fact.value")


def _canonical_facts(
    value: tuple[EventFactV1, ...], field: str
) -> tuple[EventFactV1, ...]:
    if type(value) is not tuple or any(type(item) is not EventFactV1 for item in value):
        raise AttentionContractError("INVALID_FACTS", field)
    keys = [item.key for item in value]
    if len(set(keys)) != len(keys):
        raise AttentionContractError("DUPLICATE_FACT_KEY", field)
    return tuple(sorted(value, key=lambda item: (item.key, item.value)))


@dataclass(frozen=True)
class ProviderEventV1:
    """Synthetic metadata-only event; deliberately has no body or attachment."""

    identity: SourceIdentityV1
    event_type: str
    occurred_at_ms: int
    facts: tuple[EventFactV1, ...]

    def __post_init__(self) -> None:
        if type(self.identity) is not SourceIdentityV1:
            raise AttentionContractError("INVALID_EVENT_IDENTITY")
        _text(self.event_type, "event_type")
        _timestamp(self.occurred_at_ms, "occurred_at_ms")
        object.__setattr__(self, "facts", _canonical_facts(self.facts, "event.facts"))


class WatchState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WatchSpecV1:
    """In-memory explicit expectation scope; not a durable WorkItem owner."""

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

    def __post_init__(self) -> None:
        for name in (
            "watch_id",
            "expectation_id",
            "provider_id",
            "account_id",
            "object_type",
            "event_type",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(
            self,
            "required_facts",
            _canonical_facts(self.required_facts, "watch.required_facts"),
        )
        _timestamp(self.not_before_ms, "not_before_ms")
        _timestamp(self.expires_at_ms, "expires_at_ms")
        if self.expires_at_ms <= self.not_before_ms:
            raise AttentionContractError("INVALID_WATCH_WINDOW")
        if type(self.state) is not WatchState:
            raise AttentionContractError("INVALID_WATCH_STATE")


class EventCoverageState(StrEnum):
    """Synthetic source-coverage witness; this package does not own cursors."""

    COMPLETE = "complete"
    GAP = "gap"
    CURSOR_EXPIRED = "cursor_expired"
    UNAVAILABLE = "unavailable"


class MatchDisposition(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    NEEDS_REVIEW = "needs_review"
    IDENTITY_CONFLICT = "identity_conflict"


@dataclass(frozen=True)
class EventDecisionV1:
    identity: SourceIdentityV1
    disposition: MatchDisposition
    matched_watch_ids: tuple[str, ...]
    delivery_count: int
    reason_code: str

    @property
    def requires_review(self) -> bool:
        return self.disposition in {
            MatchDisposition.AMBIGUOUS,
            MatchDisposition.NEEDS_REVIEW,
            MatchDisposition.IDENTITY_CONFLICT,
        }


@dataclass(frozen=True)
class EventReplayResultV1:
    coverage: EventCoverageState
    reconciliation_required: bool
    decisions: tuple[EventDecisionV1, ...]


__all__ = [
    "AttentionContractError",
    "EventCoverageState",
    "EventDecisionV1",
    "EventFactV1",
    "EventReplayResultV1",
    "MatchDisposition",
    "ProviderEventV1",
    "SourceIdentityV1",
    "WatchSpecV1",
    "WatchState",
]
