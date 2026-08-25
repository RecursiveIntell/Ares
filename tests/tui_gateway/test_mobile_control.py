from __future__ import annotations

import time

import pytest

from tui_gateway import server
from tui_gateway.request_outcomes import (
    IdempotencyConflict,
    RequestOutcomeLedger,
    RequestOutcomeState,
)
from tui_gateway.session_control import ControllerLeaseError, SessionController
from tui_gateway.transport import bind_transport, reset_transport


class _LoopbackTransport:
    _peer = "127.0.0.1:45678"
    auth_identity = None

    def close(self) -> None:
        return None

    def write(self, _frame: dict) -> bool:
        return True


def test_controller_lease_is_exclusive_and_fenced() -> None:
    controller = SessionController()
    first = controller.acquire(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        ttl_seconds=30,
    )

    with pytest.raises(ControllerLeaseError, match="held"):
        controller.acquire(
            host_id="host",
            profile="default",
            session_id="session",
            runtime_id="runtime",
            principal_id="mobile",
            controller_instance_id="phone",
            ttl_seconds=30,
        )

    controller.validate(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        generation=first.generation,
        fencing_token=first.fencing_token,
    )

    with pytest.raises(ControllerLeaseError, match="stale"):
        controller.validate(
            host_id="host",
            profile="default",
            session_id="session",
            runtime_id="runtime",
            principal_id="desktop",
            controller_instance_id="desktop-window",
            generation=first.generation + 1,
            fencing_token=first.fencing_token,
        )


def test_controller_handoff_invalidates_old_generation() -> None:
    controller = SessionController()
    first = controller.acquire(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        ttl_seconds=30,
    )
    second = controller.handoff(
        lease=first,
        principal_id="mobile",
        controller_instance_id="phone",
        ttl_seconds=30,
    )

    assert second.generation > first.generation
    assert second.principal_id == "mobile"
    with pytest.raises(ControllerLeaseError, match="stale"):
        controller.validate(
            host_id="host",
            profile="default",
            session_id="session",
            runtime_id="runtime",
            principal_id="desktop",
            controller_instance_id="desktop-window",
            generation=first.generation,
            fencing_token=first.fencing_token,
        )


def test_expired_controller_can_be_reacquired_with_new_generation(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("tui_gateway.session_control.time.monotonic", lambda: now[0])
    controller = SessionController()
    first = controller.acquire(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        ttl_seconds=5,
    )
    now[0] = 106.0
    second = controller.acquire(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="mobile",
        controller_instance_id="phone",
        ttl_seconds=5,
    )

    assert second.generation > first.generation
    assert controller.status(
        host_id="host", profile="default", session_id="session", runtime_id="runtime"
    ).principal_id == "mobile"


def test_controller_renew_extends_expiry_without_replacing_fencing(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("tui_gateway.session_control.time.monotonic", lambda: now[0])
    controller = SessionController()
    lease = controller.acquire(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        ttl_seconds=5,
    )
    now[0] = 102.0

    renewed = controller.renew(
        host_id="host",
        profile="default",
        session_id="session",
        runtime_id="runtime",
        principal_id="desktop",
        controller_instance_id="desktop-window",
        generation=lease.generation,
        fencing_token=lease.fencing_token,
        ttl_seconds=30,
    )

    assert renewed.generation == lease.generation
    assert renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at == 132.0
    assert controller.status(
        host_id="host", profile="default", session_id="session", runtime_id="runtime"
    ) == renewed


def test_idempotency_ledger_returns_one_outcome_and_rejects_payload_conflict() -> None:
    ledger = RequestOutcomeLedger()
    scope = ("host", "default", "session")
    first = ledger.reserve(scope=scope, method="session.title", key="request-1", payload_digest="a")
    same = ledger.reserve(scope=scope, method="session.title", key="request-1", payload_digest="a")
    assert first.state is RequestOutcomeState.IN_PROGRESS
    assert same == first

    with pytest.raises(IdempotencyConflict):
        ledger.reserve(scope=scope, method="session.title", key="request-1", payload_digest="b")

    completed = ledger.complete(first, result={"title": "safe"})
    replay = ledger.reserve(scope=scope, method="session.title", key="request-1", payload_digest="a")
    assert completed.state is RequestOutcomeState.COMPLETED
    assert replay == completed
    assert replay.result == {"title": "safe"}


def test_controller_rpc_contention_is_typed_not_internal(monkeypatch) -> None:
    transport_a = _LoopbackTransport()
    transport_b = _LoopbackTransport()
    old_sessions = dict(server._sessions)
    old_caps = dict(server._mobile_protocol_capabilities)
    server._sessions.clear()
    server._sessions["live"] = {"profile": "default"}
    monkeypatch.setattr(server, "_session_controller", SessionController())
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_control_lease_v1", True)
    base = {
        "host_id": server.host_identity_digest(),
        "profile": "default",
        "session_id": "live",
        "runtime_id": server.protocol_runtime_id(),
        "ttl_seconds": 60,
    }
    token = bind_transport(transport_a)
    try:
        first = server._methods["session.control.acquire"](
            "a", {**base, "controller_instance_id": "a"}
        )
    finally:
        reset_transport(token)
    token = bind_transport(transport_b)
    try:
        second = server._methods["session.control.acquire"](
            "b", {**base, "controller_instance_id": "b"}
        )
    finally:
        reset_transport(token)
    server._sessions.clear()
    server._sessions.update(old_sessions)
    server._mobile_protocol_capabilities.clear()
    server._mobile_protocol_capabilities.update(old_caps)
    assert first["error"] is None if "error" in first else True
    assert second["error"]["code"] == 4409
    assert second["error"]["data"]["reason"] == "lease_held"


def test_controller_renew_rpc_preserves_current_fencing(monkeypatch) -> None:
    transport = _LoopbackTransport()
    old_sessions = dict(server._sessions)
    old_caps = dict(server._mobile_protocol_capabilities)
    server._sessions.clear()
    server._sessions["live"] = {"profile": "default"}
    monkeypatch.setattr(server, "_session_controller", SessionController())
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_control_lease_v1", True)
    base = {
        "host_id": server.host_identity_digest(),
        "profile": "default",
        "session_id": "live",
        "runtime_id": server.protocol_runtime_id(),
    }
    token = bind_transport(transport)
    try:
        acquired = server._methods["session.control.acquire"](
            "acquire", {**base, "controller_instance_id": "desktop", "ttl_seconds": 5}
        )
        lease = acquired["result"]["lease"]
        renewed = server._methods["session.control.renew"](
            "renew",
            {
                **base,
                "controller_instance_id": "desktop",
                "generation": lease["generation"],
                "fencing_token": lease["fencing_token"],
                "ttl_seconds": 30,
            },
        )
    finally:
        reset_transport(token)
        server._sessions.clear()
        server._sessions.update(old_sessions)
        server._mobile_protocol_capabilities.clear()
        server._mobile_protocol_capabilities.update(old_caps)

    renewed_lease = renewed["result"]["lease"]
    assert renewed_lease["generation"] == lease["generation"]
    assert renewed_lease["fencing_token"] == lease["fencing_token"]
    assert renewed_lease["expires_at"] >= lease["expires_at"]
