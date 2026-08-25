from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ProtocolValidationError(ValueError):
    """A typed failure for malformed or semantically invalid mobile frames."""


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{name} must be an object")
    return value


def _required_string(obj: Mapping[str, Any], key: str, *, scope: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolValidationError(f"{scope}.{key} must be a non-empty string")
    return value


def _required_nonnegative_int(obj: Mapping[str, Any], key: str, *, scope: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolValidationError(f"{scope}.{key} must be a non-negative integer")
    return value


def validate_event_envelope(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the identity and cursor fields of one authoritative event."""
    obj = _mapping(event, name="event")
    for key in (
        "host_id",
        "connection_id",
        "profile",
        "session_id",
        "lineage_root_id",
        "runtime_id",
        "event_id",
        "type",
    ):
        _required_string(obj, key, scope="event")
    _required_nonnegative_int(obj, "revision", scope="event")
    if "payload" in obj and not isinstance(obj["payload"], Mapping):
        raise ProtocolValidationError("event.payload must be an object when present")
    if "critical" in obj and not isinstance(obj["critical"], bool):
        raise ProtocolValidationError("event.critical must be boolean when present")
    return dict(obj)


def validate_session_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimum authoritative operator snapshot shape."""
    obj = _mapping(snapshot, name="snapshot")
    scope = _mapping(obj.get("scope"), name="snapshot.scope")
    for key in (
        "host_id",
        "connection_id",
        "profile",
        "session_id",
        "lineage_root_id",
        "runtime_id",
    ):
        _required_string(scope, key, scope="snapshot.scope")
    schema_version = obj.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise ProtocolValidationError("snapshot.schema_version must be a positive integer")
    _required_nonnegative_int(obj, "revision", scope="snapshot")
    _required_string(obj, "status", scope="snapshot")
    for key in ("transcript_tail", "pending_requests"):
        if not isinstance(obj.get(key), list):
            raise ProtocolValidationError(f"snapshot.{key} must be an array")
    if obj.get("transcript_cursor") is not None and not isinstance(obj["transcript_cursor"], str):
        raise ProtocolValidationError("snapshot.transcript_cursor must be a string or null")
    if not isinstance(obj.get("controller"), Mapping):
        raise ProtocolValidationError("snapshot.controller must be an object")
    return dict(obj)
