from __future__ import annotations

import hashlib
import json
import sys
import types
from contextlib import contextmanager
from typing import cast

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.request_outcomes import mobile_request_outcome_scope_key
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
        self.rows: dict[tuple[str, str, str], dict] = {}

    def mobile_request_outcome_reserve(self, **kwargs):
        key = (kwargs["scope_key"], kwargs["method"], kwargs["idempotency_key"])
        row = self.rows.get(key)
        if row is not None:
            return {**row, "created": False, "conflict": row["payload_digest"] != kwargs["payload_digest"]}
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
        self.rows[key] = row
        return {**row, "created": True, "conflict": False}

    def mobile_request_outcome_finish(self, **kwargs):
        key = (kwargs["scope_key"], kwargs["method"], kwargs["idempotency_key"])
        row = self.rows[key]
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


@pytest.fixture
def approval_env(monkeypatch):
    db = _OutcomeDb()
    session = {"session_key": "stored", "profile": "default"}
    old_caps = dict(server._mobile_protocol_capabilities)
    monkeypatch.setattr(server, "_session_controller", SessionController())
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: (session, None))
    monkeypatch.setitem(server._mobile_protocol_capabilities, "session_control_lease_v1", True)
    monkeypatch.setitem(server._mobile_protocol_capabilities, "write_idempotency_v1", True)

    @contextmanager
    def scoped_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", scoped_db)
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "tools.approval",
        types.SimpleNamespace(
            resolve_gateway_approval=lambda *_args, **_kwargs: calls.append((_args, _kwargs)) or 1
        ),
    )
    yield db, session, calls
    server._mobile_protocol_capabilities.clear()
    server._mobile_protocol_capabilities.update(old_caps)


def test_mobile_approval_retry_replays_terminal_outcome_once(approval_env):
    _db, _session, calls = approval_env
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
        "idempotency_key": "approval-response-1",
        "request_id": "approval-1",
        "choice": "deny",
    }
    token = bind_transport(cast(Transport, _LoopbackTransport()))
    try:
        first = server._methods["approval.respond"]("first", params)
        second = server._methods["approval.respond"]("second", params)
    finally:
        reset_transport(token)
    assert first["result"]["resolved"] == 1
    assert second["result"]["resolved"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["in_progress", "indeterminate"])
def test_mobile_approval_never_reexecutes_a_preexisting_nonreplayable_outcome(approval_env, state):
    db, session, calls = approval_env
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
        db.rows[key] = row
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
        "request_id": "approval-1",
        "choice": "deny",
    }
    token = bind_transport(cast(Transport, _LoopbackTransport()))
    try:
        response = server._methods["approval.respond"]("prior", params)
    finally:
        reset_transport(token)

    assert calls == []
    assert response["error"]["code"] == 4412
    assert response["error"]["data"]["reason"] == f"outcome_{state}"


def test_mobile_approval_real_sessiondb_in_progress_is_never_reexecuted(approval_env, monkeypatch, tmp_path):
    _fake_db, session, calls = approval_env
    db = SessionDB(db_path=tmp_path / "state.db")
    host = server.host_identity_digest()
    runtime = server.protocol_runtime_id()
    principal = "loopback"
    scope_key = mobile_request_outcome_scope_key(
        host_id=host,
        profile="default",
        session_key=session["session_key"],
        principal_id=principal,
    )
    payload_digest = hashlib.sha256(
        json.dumps(
            {"choice": "deny", "resolve_all": False, "request_id": "approval-1"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    db.mobile_request_outcome_reserve(
        scope_key=scope_key,
        method="approval.respond",
        idempotency_key="persisted-prior",
        payload_digest=payload_digest,
    )

    @contextmanager
    def scoped_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", scoped_db)
    lease = server._session_controller.acquire(
        host_id=host,
        profile="default",
        session_id="live",
        runtime_id=runtime,
        principal_id=principal,
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
        "idempotency_key": "persisted-prior",
        "request_id": "approval-1",
        "choice": "deny",
    }
    token = bind_transport(cast(Transport, _LoopbackTransport()))
    try:
        response = server._methods["approval.respond"]("persisted-prior", params)
    finally:
        reset_transport(token)
        db.close()

    assert calls == []
    assert response["error"]["code"] == 4412
    assert response["error"]["data"]["reason"] == "outcome_in_progress"


def test_mobile_approval_idempotency_scope_includes_authenticated_principal(approval_env):
    db, _session, calls = approval_env
    host = server.host_identity_digest()
    runtime = server.protocol_runtime_id()

    def request_for(principal, instance, lease):
        return {
            "session_id": "live",
            "profile": "default",
            "host_id": host,
            "runtime_id": runtime,
            "controller_instance_id": instance,
            "generation": lease.generation,
            "fencing_token": lease.fencing_token,
            "idempotency_key": "same-key",
            "request_id": "approval-1",
            "choice": "deny",
            "principal": principal,
        }

    desktop = _LoopbackTransport()
    desktop.auth_identity = {"user_id": "desktop"}
    first_lease = server._session_controller.acquire(
        host_id=host,
        profile="default",
        session_id="live",
        runtime_id=runtime,
        principal_id="desktop",
        controller_instance_id="desktop-window",
        ttl_seconds=60,
    )
    token = bind_transport(cast(Transport, desktop))
    try:
        first = server._methods["approval.respond"](
            "desktop",
            request_for("desktop", "desktop-window", first_lease),
        )
    finally:
        reset_transport(token)
    assert first["result"]["resolved"] == 1
    assert server._session_controller.release(first_lease) is True

    mobile = _LoopbackTransport()
    mobile.auth_identity = {"user_id": "mobile"}
    second_lease = server._session_controller.acquire(
        host_id=host,
        profile="default",
        session_id="live",
        runtime_id=runtime,
        principal_id="mobile",
        controller_instance_id="phone",
        ttl_seconds=60,
    )
    token = bind_transport(cast(Transport, mobile))
    try:
        second = server._methods["approval.respond"](
            "mobile",
            request_for("mobile", "phone", second_lease),
        )
    finally:
        reset_transport(token)

    assert second["result"]["resolved"] == 1
    assert len(calls) == 2
    assert len(db.rows) == 2
