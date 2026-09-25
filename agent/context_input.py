"""Authentic input admission for the existing conversation-turn driver."""
from __future__ import annotations

import uuid

from hermes_state_continuity import ContextContinuationError


def accept_turn_input(agent, *, user_message, persist_user_message, timestamp,
                      display_kind, display_metadata, event_id, accepted_receipt=None):
    """Durable acceptance precedes waiting for another controller's lease.

    Transport IDs are opaque, root-scoped deduplication keys. Callers without
    one receive a per-invocation identity; that is not cross-delivery dedup.
    Synthetic producers keep their existing typed owners.
    """
    if accepted_receipt is not None:
        from hermes_state_inbox import ContextInputReceipt
        db = getattr(agent, "_session_db", None)
        if (type(accepted_receipt) is not ContextInputReceipt or display_kind
                or getattr(agent, "_persist_disabled", False)
                or not callable(getattr(type(db), "read_context_input", None))):
            raise ContextContinuationError("CONTEXT_INPUT_RECEIPT_INVALID")
        current = db.read_context_input(agent.session_id,
            source=accepted_receipt.source, event_id=accepted_receipt.event_id)
        if current != accepted_receipt or persist_user_message != current.content:
            raise ContextContinuationError("CONTEXT_INPUT_RECEIPT_MISMATCH")
        return current
    if display_kind or getattr(agent, "_persist_disabled", False):
        return None
    from ares_runtime.continuity.runtime import context_dispatch_required
    if not context_dispatch_required(agent):
        return None
    db = getattr(agent, "_session_db", None)
    if not callable(getattr(type(db), "accept_context_input", None)):
        raise ContextContinuationError("CONTEXT_INPUT_OWNER_UNAVAILABLE")
    content = user_message if persist_user_message is None else persist_user_message
    # Selected continuity routes currently attest stateless text only. Do not
    # pretend the transcript's lossy attachment projection is the input bytes.
    if type(content) is not str:
        raise ContextContinuationError("CONTEXT_INPUT_TEXT_ROUTE_REQUIRED")
    pending = getattr(agent, "_pending_cli_user_message", None)
    if timestamp is None and isinstance(pending, dict) and pending.get("content") == content:
        timestamp = pending.get("timestamp")
    receipt = db.accept_context_input(agent.session_id,
        source=str(getattr(agent, "platform", None) or "agent"),
        event_id=event_id if event_id is not None else str(uuid.uuid4()),
        content=content, timestamp=timestamp, display_metadata=display_metadata)
    # No automatic write retry: a lost acknowledgement leaves durable input
    # for the same stable ID to reconcile on its next delivery.
    return receipt


def project_turn_inputs(agent, receipt, *, holder, conversation_history):
    """Called only after the live tip and conversation lease are established."""
    result = agent._session_db.project_context_inputs_before(agent.session_id,
        receipt=receipt, turn_lease_holder=holder)
    if result["projection"] is not None:
        # Projection proves acceptance, not completion. Never execute a second
        # turn because the transport repeated an event after losing its ACK.
        raise ContextContinuationError("CONTEXT_INPUT_ALREADY_PROJECTED")
    if result["inserted"]:
        return agent._session_db.get_messages_as_conversation(agent.session_id,
            repair_alternation=False, include_row_ids=True)
    return conversation_history
