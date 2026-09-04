from __future__ import annotations

import copy
import threading

import pytest

from tui_gateway import server
from tui_gateway.protocol.registry import protocol_runtime_id
from tui_gateway.transport import bind_transport, reset_transport


class FakeTransport:
    def __init__(self, *, user: str) -> None:
        self.auth_identity = {"user_id": user, "provider": "test"}
        self.frames: list[dict] = []
        self.closed = False

    def write(self, obj: dict) -> bool:
        self.frames.append(copy.deepcopy(obj))
        return True

    def close(self) -> None:
        self.closed = True


def _session(*, primary: FakeTransport, profile: str = "default") -> dict:
    return {
        "transport": primary,
        "history": [{"role": "user", "content": "fixture"}],
        "session_key": "stored-session",
        "profile": profile,
        "running": False,
        "pending_requests": [],
        "lineage_root_id": "lineage-session",
        "history_lock": threading.Lock(),
    }


@pytest.fixture
def mobile_gateway_state(monkeypatch):
    old_sessions = dict(server._sessions)
    old_revisions = dict(server._session_event_revisions)
    old_caps = dict(server._mobile_protocol_capabilities)
    server._sessions.clear()
    server._session_event_revisions.clear()
    server.observer_registry.clear()
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_observe_v1", True)
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_snapshot_v1", True)
    monkeypatch.setitem(server._mobile_protocol_capabilities, "mobile_surface_v1", True)
    yield
    server._sessions.clear()
    server._sessions.update(old_sessions)
    server._session_event_revisions.clear()
    server._session_event_revisions.update(old_revisions)
    server.observer_registry.clear()
    server._mobile_protocol_capabilities.clear()
    server._mobile_protocol_capabilities.update(old_caps)


