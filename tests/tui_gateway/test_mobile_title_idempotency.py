from __future__ import annotations

from contextlib import contextmanager
from typing import cast

import pytest

from tui_gateway import server
from tui_gateway.session_control import SessionController
from tui_gateway.transport import Transport, bind_transport, reset_transport


class _LoopbackTransport:
    _peer = "127.0.0.1:45678"
    auth_identity: dict[str, str] | None = None

    def close(self) -> None:
        return None

    def write(self, _frame: dict) -> bool:
        return True


class _OutcomeDb:
    def __init__(self) -> None:
        self.requests: dict[tuple[str, str, str], dict] = {}
        self.title_writes = 0

    def mobile_request_outcome_reserve(self, **kwargs):
        key = (kwargs["scope_key"], kwargs["method"], kwargs["idempotency_key"])
        existing = self.requests.get(key)
        if existing:
            return {**existing, "created": False, "conflict": existing["payload_digest"] != kwargs["payload_digest"]}
        row = {
            "scope_key": kwargs["scope_key"],
            "method": kwargs["method"],
            "idempotency_key": kwargs["idempotency_key"],
            "payload_digest": kwargs["payload_digest"],
            "state": "in_progress",
            "result_json": None,
            "error_json": None,
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        self.requests[key] = row
        return {**row, "created": True, "conflict": False}

    def mobile_request_outcome_finish(self, **kwargs):
        key = (kwargs["scope_key"], kwargs["method"], kwargs["idempotency_key"])
        row = self.requests[key]
        if row["state"] == "in_progress":
            row.update(
                {
                    "state": kwargs["state"],
                    "result_json": kwargs["result_json"],
                    "error_json": kwargs["error_json"],
                    "updated_at": 2.0,
                }
            )
        return {**row, "created": False, "conflict": False}

    def set_session_title(self, _key, _title):
        self.title_writes += 1
        return True

    def get_session(self, _key):
        return {"title": "safe"}


@pytest.fixture
def mobile_title_env(monkeypatch):
    db = _OutcomeDb()
    session = {"session_key": "stored", "profile": "default", "pending_title": None}
    old_sessions = dict(server._sessions)
    old_caps = dict(server._mobile_protocol_capabilities)
    server._sessions.clear()
    server._sessions["live"] = session
    monkeypatch.setattr(server, "_session_controller", SessionController())
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_control_lease_v1", True)
    monkeypatch.setitem(server._mobile_protocol_capabilities, "write_idempotency_v1", True)

    @contextmanager
    def scoped_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", scoped_db)
    monkeypatch.setattr(server, "_emit_session_info_for_session", lambda *_args: None)
    yield db, session
    server._sessions.clear()
    server._sessions.update(old_sessions)
    server._mobile_protocol_capabilities.clear()
    server._mobile_protocol_capabilities.update(old_caps)


def test_mobile_title_retry_replays_durable_outcome_without_second_write(mobile_title_env):
    db, _session = mobile_title_env
    transport = _LoopbackTransport()
    host = server.host_identity_digest()
    runtime = server.protocol_runtime_id()
    lease = server._session_controller.acquire(
        host_id=host,
        profile="default",
        session_id="live",
        runtime_id=runtime,
        principal_id="loopback",
        controller_instance_id="client-a",
        ttl_seconds=60,
    )
    params = {
        "session_id": "live",
        "profile": "default",
        "host_id": host,
        "runtime_id": runtime,
        "controller_instance_id": "client-a",
        "generation": lease.generation,
        "fencing_token": lease.fencing_token,
        "idempotency_key": "title-request-1",
        "title": "safe",
    }
    token = bind_transport(cast(Transport, transport))
    try:
        first = server._methods["session.title"]("first", params)
        second = server._methods["session.title"]("second", params)
    finally:
        reset_transport(token)
    assert first["result"]["title"] == "safe"
    assert second["result"]["title"] == "safe"
    assert db.title_writes == 1


@pytest.mark.parametrize("state", ["in_progress", "indeterminate"])
def test_mobile_title_never_reexecutes_a_preexisting_nonreplayable_outcome(mobile_title_env, state):
    db, _session = mobile_title_env
    host = server.host_identity_digest()
    runtime = server.protocol_runtime_id()
    lease = server._session_controller.acquire(
        host_id=host,
        profile="default",
        session_id="live",
        runtime_id=runtime,
        principal_id="loopback",
        controller_instance_id="client-a",
        ttl_seconds=60,
    )

    def reserve_existing(**kwargs):
        key = (kwargs["scope_key"], kwargs["method"], kwargs["idempotency_key"])
        row = {
            "scope_key": kwargs["scope_key"],
            "method": kwargs["method"],
            "idempotency_key": kwargs["idempotency_key"],
            "payload_digest": kwargs["payload_digest"],
            "state": state,
            "result_json": None,
            "error_json": '{"reason":"prior-process"}',
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        db.requests[key] = row
        return {**row, "created": False, "conflict": False}

    db.mobile_request_outcome_reserve = reserve_existing
    params = {
        "session_id": "live",
        "profile": "default",
        "host_id": host,
        "runtime_id": runtime,
        "controller_instance_id": "client-a",
        "generation": lease.generation,
        "fencing_token": lease.fencing_token,
        "idempotency_key": f"prior-{state}",
        "title": "safe",
    }
    token = bind_transport(cast(Transport, _LoopbackTransport()))
    try:
        response = server._methods["session.title"]("prior", params)
    finally:
        reset_transport(token)

    assert db.title_writes == 0
    assert response["error"]["code"] == 4412
    assert response["error"]["data"]["reason"] == f"outcome_{state}"
