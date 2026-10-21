"""Contract tests for the closed execution-record state machine."""

from __future__ import annotations

import copy

import pytest

from ares_runtime.execution_record import (
    ExecutionRecordError,
    ExecutionRecordV1,
    ExecutionState,
    digest_record,
)


def base() -> dict:
    return {
        "execution_id": "turn:api:1:0",
        "session_ref": "session:test",
        "session_generation": 0,
        "turn_ref": "phase:1",
        "call_ref": "ares-call:test",
        "admission_ref": "context-dispatch:turn:api:1:0",
    }


def materialized() -> ExecutionRecordV1:
    return ExecutionRecordV1.create(base()).transition(
        ExecutionState.MATERIALIZED.value,
        materialization_ref="ares-materialization:" + "a" * 64,
        logical_request_digest="sha256:" + "b" * 64,
    )


def send_started() -> ExecutionRecordV1:
    return materialized().transition(
        ExecutionState.SEND_STARTED.value,
        wire_request_digest="sha256:" + "c" * 64,
        wire_bytes=128,
        transport_version="ares.immutable-provider-body/v1+blake3",
        route_ref="route:local",
        governor_qualified=True,
        governor_mode="enforce",
    )


def test_create_defaults_to_admitted_sequence_one():
    record = ExecutionRecordV1.create(base())
    assert record.state == "admitted"
    assert record.sequence == 1
    assert record.is_terminal is False
    assert record.to_dict()["governor_mode"] == "unavailable"
    assert record.to_dict()["dispatch_intent_refs"] == []


def test_parse_roundtrip_preserves_digest():
    record = ExecutionRecordV1.create(base())
    parsed = ExecutionRecordV1.parse(record.to_dict())
    assert parsed.canonical_bytes() == record.canonical_bytes()
    assert parsed.to_dict()["record_digest"] == record.to_dict()["record_digest"]


def test_rejects_unknown_field():
    value = ExecutionRecordV1.create(base()).to_dict()
    value["unexpected"] = 1
    with pytest.raises(ExecutionRecordError) as error:
        ExecutionRecordV1.parse(value)
    assert error.value.code == "INVALID_FIELDS"


def test_rejects_tampered_digest():
    value = ExecutionRecordV1.create(base()).to_dict()
    value["turn_ref"] = "phase:2"
    with pytest.raises(ExecutionRecordError) as error:
        ExecutionRecordV1.parse(value)
    assert error.value.code == "RECORD_DIGEST_MISMATCH"


def test_materialized_requires_refs():
    record = ExecutionRecordV1.create(base())
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(ExecutionState.MATERIALIZED.value)
    assert error.value.code == "MATERIALIZATION_REF_REQUIRED"


def test_send_started_requires_wire_digest():
    record = materialized()
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(ExecutionState.SEND_STARTED.value, wire_bytes=10)
    assert error.value.code == "WIRE_DIGEST_REQUIRED"


def test_settled_requires_terminal_outcome():
    record = send_started()
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(ExecutionState.SETTLED.value)
    assert error.value.code == "TERMINAL_OUTCOME_REQUIRED"


def test_full_happy_path_and_terminal_immutability():
    record = send_started().transition(
        ExecutionState.SETTLED.value,
        terminal_outcome="ok",
        response_digest="sha256:" + "d" * 64,
        http_status=200,
    )
    assert record.state == "settled"
    assert record.is_terminal is True
    assert record.to_dict()["terminal_outcome"] == "ok"
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(ExecutionState.SETTLED.value, terminal_outcome="ok")
    assert error.value.code == "TERMINAL_STATE_IMMUTABLE"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("admitted", "send_started"),
        ("admitted", "settled"),
        ("materialized", "settled"),
        ("send_started", "abandoned"),
        ("settled", "admitted"),
        ("denied", "materialized"),
    ],
)
def test_rejects_illegal_transitions(current, target):
    record = ExecutionRecordV1.create(base())
    if current in {"materialized", "send_started", "settled", "denied"}:
        record = materialized()
    if current in {"send_started", "settled"}:
        record = send_started()
    if current == "settled":
        record = send_started().transition(
            ExecutionState.SETTLED.value, terminal_outcome="ok", http_status=200
        )
    if current == "denied":
        record = ExecutionRecordV1.create(base()).transition(
            ExecutionState.DENIED.value
        )
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(target)
    expected = (
        "TERMINAL_STATE_IMMUTABLE"
        if current in {"settled", "denied"}
        else "ILLEGAL_TRANSITION"
    )
    assert error.value.code == expected


def test_denied_sets_terminal_outcome():
    record = ExecutionRecordV1.create(base()).transition(ExecutionState.DENIED.value)
    assert record.state == "denied"
    assert record.to_dict()["terminal_outcome"] == "denied"


def test_abandoned_sets_never_sent():
    record = ExecutionRecordV1.create(base()).transition(ExecutionState.ABANDONED.value)
    assert record.state == "abandoned"
    assert record.to_dict()["terminal_outcome"] == "never_sent"


def test_indeterminate_sets_unknown():
    record = send_started().transition(ExecutionState.INDETERMINATE.value)
    assert record.state == "indeterminate"
    assert record.to_dict()["terminal_outcome"] == "unknown"


def test_sequence_increments_on_each_transition():
    record = materialized().transition(
        ExecutionState.SEND_STARTED.value,
        wire_request_digest="sha256:" + "c" * 64,
        wire_bytes=1,
    )
    assert record.sequence == 3


def test_identity_fields_are_immutable():
    record = ExecutionRecordV1.create(base())
    with pytest.raises(ExecutionRecordError) as error:
        record.transition(ExecutionState.DENIED.value, execution_id="other")
    assert error.value.code == "FIELD_NOT_UPDATABLE"


def test_duplicate_dispatch_ref_is_rejected():
    value = ExecutionRecordV1.create(base()).to_dict()
    value["dispatch_intent_refs"] = [
        "ares-dispatch-intent:one",
        "ares-dispatch-intent:one",
    ]
    value.pop("record_digest")
    value["record_digest"] = digest_record(value)
    with pytest.raises(ExecutionRecordError) as error:
        ExecutionRecordV1.parse(value)
    assert error.value.code == "DUPLICATE_LIST_ITEM"


def test_missing_required_field_is_rejected():
    value = base()
    value.pop("turn_ref")
    with pytest.raises(ExecutionRecordError) as error:
        ExecutionRecordV1.create(value)
    assert error.value.code == "INVALID_TEXT"


def test_record_is_a_copy_not_a_shared_mapping():
    record = ExecutionRecordV1.create(base())
    value = record.to_dict()
    value["dispatch_intent_refs"].append("ares-dispatch-intent:mutated")
    assert record.to_dict()["dispatch_intent_refs"] == []


def test_digest_changes_when_wire_bytes_change():
    first = send_started()
    second = first.transition(
        ExecutionState.SETTLED.value,
        terminal_outcome="ok",
        http_status=200,
    )
    assert first.to_dict()["record_digest"] != second.to_dict()["record_digest"]


def test_canonical_bytes_are_stable_across_key_order():
    value = ExecutionRecordV1.create(base()).to_dict()
    reordered = dict(reversed(list(value.items())))
    assert (
        ExecutionRecordV1.parse(reordered).canonical_bytes()
        == ExecutionRecordV1.parse(copy.deepcopy(value)).canonical_bytes()
    )
