"""Closed, versioned execution-record contract for one managed model attempt.

This module is deliberately stdlib-only.  It owns the record shape, the
allowed state transitions, and the canonical digest of the record; it does
not read or write a database, call a provider, or authorize an effect.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

SCHEMA_VERSION = "ares.execution-record/v1"
CANONICALIZATION = "ares-json-sorted-v1"
DIGEST_ALGORITHM = "sha256"

_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}$")
_DIGEST_RE = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@#+-]{0,1023}$")

FIELDS: tuple[str, ...] = (
    "schema_version",
    "execution_id",
    "parent_execution_ref",
    "session_ref",
    "session_generation",
    "turn_ref",
    "call_ref",
    "source_revision",
    "runtime_identity",
    "admission_ref",
    "materialization_ref",
    "logical_request_digest",
    "logical_projection_schema",
    "wire_request_digest",
    "wire_bytes",
    "transport_version",
    "route_ref",
    "dispatch_intent_refs",
    "state",
    "terminal_outcome",
    "response_digest",
    "http_status",
    "provider_error_code",
    "receipt_refs",
    "governor_qualified",
    "governor_mode",
    "sequence",
    "recorded_at",
    "record_digest",
)
_FIELD_SET = frozenset(FIELDS)


class ExecutionState(StrEnum):
    ADMITTED = "admitted"
    MATERIALIZED = "materialized"
    SEND_STARTED = "send_started"
    SETTLED = "settled"
    DENIED = "denied"
    ABANDONED = "abandoned"
    INDETERMINATE = "indeterminate"


TERMINAL_STATES = frozenset({
    ExecutionState.SETTLED.value,
    ExecutionState.DENIED.value,
    ExecutionState.ABANDONED.value,
    ExecutionState.INDETERMINATE.value,
})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ExecutionState.ADMITTED.value: frozenset({
        ExecutionState.MATERIALIZED.value,
        ExecutionState.DENIED.value,
        ExecutionState.ABANDONED.value,
    }),
    ExecutionState.MATERIALIZED.value: frozenset({
        ExecutionState.SEND_STARTED.value,
        ExecutionState.DENIED.value,
        ExecutionState.ABANDONED.value,
    }),
    ExecutionState.SEND_STARTED.value: frozenset({
        ExecutionState.SETTLED.value,
        ExecutionState.INDETERMINATE.value,
    }),
}

GOVERNOR_MODES = frozenset({"observe", "enforce", "unavailable"})
TERMINAL_OUTCOMES = frozenset({
    "ok",
    "provider_error",
    "interrupted",
    "discarded",
    "denied",
    "never_sent",
    "unknown",
})

_IMMUTABLE_AFTER_CREATE = frozenset({
    "schema_version",
    "execution_id",
    "parent_execution_ref",
    "session_ref",
    "session_generation",
    "turn_ref",
    "admission_ref",
    "record_digest",
})


class ExecutionRecordError(ValueError):
    """Typed refusal from the pure record contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (": " + detail if detail else ""))


