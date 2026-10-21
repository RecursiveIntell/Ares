"""Recovery distinguishes a provably-never-sent attempt from an unknown send."""

from __future__ import annotations

import json

import pytest

from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_input import db, accept, project  # noqa: F401


def _begin(db, receipt):
    return db.begin_context_input_turn("s", receipt=receipt, turn_lease_holder="holder")


def _admit(db):
    return db.admit_context_dispatch(
        "s",
        turn_lease_holder="holder",
        attempt_id="a",
        expected_snapshot_digest=db.read_context_rebase_snapshot("s").digest,
        payload_digest="sha256:" + "a" * 64,
        route_ref="test",
    )


def _seed_attempt(db):
    receipt = accept(db)
    _begin(db, receipt)
    project(db, receipt)
    _admit(db)
    return receipt


def test_enforce_abandons_never_sent_attempt_and_allows_new_turn(db):
    receipt = _seed_attempt(db)
    assert db.abandon_unsent_context_dispatch("s", governor_mode="enforce") is True
    record = db.read_execution_record("a")
    assert record.state == "abandoned"
    assert record.to_dict()["terminal_outcome"] == "never_sent"
    result = json.loads(db.get_meta("context-dispatch-result:a"))
    assert result["disposition"] == "abandoned"
    assert result["reason"] == "never_sent"

    db.release_context_input_turn("s", turn_lease_holder="holder")
    recovered = _begin(db, receipt)
    assert recovered["attempts"] == 2
    assert recovered["state"] == "active"


def test_observe_mode_does_not_relax_recovery(db):
    receipt = _seed_attempt(db)
    assert db.abandon_unsent_context_dispatch("s", governor_mode="observe") is False
    assert db.read_execution_record("a").state == "admitted"
    db.release_context_input_turn("s", turn_lease_holder="holder")
    with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
        _begin(db, receipt)


def test_send_started_is_never_abandoned(db):
    receipt = _seed_attempt(db)
    db.transition_execution_record(
        "a",
        "materialized",
        materialization_ref="ares-materialization:" + "a" * 64,
        logical_request_digest="sha256:" + "b" * 64,
    )
    db.transition_execution_record(
        "a",
        "send_started",
        wire_request_digest="sha256:" + "c" * 64,
        wire_bytes=1,
        governor_qualified=True,
        governor_mode="enforce",
    )
    assert db.abandon_unsent_context_dispatch("s", governor_mode="enforce") is False
    assert db.read_execution_record("a").state == "send_started"
    db.release_context_input_turn("s", turn_lease_holder="holder")
    with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
        _begin(db, receipt)
