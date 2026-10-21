"""Refusals must be witnessed by absent effects, not only return values."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from ares_runtime.physical_dispatch import inference_call_scope
from hermes_cli import middleware
from hermes_state import SessionDB
from hermes_state_execution import ExecutionRecordStoreError
from plugins.dispatch_governor import DispatchGovernor
from tests.ares_runtime.test_physical_http_transport import client, endpoint  # noqa: F401


class MissingStore:
    def read_execution_record(self, execution_id):
        raise ExecutionRecordStoreError("EXECUTION_NOT_FOUND")


def _admitted(execution_id: str = "turn:api:1:0"):
    return ExecutionRecordV1.create({
        "execution_id": execution_id,
        "session_ref": "session:test",
        "session_generation": 0,
        "turn_ref": "phase:1",
        "call_ref": "ares-call:" + execution_id,
        "admission_ref": "context-dispatch:" + execution_id,
    })


def test_enforce_denial_sends_zero_bytes(endpoint, monkeypatch):
    import ares_runtime.managed_calls as managed_calls

    url, bodies, _statuses = endpoint
    monkeypatch.setattr(
        managed_calls,
        "produce_dispatch_materialization",
        lambda *a, **kw: {
            "execution_id": "turn:api:1:0",
            "materialization": {"schema": "ares.dispatch-materialization/v1"},
            "qualification_error": None,
        },
    )
    governor = DispatchGovernor(store_factory=MissingStore, mode="enforce")
    monkeypatch.setattr(
        middleware,
        "_get_middleware_callbacks",
        lambda kind: [governor] if kind == "llm_dispatch" else [],
    )
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 7,
    }
    with client(url) as sdk:
        with pytest.raises(middleware.EgressDispatchDenied):
            with inference_call_scope(SimpleNamespace(), payload):
                sdk.chat.completions.create(**payload)
    assert bodies == []


def test_duplicate_physical_send_keeps_one_execution(tmp_path):
    db = SessionDB(db_path=tmp_path / "duplicate.db")
    try:
        db.create_execution_record(_admitted())
        db.transition_execution_record(
            "turn:api:1:0",
            ExecutionState.MATERIALIZED.value,
            materialization_ref="ares-materialization:" + "a" * 64,
            logical_request_digest="sha256:" + "b" * 64,
        )
        governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
        first = {
            "execution_id": "turn:api:1:0",
            "dispatch_intent_ref": "ares-dispatch-intent:one",
            "provider_request_sha256": "sha256:" + "c" * 64,
            "provider_request_bytes": 10,
            "resolved_route": {
                "wire_protocol": "openai.chat_completions",
                "model_ref": "local",
            },
            "request_body_schema": "ares.immutable-provider-body/v1+blake3",
        }
        second = dict(first, dispatch_intent_ref="ares-dispatch-intent:two")
        t1 = governor(payload=first, dispatch_phase="physical_send")
        t2 = governor(payload=second, dispatch_phase="physical_send")
        assert t1 is not None and t2 is not None
        record = db.read_execution_record("turn:api:1:0")
        assert record.state == "send_started"
        assert record.to_dict()["dispatch_intent_refs"] == [
            "ares-dispatch-intent:one",
            "ares-dispatch-intent:two",
        ]
        t1.settle(outcome="ok", response={"http_status": 200})
        t2.settle(outcome="ok", response={"http_status": 200})
        settled = db.read_execution_record("turn:api:1:0")
        assert settled.state == "settled"
        assert settled.to_dict()["terminal_outcome"] == "ok"
        assert len(db.list_execution_ids()) == 1
    finally:
        db.close()
