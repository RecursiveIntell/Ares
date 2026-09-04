from __future__ import annotations

import copy

import pytest

from tui_gateway.protocol import (
    ProtocolValidationError,
    build_gateway_ready_payload,
    protocol_manifest,
    validate_event_envelope,
    validate_session_snapshot,
)


def test_protocol_manifest_is_stable_and_has_explicit_capabilities() -> None:
    first = protocol_manifest()
    second = protocol_manifest()

    assert first == second
    assert first["protocol_revision"] == 2
    assert isinstance(first["schema_digest"], str)
    assert len(first["schema_digest"]) == 64
    assert set(first["capabilities"]) == {
        "session_observe_v1",
        "session_snapshot_v1",
        "event_cursor_v1",
        "bounded_replay_v1",
        "session_control_lease_v1",
        "write_idempotency_v1",
        "mobile_surface_v1",
    }
    assert all(value is False for value in first["capabilities"].values())


def test_gateway_ready_payload_is_versioned_and_capability_closed() -> None:
    payload = build_gateway_ready_payload(
        host_identity_digest="host-test",
        runtime_id="runtime-test",
    )

    assert payload["protocol_revision"] == 2
    assert payload["host_identity_digest"] == "host-test"
    assert payload["runtime_id"] == "runtime-test"
    assert payload["schema_digest"] == protocol_manifest()["schema_digest"]
    assert payload["capabilities"]["session_observe_v1"] is False


def test_event_envelope_requires_scope_revision_and_event_identity() -> None:
    event = {
        "host_id": "host-test",
        "connection_id": "connection-test",
        "profile": "default",
        "session_id": "session-test",
        "lineage_root_id": "lineage-test",
        "runtime_id": "runtime-test",
        "revision": 3,
        "event_id": "event-test",
        "type": "message.delta",
        "payload": {"text": "safe fixture"},
    }

    assert validate_event_envelope(event) == event

    invalid = copy.deepcopy(event)
    invalid["revision"] = -1
    with pytest.raises(ProtocolValidationError, match="revision"):
        validate_event_envelope(invalid)

    invalid = copy.deepcopy(event)
    invalid.pop("runtime_id")
    with pytest.raises(ProtocolValidationError, match="runtime_id"):
        validate_event_envelope(invalid)


def test_snapshot_requires_authoritative_operator_state() -> None:
    snapshot = {
        "scope": {
            "host_id": "host-test",
            "connection_id": "connection-test",
            "profile": "default",
            "session_id": "session-test",
            "lineage_root_id": "lineage-test",
            "runtime_id": "runtime-test",
        },
        "schema_version": 1,
        "revision": 4,
        "status": "waiting_for_user",
        "transcript_tail": [],
        "transcript_cursor": None,
        "pending_requests": [],
        "controller": {"state": "none"},
    }

    assert validate_session_snapshot(snapshot) == snapshot

    invalid = copy.deepcopy(snapshot)
    invalid["controller"] = None
    with pytest.raises(ProtocolValidationError, match="controller"):
        validate_session_snapshot(invalid)

    invalid = copy.deepcopy(snapshot)
    invalid["scope"].pop("host_id")
    with pytest.raises(ProtocolValidationError, match="host_id"):
        validate_session_snapshot(invalid)


def test_host_identity_is_configured_server_state_and_fails_closed(monkeypatch, tmp_path) -> None:
    from tui_gateway.protocol import registry

    monkeypatch.setattr(registry, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        "mobile:\n  host_identity_digest: " + "a" * 64 + "\n", encoding="utf-8"
    )
    assert registry.host_identity_digest() == "a" * 64

    (tmp_path / "config.yaml").write_text(
        "mobile:\n  host_identity_digest: not-a-digest\n", encoding="utf-8"
    )
    assert registry.host_identity_digest() == "unbound"
