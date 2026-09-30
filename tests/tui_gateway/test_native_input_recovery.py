"""P13 native acceptance witnesses, not legacy in-memory queued-prompt recovery.

These tests intentionally do not certify attachment recovery: the selected TUI
admission path currently rejects attached_images before SessionDB acceptance.
"""
from __future__ import annotations

from dataclasses import asdict
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from tui_gateway import server


def test_accepted_b_survives_accepting_process_loss_with_identity_and_fifo(tmp_path):
    """A committed ACK is recoverable without its submitting process or queue."""
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("conversation", source="cli")
    db.close()
    # This child is the only owner of the accepted input. os._exit models
    # abrupt parent/host loss after commit, without cleanup or queue draining.
    child = subprocess.run(
        [sys.executable, "-c", """
import os, sys
from pathlib import Path
from hermes_state import SessionDB
owner = SessionDB(Path(sys.argv[1]))
a = owner.accept_context_input('conversation', source='cli', event_id='A', content='First')
b = owner.accept_context_input('conversation', source='cli', event_id='B', content='Second')
assert b.sequence == a.sequence + 1
os._exit(0)
""", str(path)], capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 0, child.stderr

    recovered = SessionDB(path)
    try:
        a = recovered.read_context_input("conversation", source="cli", event_id="A")
        b = recovered.read_context_input("conversation", source="cli", event_id="B")
        assert a is not None and b is not None
        assert b.sequence == a.sequence + 1
        assert recovered.read_pending_context_inputs("conversation") == (a, b)
        assert recovered.read_context_input_work("conversation")["receipts"] == (a, b)
        # Lost ACK/redelivery must not allocate another occurrence or mutate B.
        again = recovered.accept_context_input("conversation", source="cli", event_id="B", content="Second")
        assert asdict(again) == asdict(b)
        assert recovered.read_pending_context_inputs("conversation") == (a, b)
        with pytest.raises(ContextContinuationError, match="CONTEXT_INPUT_ID_COLLISION"):
            recovered.accept_context_input("conversation", source="cli", event_id="B", content="changed")
        assert recovered.read_pending_context_inputs("conversation") == (a, b)
    finally:
        recovered.close()


def test_dispatched_b_is_uncertain_not_replayed_after_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("conversation", source="cli")
    db.append_message("conversation", "user", "Earlier request")
    b = db.accept_context_input("conversation", source="cli", event_id="B", content="Second")
    assert db.try_acquire_session_turn_lease("conversation", "holder", ttl_seconds=300)
    db.begin_context_input_turn("conversation", receipt=b, turn_lease_holder="holder")
    db.append_messages_batch("conversation", [{"role": "user", "content": b.content,
        "timestamp": b.timestamp, "_context_input": {"conversation_root": b.conversation_root,
        "sequence": b.sequence, "payload_digest": b.payload_digest}}], turn_lease_holder="holder")
    db.admit_context_dispatch("conversation", turn_lease_holder="holder", attempt_id="attempt-B",
        expected_snapshot_digest=db.read_context_rebase_snapshot("conversation").digest,
        payload_digest="sha256:" + "a" * 64, route_ref="test")
    db.release_context_input_turn("conversation", turn_lease_holder="holder")
    db.close()

    recovered = SessionDB(path)
    try:
        work = recovered.read_context_input_work("conversation")
        assert work["receipts"] == (b,)
        assert work["phase"]["state"] == "uncertain"
        assert work["phase"]["dispatch_attempts"] == ["attempt-B"]
        with pytest.raises(ContextContinuationError, match="EXECUTION_UNCERTAIN"):
            recovered.reserve_context_input_wake("conversation", receipt=b)
        assert recovered.read_context_input_work("conversation")["phase"]["dispatch_attempts"] == ["attempt-B"]
    finally:
        recovered.close()


def test_selected_tui_attachment_is_blocked_before_text_only_ack(tmp_path):
    """C30 boundary: an attached submission cannot silently become text only."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation", source="cli")
    image = tmp_path / "photo.png"
    image.write_bytes(b"fixture attachment")
    agent = SimpleNamespace(_session_db=db, session_id="conversation", context_rebase_enabled=True)
    session = {"agent": agent, "session_key": "conversation", "attached_images": [str(image)]}
    try:
        with pytest.raises(ContextContinuationError, match="CONTEXT_INPUT_TEXT_ROUTE_REQUIRED"):
            server._accept_tui_context_input(session, "Look at the photo", event_id="B")
        assert db.read_context_input("conversation", source="cli", event_id="B") is None
        assert db.read_pending_context_inputs("conversation") == ()
    finally:
        db.close()
