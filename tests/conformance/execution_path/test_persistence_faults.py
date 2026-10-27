"""Persistence, transport-version, and admission fault witnesses."""

from __future__ import annotations

import pytest

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from ares_runtime.physical_dispatch import unsupported_transport_scope
from hermes_cli import middleware
from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from hermes_state_execution import ExecutionRecordStoreError
from plugins.dispatch_governor import DispatchGovernor
from tests.ares_runtime.test_continuity_input import db, accept, project  # noqa: F401


class _LeaseTicket:
    transport_lease = object()

    def settle(self, **_kwargs):
        return None


def test_unsupported_transport_denies_before_the_callback_runs(monkeypatch):
    calls = []

    def governor(**_kwargs):
        return _LeaseTicket()

    monkeypatch.setattr(
        middleware,
        "_get_middleware_callbacks",
        lambda kind: [governor] if kind == "llm_dispatch" else [],
    )
    with pytest.raises(middleware.EgressDispatchDenied) as error:
        with unsupported_transport_scope("opaque-native"):
            calls.append("callback-ran")
    assert error.value.code == "UNSUPPORTED_TRANSPORT"
    assert calls == []


def test_pending_input_admission_is_refused(db):
    db.record_context_stop("s")
    db.accept_context_input(
        "s", source="cli", event_id="pending", content="unresolved correction"
    )
    snapshot = db.read_context_rebase_snapshot("s")
    assert snapshot.has_pending_inputs
    with pytest.raises(
        ContextContinuationError, match="CONTEXT_DISPATCH_INPUT_PENDING"
    ):
        db.admit_context_dispatch(
            "s",
            turn_lease_holder="holder",
            attempt_id="premature",
            expected_snapshot_digest=snapshot.digest,
            payload_digest="sha256:" + "a" * 64,
            route_ref="test",
        )


class _FailSettleStore:
    def __init__(self, db):
        self._db = db

    def read_execution_record(self, execution_id):
        return self._db.read_execution_record(execution_id)

    def transition_execution_record(self, execution_id, state, **updates):
        if state == "settled":
            raise ExecutionRecordStoreError("SETTLEMENT_WRITE_FAILED")
        return self._db.transition_execution_record(execution_id, state, **updates)

    def append_dispatch_intent(self, execution_id, ref):
        return self._db.append_dispatch_intent(execution_id, ref)


def test_settlement_write_failure_is_unrecorded_not_success(tmp_path):
    db = SessionDB(db_path=tmp_path / "settlement-failure.db")
    execution_id = "turn:api:1:0"
    try:
        db.create_execution_record(
            ExecutionRecordV1.create({
                "execution_id": execution_id,
                "session_ref": "session:test",
                "session_generation": 0,
                "turn_ref": "phase:1",
                "call_ref": "ares-call:" + execution_id,
                "admission_ref": "context-dispatch:" + execution_id,
            })
        )
        db.transition_execution_record(
            execution_id,
            ExecutionState.MATERIALIZED.value,
            materialization_ref="ares-materialization:" + "a" * 64,
            logical_request_digest="sha256:" + "b" * 64,
        )
        db.transition_execution_record(
            execution_id,
            ExecutionState.SEND_STARTED.value,
            wire_request_digest="sha256:" + "c" * 64,
            wire_bytes=1,
            governor_qualified=True,
            governor_mode="enforce",
        )
        governor = DispatchGovernor(
            store_factory=lambda: _FailSettleStore(db), mode="enforce"
        )
        ticket = governor(
            payload={
                "execution_id": execution_id,
                "dispatch_intent_ref": "ares-dispatch-intent:one",
                "provider_request_sha256": "sha256:" + "c" * 64,
                "provider_request_bytes": 1,
                "resolved_route": {
                    "wire_protocol": "openai.chat_completions",
                    "model_ref": "local",
                },
                "request_body_schema": "ares.immutable-provider-body/v1+blake3",
            },
            dispatch_phase="physical_send",
        )
        assert ticket is not None
        with pytest.raises(middleware.EgressOutcomeUnrecorded):
            ticket.settle(outcome="ok", response={"http_status": 200})
        assert db.read_execution_record(execution_id).state == "send_started"
    finally:
        db.close()
