"""The dispatch governor persists send_started before bytes and settles once."""

from __future__ import annotations

import pytest

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from hermes_cli.middleware import EgressDispatchDenied
from hermes_state import SessionDB
from hermes_state_execution import ExecutionRecordStoreError
from plugins.dispatch_governor import DispatchGovernor


def _admitted(execution_id: str = "turn:api:1:0"):
    return ExecutionRecordV1.create({
        "execution_id": execution_id,
        "session_ref": "session:test",
        "session_generation": 0,
        "turn_ref": "phase:1",
        "call_ref": "ares-call:" + execution_id,
        "admission_ref": "context-dispatch:" + execution_id,
    })


def _observation(execution_id: str = "turn:api:1:0"):
    return {
        "execution_id": execution_id,
        "dispatch_intent_ref": "ares-dispatch-intent:one",
        "provider_request_digest": "c" * 64,
        "provider_request_bytes": 128,
        "resolved_route": "route:local",
        "request_body_schema": "ares.immutable-provider-body/v1+blake3",
    }


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "governor.db")
    try:
        value.create_execution_record(_admitted())
        value.transition_execution_record(
            "turn:api:1:0",
            ExecutionState.MATERIALIZED.value,
            materialization_ref="ares-materialization:" + "a" * 64,
            logical_request_digest="sha256:" + "b" * 64,
        )
        yield value
    finally:
        value.close()


def test_governor_writes_send_started_before_returning_ticket(db):
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    ticket = governor(payload=_observation(), dispatch_phase="physical_send")
    record = db.read_execution_record("turn:api:1:0")
    assert record.state == "send_started"
    value = record.to_dict()
    assert value["wire_request_digest"] == "blake3:" + "c" * 64
    assert value["wire_bytes"] == 128
    assert value["route_ref"] == "route:local"
    assert value["governor_qualified"] is True
    assert value["governor_mode"] == "enforce"
    assert value["dispatch_intent_refs"] == ["ares-dispatch-intent:one"]

    ticket.settle(
        outcome="ok",
        response={"response_digest": "sha256:" + "d" * 64, "http_status": 200},
    )
    settled = db.read_execution_record("turn:api:1:0")
    assert settled.state == "settled"
    assert settled.to_dict()["terminal_outcome"] == "ok"
    assert settled.to_dict()["response_digest"] == "sha256:" + "d" * 64
    assert settled.to_dict()["http_status"] == 200


def test_governor_denies_when_record_is_missing_in_enforce_mode():
    class MissingStore:
        def read_execution_record(self, execution_id):
            raise ExecutionRecordStoreError("EXECUTION_NOT_FOUND")

    governor = DispatchGovernor(store_factory=MissingStore, mode="enforce")
    with pytest.raises(EgressDispatchDenied) as error:
        governor(payload=_observation(), dispatch_phase="physical_send")
    assert error.value.code == "DISPATCH_RECORD_UNAVAILABLE"


def test_governor_observe_mode_does_not_deny_a_missing_record():
    class MissingStore:
        def read_execution_record(self, execution_id):
            raise ExecutionRecordStoreError("EXECUTION_NOT_FOUND")

    governor = DispatchGovernor(store_factory=MissingStore, mode="observe")
    assert governor(payload=_observation(), dispatch_phase="physical_send") is None


def test_governor_ignores_non_physical_phases_and_missing_execution_id(db):
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    assert governor(payload=_observation(), dispatch_phase="sdk_invocation") is None
    no_id = dict(_observation())
    no_id.pop("execution_id")
    assert governor(payload=no_id, dispatch_phase="physical_send") is None


def test_governor_does_not_transition_a_terminal_record(db):
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    db.transition_execution_record("turn:api:1:0", ExecutionState.DENIED.value)
    assert governor(payload=_observation(), dispatch_phase="physical_send") is None
    assert db.read_execution_record("turn:api:1:0").state == "denied"


def test_governor_enforce_rejects_unqualified_wire_observation(db):
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    observation = _observation()
    observation.pop("provider_request_digest")
    with pytest.raises(EgressDispatchDenied) as error:
        governor(payload=observation, dispatch_phase="physical_send")
    assert error.value.code == "DISPATCH_WIRE_UNQUALIFIED"


def test_governor_observe_records_no_send_started_without_wire_digest(db):
    governor = DispatchGovernor(store_factory=lambda: db, mode="observe")
    observation = _observation()
    observation.pop("provider_request_digest")
    ticket = governor(payload=observation, dispatch_phase="physical_send")
    assert ticket is not None
    assert db.read_execution_record("turn:api:1:0").state == "materialized"


def test_register_registers_one_llm_dispatch_callback():
    class Context:
        def __init__(self):
            self.registered = []

        def register_middleware(self, kind, callback):
            self.registered.append((kind, callback))

    from plugins.dispatch_governor import register

    context = Context()
    register(context)
    assert [kind for kind, _ in context.registered] == ["llm_dispatch"]
    assert isinstance(context.registered[0][1], DispatchGovernor)