def _text(value: object, field: str, *, ref: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ExecutionRecordError("INVALID_TEXT", field)
    if ref and not _REF_RE.fullmatch(value):
        raise ExecutionRecordError("INVALID_REF", field)
    return value


def _optional_text(value: object, field: str, *, ref: bool = False) -> str | None:
    if value is None:
        return None
    return _text(value, field, ref=ref)


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ExecutionRecordError("INVALID_DIGEST", field)
    return value


def _optional_digest(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field)


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ExecutionRecordError("INVALID_INTEGER", field)
    return value


def _optional_integer(value: object, field: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _integer(value, field, minimum=minimum)


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise ExecutionRecordError("INVALID_LIST", field)
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = _text(item, field, ref=True)
        if text in seen:
            raise ExecutionRecordError("DUPLICATE_LIST_ITEM", field)
        seen.add(text)
        result.append(text)
    return result


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """Canonical bytes for this contract only (sorted keys, UTF-8, no newline)."""

    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionRecordError("NOT_CANONICAL", str(exc)) from exc


def digest_record(value: Mapping[str, Any]) -> str:
    """Digest of all fields except ``record_digest``."""

    body = {key: item for key, item in dict(value).items() if key != "record_digest"}
    return DIGEST_ALGORITHM + ":" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _validate(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _FIELD_SET:
        raise ExecutionRecordError("INVALID_FIELDS")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ExecutionRecordError("UNSUPPORTED_SCHEMA")

    _text(value.get("execution_id"), "execution_id")
    if not _EXECUTION_ID_RE.fullmatch(value["execution_id"]):
        raise ExecutionRecordError("INVALID_EXECUTION_ID")
    _text(value.get("session_ref"), "session_ref", ref=True)
    _integer(value.get("session_generation"), "session_generation")
    _text(value.get("turn_ref"), "turn_ref", ref=True)
    _text(value.get("call_ref"), "call_ref", ref=True)
    _optional_text(value.get("parent_execution_ref"), "parent_execution_ref", ref=True)
    _optional_text(value.get("source_revision"), "source_revision", ref=True)
    _optional_text(value.get("runtime_identity"), "runtime_identity", ref=True)
    _text(value.get("admission_ref"), "admission_ref", ref=True)
    _optional_text(value.get("materialization_ref"), "materialization_ref", ref=True)
    _optional_digest(value.get("logical_request_digest"), "logical_request_digest")
    _text(value.get("logical_projection_schema"), "logical_projection_schema")
    _optional_digest(value.get("wire_request_digest"), "wire_request_digest")
    _optional_integer(value.get("wire_bytes"), "wire_bytes")
    _optional_text(value.get("transport_version"), "transport_version")
    _optional_text(value.get("route_ref"), "route_ref", ref=True)
    _string_list(value.get("dispatch_intent_refs"), "dispatch_intent_refs")
    _string_list(value.get("receipt_refs"), "receipt_refs")

    state = value.get("state")
    if state not in {item.value for item in ExecutionState}:
        raise ExecutionRecordError("INVALID_STATE")
    outcome = value.get("terminal_outcome")
    if state in TERMINAL_STATES:
        if outcome not in TERMINAL_OUTCOMES:
            raise ExecutionRecordError("INVALID_TERMINAL_OUTCOME")
    elif outcome is not None:
        raise ExecutionRecordError("TERMINAL_OUTCOME_ON_OPEN_STATE")

    _optional_digest(value.get("response_digest"), "response_digest")
    _optional_integer(value.get("http_status"), "http_status", minimum=100)
    if value.get("http_status") is not None and value["http_status"] > 599:
        raise ExecutionRecordError("INVALID_HTTP_STATUS", "http_status")
    _optional_text(value.get("provider_error_code"), "provider_error_code")

    if type(value.get("governor_qualified")) is not bool:
        raise ExecutionRecordError("INVALID_BOOLEAN", "governor_qualified")
    if value.get("governor_mode") not in GOVERNOR_MODES:
        raise ExecutionRecordError("INVALID_GOVERNOR_MODE")
    _integer(value.get("sequence"), "sequence", minimum=1)
    recorded_at = value.get("recorded_at")
    if (
        type(recorded_at) not in (int, float)
        or not math.isfinite(recorded_at)
        or recorded_at < 0
    ):
        raise ExecutionRecordError("INVALID_RECORDED_AT")
    _digest(value.get("record_digest"), "record_digest")
    if value.get("record_digest") != digest_record(value):
        raise ExecutionRecordError("RECORD_DIGEST_MISMATCH")


@dataclass(frozen=True)
class ExecutionRecordV1:
    """One immutable revision of an execution record."""

    _values: Mapping[str, Any]

    @classmethod
    def create(cls, values: Mapping[str, Any]) -> "ExecutionRecordV1":
        value = {field: None for field in FIELDS if field != "record_digest"}
        value.update(dict(values))
        value["schema_version"] = SCHEMA_VERSION
        if value.get("logical_projection_schema") is None:
            value["logical_projection_schema"] = "ares-json-sorted-v1"
        if value.get("dispatch_intent_refs") is None:
            value["dispatch_intent_refs"] = []
        if value.get("receipt_refs") is None:
            value["receipt_refs"] = []
        if value.get("governor_qualified") is None:
            value["governor_qualified"] = False
        if value.get("governor_mode") is None:
            value["governor_mode"] = "unavailable"
        if value.get("state") is None:
            value["state"] = ExecutionState.ADMITTED.value
        if value.get("sequence") is None:
            value["sequence"] = 1
        if value.get("recorded_at") is None:
            value["recorded_at"] = time.time()
        value.pop("record_digest", None)
        value["record_digest"] = digest_record(value)
        _validate(value)
        return cls(value)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "ExecutionRecordV1":
        value = dict(raw)
        _validate(value)
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (list(item) if isinstance(item, list) else item)
            for key, item in self._values.items()
        }

    @property
    def execution_id(self) -> str:
        return self._values["execution_id"]

    @property
    def state(self) -> str:
        return self._values["state"]

    @property
    def sequence(self) -> int:
        return self._values["sequence"]

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self._values)

    def evolve(self, **updates: Any) -> "ExecutionRecordV1":
        """Advance the record revision without changing its state."""
        if self.is_terminal:
            raise ExecutionRecordError("TERMINAL_STATE_IMMUTABLE", self.state)
        for key in updates:
            if key not in _FIELD_SET or key in _IMMUTABLE_AFTER_CREATE:
                raise ExecutionRecordError("FIELD_NOT_UPDATABLE", key)
        value = dict(self._values)
        value.update(updates)
        value["sequence"] = self.sequence + 1
        value["recorded_at"] = updates.get("recorded_at", time.time())
        value.pop("record_digest", None)
        value["record_digest"] = digest_record(value)
        _validate(value)
        return ExecutionRecordV1(value)

    def transition(self, new_state: str, **updates: Any) -> "ExecutionRecordV1":
        current = self.state
        if current in TERMINAL_STATES:
            raise ExecutionRecordError("TERMINAL_STATE_IMMUTABLE", current)
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise ExecutionRecordError("ILLEGAL_TRANSITION", f"{current}->{new_state}")

        for key in updates:
            if key not in _FIELD_SET or key in _IMMUTABLE_AFTER_CREATE:
                raise ExecutionRecordError("FIELD_NOT_UPDATABLE", key)

        value = dict(self._values)
        value.update(updates)
        value["state"] = new_state
        value["sequence"] = self.sequence + 1
        value["recorded_at"] = updates.get("recorded_at", time.time())

        if new_state == ExecutionState.MATERIALIZED.value:
            if value.get("materialization_ref") is None:
                raise ExecutionRecordError("MATERIALIZATION_REF_REQUIRED")
            if value.get("logical_request_digest") is None:
                raise ExecutionRecordError("LOGICAL_DIGEST_REQUIRED")
        elif new_state == ExecutionState.SEND_STARTED.value:
            if value.get("wire_request_digest") is None:
                raise ExecutionRecordError("WIRE_DIGEST_REQUIRED")
            if value.get("wire_bytes") is None:
                raise ExecutionRecordError("WIRE_BYTES_REQUIRED")
        elif new_state == ExecutionState.SETTLED.value:
            if value.get("terminal_outcome") not in TERMINAL_OUTCOMES:
                raise ExecutionRecordError("TERMINAL_OUTCOME_REQUIRED")
        elif new_state == ExecutionState.DENIED.value:
            value["terminal_outcome"] = "denied"
        elif new_state == ExecutionState.ABANDONED.value:
            value["terminal_outcome"] = "never_sent"
        elif new_state == ExecutionState.INDETERMINATE.value:
            value["terminal_outcome"] = "unknown"

        value.pop("record_digest", None)
        value["record_digest"] = digest_record(value)
        _validate(value)
        return ExecutionRecordV1(value)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANONICALIZATION",
    "DIGEST_ALGORITHM",
    "ExecutionRecordError",
    "ExecutionRecordV1",
    "ExecutionState",
    "FIELDS",
    "GOVERNOR_MODES",
    "SCHEMA_VERSION",
    "TERMINAL_OUTCOMES",
    "TERMINAL_STATES",
    "canonical_bytes",
    "digest_record",
]
