"""Stop fences receipt admission, not just eventual provider dispatch."""
import json

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_input import db, accept  # noqa: F401


def admit_input(db, receipt, operation):
    if operation == "wake":
        return db.reserve_context_input_wake("s", receipt=receipt)
    return db.begin_context_input_turn("s", receipt=receipt, turn_lease_holder="holder")


@pytest.mark.parametrize("operation", ["wake", "turn"])
@pytest.mark.parametrize("later_input", [False, True])
def test_stopped_receipt_cannot_be_readmitted_after_reopen(db, operation, later_input):
    stopped = accept(db)
    cut = db.record_context_stop("s")
    assert cut["input_sequence"] == stopped.sequence
    if later_input:
        accept(db, event="later", content="Explicit new instruction")
    reopened = SessionDB(db.db_path)
    try:
        before = reopened.read_context_input_work("s")
        with pytest.raises(ContextContinuationError, match="CONTEXT_INPUT_STOPPED"):
            admit_input(reopened, stopped, operation)
        # Retain the authentic input and exact phase evidence; rejection is not
        # permission to delete receipts or manufacture a response/projection.
        assert reopened.read_context_input_work("s") == before
        assert reopened.read_context_input("s", source="cli", event_id=stopped.event_id) == stopped
    finally:
        reopened.close()


@pytest.mark.parametrize("operation", ["wake", "turn"])
def test_explicit_post_stop_receipt_can_be_admitted_without_old_phase(db, operation):
    accept(db)
    cut = db.record_context_stop("s")
    new = accept(db, event="new", content="Explicit new instruction")
    assert new.sequence > cut["input_sequence"]
    admitted = admit_input(db, new, operation)
    assert admitted["through_sequence" if operation == "wake" else "last_sequence"] == new.sequence


@pytest.mark.parametrize("operation", ["wake", "turn"])
@pytest.mark.parametrize("sequence", [None, True, -1, "1"])
def test_invalid_native_stop_cut_cannot_admit(db, operation, sequence):
    receipt = accept(db)
    control = db.record_context_stop("s")
    control["input_sequence"] = sequence
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value=? WHERE key=?",
        (json.dumps(control), "context-control:" + receipt.conversation_root)))
    with pytest.raises(ContextContinuationError, match="CONTEXT_INPUT_STOP_UNCONFIRMED"):
        admit_input(db, receipt, operation)
    with pytest.raises(ContextContinuationError, match="CONTEXT_INPUT_STOP_UNCONFIRMED"):
        db.read_context_input_work("s")
    # Eligibility discovery now validates the cut too; inspect the native
    # phase owner directly to prove the failed admission wrote no phase.
    with db._read_ctx() as conn:
        assert db._input_turn_on_conn(conn, receipt.conversation_root, receipt.profile_name) is None


@pytest.mark.parametrize("operation", ["wake", "turn"])
def test_legacy_stop_keeps_existing_dispatch_watermark_gate(db, operation):
    receipt = accept(db)
    control = db.record_context_stop("s")
    control["schema"] = "SessionDBContextControlV1"
    control.pop("input_sequence")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value=? WHERE key=?",
        (json.dumps(control), "context-control:" + receipt.conversation_root)))
    assert db.read_context_rebase_snapshot("s").dispatch_stopped
    admit_input(db, receipt, operation)
    # Construction is not dispatch; this change does not guess a receipt cut
    # from a legacy watermark, or lift that existing provider execution fence.
    assert db.read_context_rebase_snapshot("s").dispatch_stopped
