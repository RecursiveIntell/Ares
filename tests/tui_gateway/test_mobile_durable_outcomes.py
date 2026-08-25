from __future__ import annotations

import pytest

from hermes_state import SessionDB
from tui_gateway.request_outcomes import DurableRequestOutcomeLedger, RequestOutcomeState


def test_mobile_request_outcome_survives_session_db_restart(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    first = db.mobile_request_outcome_reserve(
        scope_key="host/default/session",
        method="session.title",
        idempotency_key="request-1",
        payload_digest="digest-a",
        now=10.0,
    )
    assert first["created"] is True
    assert first["state"] == "in_progress"
    db.mobile_request_outcome_finish(
        scope_key="host/default/session",
        method="session.title",
        idempotency_key="request-1",
        payload_digest="digest-a",
        state="completed",
        result_json='{"title":"safe"}',
        error_json=None,
        now=11.0,
    )
    db.close()

    reopened = SessionDB(db_path=db_path)
    replay = reopened.mobile_request_outcome_reserve(
        scope_key="host/default/session",
        method="session.title",
        idempotency_key="request-1",
        payload_digest="digest-a",
        now=12.0,
    )
    assert replay["created"] is False
    assert replay["state"] == "completed"
    assert replay["result_json"] == '{"title":"safe"}'
    reopened.close()


def test_mobile_request_outcome_rejects_same_key_with_different_payload(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.mobile_request_outcome_reserve(
        scope_key="host/default/session",
        method="session.title",
        idempotency_key="request-1",
        payload_digest="digest-a",
        now=10.0,
    )
    conflict = db.mobile_request_outcome_reserve(
        scope_key="host/default/session",
        method="session.title",
        idempotency_key="request-1",
        payload_digest="digest-b",
        now=11.0,
    )
    assert conflict["created"] is False
    assert conflict["conflict"] is True
    db.close()


def test_durable_ledger_projects_typed_terminal_outcome(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    ledger = DurableRequestOutcomeLedger(db)
    record = ledger.reserve(
        scope=("host", "default", "session"),
        method="session.title",
        key="request-1",
        payload_digest="digest-a",
        now=10.0,
    )
    completed = ledger.finish(
        record,
        state=RequestOutcomeState.COMPLETED,
        result={"title": "safe"},
        now=11.0,
    )
    assert completed.state is RequestOutcomeState.COMPLETED
    assert completed.result == {"title": "safe"}
    db.close()
