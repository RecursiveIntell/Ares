"""Durable execution-record store tests against the real SessionDB."""

from __future__ import annotations

import threading

import pytest

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from hermes_state import SessionDB
from hermes_state_execution import ExecutionRecordStoreError


def base(execution_id: str = "turn:api:1:0") -> ExecutionRecordV1:
    return ExecutionRecordV1.create({
        "execution_id": execution_id,
        "session_ref": "session:test",
        "session_generation": 0,
        "turn_ref": "phase:1",
        "call_ref": "ares-call:test",
        "admission_ref": "context-dispatch:" + execution_id,
    })


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield value
    finally:
        value.close()


def test_create_read_and_transition(db):
    created = db.create_execution_record(base())
    assert created.state == "admitted"
    assert db.read_execution_record("turn:api:1:0").state == "admitted"

    materialized = db.transition_execution_record(
        "turn:api:1:0",
        ExecutionState.MATERIALIZED.value,
        materialization_ref="ares-materialization:" + "a" * 64,
        logical_request_digest="sha256:" + "b" * 64,
    )
    assert materialized.state == "materialized"

    sent = db.transition_execution_record(
        "turn:api:1:0",
        ExecutionState.SEND_STARTED.value,
        wire_request_digest="sha256:" + "c" * 64,
        wire_bytes=128,
        governor_qualified=True,
        governor_mode="enforce",
    )
    assert sent.state == "send_started"

    settled = db.transition_execution_record(
        "turn:api:1:0",
        ExecutionState.SETTLED.value,
        terminal_outcome="ok",
        response_digest="sha256:" + "d" * 64,
        http_status=200,
    )
    assert settled.state == "settled"
    assert settled.sequence == 4


def test_duplicate_create_is_refused(db):
    db.create_execution_record(base())
    with pytest.raises(ExecutionRecordStoreError) as error:
        db.create_execution_record(base())
    assert error.value.code == "EXECUTION_ALREADY_EXISTS"


def test_terminal_transition_is_refused(db):
    db.create_execution_record(base())
    db.transition_execution_record("turn:api:1:0", ExecutionState.DENIED.value)
    with pytest.raises(ExecutionRecordStoreError) as error:
        db.transition_execution_record(
            "turn:api:1:0", ExecutionState.MATERIALIZED.value
        )
    assert error.value.code == "ALREADY_TERMINAL"


def test_restart_readback_keeps_head_and_events(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    try:
        first.create_execution_record(base())
        first.transition_execution_record("turn:api:1:0", ExecutionState.DENIED.value)
    finally:
        first.close()

    second = SessionDB(db_path=path)
    try:
        record = second.read_execution_record("turn:api:1:0")
        assert record.state == "denied"
        events = second.read_execution_events("turn:api:1:0")
        assert [event["record"]["state"] for event in events] == ["admitted", "denied"]
    finally:
        second.close()


def test_prune_keeps_head_and_bounded_events(db):
    db.create_execution_record(base())
    for state in (ExecutionState.DENIED.value,):
        db.transition_execution_record("turn:api:1:0", state)
    removed = db.prune_execution_events("turn:api:1:0", keep=1)
    assert removed == 1
    assert db.read_execution_record("turn:api:1:0").state == "denied"
    assert len(db.read_execution_events("turn:api:1:0")) == 1


def test_list_execution_ids(db):
    db.create_execution_record(base("turn:api:1:0"))
    db.create_execution_record(base("turn:api:2:0"))
    assert db.list_execution_ids() == ["turn:api:1:0", "turn:api:2:0"]


def test_two_writer_race_has_exactly_one_winner(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    second = SessionDB(db_path=path)
    try:
        first.create_execution_record(base())
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def attempt(database):
            barrier.wait(timeout=5)
            try:
                database.transition_execution_record(
                    "turn:api:1:0", ExecutionState.DENIED.value
                )
                outcomes.append("won")
            except ExecutionRecordStoreError as error:
                outcomes.append(error.code)

        threads = [
            threading.Thread(target=attempt, args=(db,)) for db in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(outcomes) == ["ALREADY_TRANSITIONED", "won"]
        assert first.read_execution_record("turn:api:1:0").state == "denied"
    finally:
        first.close()
        second.close()