def test_snapshot_is_authoritative_and_observe_does_not_resume(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    observer = FakeTransport(user="mobile")
    server._sessions["live-session"] = _session(primary=primary)

    token = bind_transport(observer)
    try:
        snapshot = server._methods["session.snapshot"](
            "snap-1", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
        observed = server._methods["session.observe"](
            "observe-1", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)

    assert snapshot["result"]["snapshot"]["scope"]["session_id"] == "live-session"
    assert observed["result"]["observing"] is True
    assert observed["result"]["runtime_id"] == protocol_runtime_id()
    assert "session.resume" not in observer.frames


def test_mobile_session_list_returns_route_qualified_projection(mobile_gateway_state, monkeypatch):
    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def list_sessions_rich(self, **_kwargs):
            return [{
                "id": "stored-session",
                "title": "Fixture",
                "preview": "hello",
                "message_count": 1,
                "started_at": 12.5,
                "lineage_root_id": "lineage-session",
            }]

    monkeypatch.setattr(server, "_profile_db", lambda _params: FakeDB())
    token = bind_transport(FakeTransport(user="mobile"))
    try:
        listed = server._methods["session.mobile.list"](
            "list-1",
            {
                "host_id": server.host_identity_digest(),
                "profile": "default",
            },
        )
    finally:
        reset_transport(token)

    item = listed["result"]["sessions"][0]
    assert item["scope"] == {
        "host_id": "unbound",
        "connection_id": "local",
        "profile": "default",
        "session_id": "stored-session",
        "lineage_root_id": "lineage-session",
        "runtime_id": protocol_runtime_id(),
    }


def test_observer_receives_annotated_event_while_primary_continues(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    observer = FakeTransport(user="mobile")
    server._sessions["live-session"] = _session(primary=primary)

    token = bind_transport(observer)
    try:
        result = server._methods["session.observe"](
            "observe-1", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)

    assert result["result"]["observing"] is True
    server._emit("message.delta", "live-session", {"text": "fixture"})

    assert len(primary.frames) == 1
    assert len(observer.frames) == 1
    event = observer.frames[0]["params"]
    assert event["type"] == "message.delta"
    assert event["runtime_id"] == protocol_runtime_id()
    assert event["revision"] == 1
    assert event["event_id"]
    assert event["host_id"] == "unbound"
    assert event["connection_id"] == "local"


def test_wrong_profile_and_unauthenticated_observer_fail_closed(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    server._sessions["live-session"] = _session(primary=primary)

    token = bind_transport(FakeTransport(user="mobile"))
    try:
        wrong_profile = server._methods["session.observe"](
            "observe-1", {"session_id": "live-session", "profile": "other", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)

    assert wrong_profile["error"]["code"] == 4403

    unauthenticated = FakeTransport(user="mobile")
    unauthenticated.auth_identity = None
    token = bind_transport(unauthenticated)
    try:
        denied = server._methods["session.observe"](
            "observe-2", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)

    assert denied["error"]["code"] == 4403


def test_wrong_host_observer_fails_closed(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    server._sessions["live-session"] = _session(primary=primary)
    token = bind_transport(FakeTransport(user="mobile"))
    try:
        denied = server._methods["session.observe"](
            "observe-wrong-host",
            {"session_id": "live-session", "profile": "default", "host_id": "b" * 64},
        )
    finally:
        reset_transport(token)
    assert denied["error"]["code"] == 4403
    assert denied["error"]["data"]["reason"] == "wrong_host"


def test_unadvertised_observer_capability_is_typed_and_disabled(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    server._sessions["live-session"] = _session(primary=primary)
    server._mobile_protocol_capabilities["session_observe_v1"] = False

    token = bind_transport(FakeTransport(user="mobile"))
    try:
        denied = server._methods["session.observe"](
            "observe-1", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)

    assert denied["error"]["code"] == 4401
    assert denied["error"]["data"]["capability"] == "session_observe_v1"


def test_observer_reports_snapshot_required_for_unproven_cursor(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    observer = FakeTransport(user="mobile")
    server._sessions["live-session"] = _session(primary=primary)

    token = bind_transport(observer)
    try:
        first = server._methods["session.observe"](
            "observe-1", {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()}
        )
    finally:
        reset_transport(token)
    assert first["result"]["snapshot_required"] is False

    server._emit("message.delta", "live-session", {"text": "one"})
    server._emit("message.delta", "live-session", {"text": "two"})

    token = bind_transport(observer)
    try:
        reconnect = server._methods["session.observe"](
            "observe-2",
            {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest(), "last_seen_revision": 0},
        )
    finally:
        reset_transport(token)

    assert reconnect["result"]["current_revision"] == 2
    assert reconnect["result"]["snapshot_required"] is True


def test_observer_does_not_receive_reused_session_id_from_a_different_profile(mobile_gateway_state):
    first_primary = FakeTransport(user="desktop-default")
    observer = FakeTransport(user="mobile-default")
    server._sessions["reused-session"] = _session(primary=first_primary, profile="default")

    token = bind_transport(observer)
    try:
        observed = server._methods["session.observe"](
            "observe-default",
            {"session_id": "reused-session", "profile": "default", "host_id": server.host_identity_digest()},
        )
    finally:
        reset_transport(token)
    assert observed["result"]["observing"] is True

    other_primary = FakeTransport(user="desktop-other")
    server._sessions["reused-session"] = _session(primary=other_primary, profile="other")
    server._emit("message.delta", "reused-session", {"text": "other-profile-only"})

    assert observer.frames == []
    assert len(other_primary.frames) == 1


def test_unobserve_requires_exact_host_and_profile_scope(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    observer = FakeTransport(user="mobile")
    server._sessions["live-session"] = _session(primary=primary, profile="default")

    token = bind_transport(observer)
    try:
        server._methods["session.observe"](
            "observe",
            {"session_id": "live-session", "profile": "default", "host_id": server.host_identity_digest()},
        )
        denied = server._methods["session.unobserve"](
            "unobserve-wrong-profile",
            {"session_id": "live-session", "profile": "other", "host_id": server.host_identity_digest()},
        )
    finally:
        reset_transport(token)

    assert denied["error"]["code"] == 4403
    server._emit("message.delta", "live-session", {"text": "still-observed"})
    assert len(observer.frames) == 1


def test_runtime_generation_mismatch_forces_snapshot_recovery(mobile_gateway_state):
    primary = FakeTransport(user="desktop")
    observer = FakeTransport(user="mobile")
    server._sessions["live-session"] = _session(primary=primary)

    token = bind_transport(observer)
    try:
        observed = server._methods["session.observe"](
            "observe-old-runtime",
            {
                "session_id": "live-session",
                "profile": "default",
                "host_id": server.host_identity_digest(),
                "runtime_id": "previous-runtime",
                "last_seen_revision": 0,
            },
        )
    finally:
        reset_transport(token)

    assert observed["result"]["snapshot_required"] is True
