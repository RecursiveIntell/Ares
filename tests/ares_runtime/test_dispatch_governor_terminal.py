"""Correlated terminal attempts must not reach the physical transport.

Uses real SessionDB, middleware, sync/async HTTPX and MockTransport (no network).
The local call-basis fixture is not a native grant or mission admission.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ares_runtime import physical_dispatch as physical
from hermes_cli import middleware
from hermes_state_execution import ExecutionRecordStoreError
from plugins.dispatch_governor import DispatchGovernor
from tests.ares_runtime.test_dispatch_governor_plugin import _observation

pytest_plugins = ("tests.ares_runtime.test_dispatch_governor_plugin",)
EXECUTION = "turn:api:1:0"
BODY = json.dumps({
    "model": "local",
    "input": "offline",
    "max_output_tokens": 7,
}).encode()
URL = "https://offline.invalid/v1/responses"
TERMINAL = ("denied", "abandoned", "settled", "indeterminate")


def terminate(db, state):
    if state in {"denied", "abandoned"}:
        db.transition_execution_record(EXECUTION, state)
    else:
        ticket = DispatchGovernor(store_factory=lambda: db, mode="enforce")(
            payload=_observation(), dispatch_phase="physical_send"
        )
        ticket.settle(outcome="ok" if state == "settled" else "unknown")
    assert db.read_execution_record(EXECUTION).state == state


def bind(governor, monkeypatch):
    monkeypatch.setattr(
        middleware,
        "_get_middleware_callbacks",
        lambda kind: [governor] if kind == "llm_dispatch" else [],
    )
    return physical._call.set({
        "execution_id": EXECUTION,
        "materialization": {},
        "qualification_error": None,
    })


def response(request, sends):
    sends.append(request.content)
    return httpx.Response(200, stream=httpx.ByteStream(b"offline result"))


def assert_result(mode, sends, denied):
    if mode == "enforce":
        assert sends == [], "terminal correlated execution reached physical transport"
        assert denied is not None, "terminal correlated execution was not denied"
        assert denied.code == "DISPATCH_EXECUTION_TERMINAL"
    else:
        assert sends == [BODY]
        assert denied is None


@pytest.mark.parametrize("state", TERMINAL)
@pytest.mark.parametrize("mode", ["enforce", "observe"])
def test_sync_terminal_physical_dispatch(db, monkeypatch, state, mode):
    terminate(db, state)
    before = db.read_execution_record(EXECUTION).to_dict()
    changes = db._conn.total_changes
    governor = DispatchGovernor(store_factory=lambda: db, mode=mode)
    token = bind(governor, monkeypatch)
    sends, denied = [], None
    try:
        inner = httpx.MockTransport(lambda request: response(request, sends))
        with httpx.Client(transport=physical._SyncTransport(inner)) as client:
            try:
                client.post(URL, content=BODY).read()
            except middleware.EgressDispatchDenied as error:
                denied = error
    finally:
        physical._call.reset(token)
    assert_result(mode, sends, denied)
    assert db.read_execution_record(EXECUTION).to_dict() == before
    assert db._conn.total_changes == changes


@pytest.mark.asyncio
@pytest.mark.parametrize("state", TERMINAL)
@pytest.mark.parametrize("mode", ["enforce", "observe"])
async def test_async_terminal_physical_dispatch(db, monkeypatch, state, mode):
    terminate(db, state)
    before = db.read_execution_record(EXECUTION).to_dict()
    changes = db._conn.total_changes
    governor = DispatchGovernor(store_factory=lambda: db, mode=mode)
    token = bind(governor, monkeypatch)
    sends, denied = [], None
    try:
        inner = httpx.MockTransport(lambda request: response(request, sends))
        async with httpx.AsyncClient(
            transport=physical._AsyncTransport(inner)
        ) as client:
            try:
                result = await client.post(URL, content=BODY)
                await result.aread()
            except middleware.EgressDispatchDenied as error:
                denied = error
    finally:
        physical._call.reset(token)
    assert_result(mode, sends, denied)
    assert db.read_execution_record(EXECUTION).to_dict() == before
    assert db._conn.total_changes == changes


@pytest.mark.parametrize("conflict", ["ALREADY_TRANSITIONED", "ALREADY_TERMINAL"])
@pytest.mark.parametrize("state", TERMINAL)
def test_terminal_cas_reread_cannot_release_transport(db, monkeypatch, conflict, state):
    class ConcurrentTerminalStore:
        def read_execution_record(self, execution_id):
            return db.read_execution_record(execution_id)

        def transition_execution_record(self, execution_id, target, **fields):
            assert target == "send_started"
            terminate(db, state)
            raise ExecutionRecordStoreError(conflict)

    governor = DispatchGovernor(store_factory=ConcurrentTerminalStore, mode="enforce")
    token = bind(governor, monkeypatch)
    sends, denied = [], None
    try:
        inner = httpx.MockTransport(lambda request: response(request, sends))
        with httpx.Client(transport=physical._SyncTransport(inner)) as client:
            try:
                client.post(URL, content=BODY).read()
            except middleware.EgressDispatchDenied as error:
                denied = error
    finally:
        physical._call.reset(token)
    assert_result("enforce", sends, denied)
    assert db.read_execution_record(EXECUTION).state == state


def test_nonterminal_positive_dispatch_still_sends_and_settles(db, monkeypatch):
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    token = bind(governor, monkeypatch)
    sends = []
    try:

        def handle(request):
            assert db.read_execution_record(EXECUTION).state == "send_started"
            return response(request, sends)

        with httpx.Client(
            transport=physical._SyncTransport(httpx.MockTransport(handle))
        ) as client:
            assert client.post(URL, content=BODY).read() == b"offline result"
    finally:
        physical._call.reset(token)
    record = db.read_execution_record(EXECUTION)
    assert sends == [BODY] and record.state == "settled"
    assert record.to_dict()["terminal_outcome"] == "ok"
