"""Authentic input admission for the existing conversation-turn driver."""
from __future__ import annotations

import uuid
from contextlib import contextmanager

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
    # Gateway provenance is reauthorized for the entire range. A direct
    # receipt handoff without that current-policy owner must fail closed.
    work = agent._session_db.read_context_input_work(agent.session_id)
    by_sequence = {r.sequence: r for r in (*work["receipts"], *agent._session_db.read_pending_context_inputs(agent.session_id))}
    receipts = tuple(by_sequence[n] for n in sorted(by_sequence) if n <= receipt.sequence)
    authorize = getattr(agent, "_context_input_authorizer", None)
    if any(agent._session_db.read_context_input_origin(agent.session_id, r) is not None for r in receipts):
        if not callable(authorize) or authorize(agent.session_id, receipts) is not True:
            raise ContextContinuationError("CONTEXT_INPUT_AUTHORIZATION_REQUIRED")
    agent._context_input_phase = agent._session_db.begin_context_input_turn(agent.session_id,
        receipt=receipt, turn_lease_holder=holder)
    result = agent._session_db.project_context_inputs_before(agent.session_id,
        receipt=receipt, turn_lease_holder=holder)
    if result["projection"] is not None:
        # Only a native phase with no admitted dispatch reaches this point.
        # Reuse the exact durable occurrence and its stored API sidecar.
        history = agent._session_db.get_messages_as_conversation(agent.session_id,
            repair_alternation=False, include_row_ids=True)
        if not history or history[-1].get("_row_id") != result["projection"]["row_id"]:
            raise ContextContinuationError("CONTEXT_INPUT_PROJECTED_TAIL_CHANGED")
        agent._context_input_existing_message = history[-1]
        return history[:-1]
    if result["inserted"]:
        return agent._session_db.get_messages_as_conversation(agent.session_id,
            repair_alternation=False, include_row_ids=True)
    return conversation_history


def validate_turn_input_authority(agent):
    if getattr(agent, "_context_input_phase", None) is None:
        return
    work = agent._session_db.read_context_input_work(agent.session_id)
    authorize = getattr(agent, "_context_input_authorizer", None)
    if callable(authorize) and authorize(agent.session_id, work["receipts"]) is not True:
        raise ContextContinuationError("CONTEXT_INPUT_AUTHORIZATION_REQUIRED")


def complete_turn_inputs(agent, messages):
    if getattr(agent, "_context_input_phase", None) is None:
        return
    validate_turn_input_authority(agent)
    final = messages[-1] if messages else {}
    attempt = (getattr(agent, "_context_response_admission", None) or {}).get("attempt_id")
    try:
        confirmed = agent._session_db.confirm_context_input_response(agent.session_id,
            turn_lease_holder=agent._active_session_turn_lease_holder,
            phase_id=agent._context_input_phase["phase_id"], final_content=final.get("content"), final_attempt_id=attempt)
    except ContextContinuationError as exc:
        if exc.code != "CONTEXT_INPUT_COMPLETION_UNCONFIRMED":
            raise
        # Some existing loop paths already persisted the terminal row. Bind
        # that exact row instead of appending a second response.
        confirmed = agent._session_db.complete_context_input_turn(agent.session_id,
            turn_lease_holder=agent._active_session_turn_lease_holder,
            final_row_id=final.get("_row_id"), final_content=final.get("content"), final_attempt_id=attempt)
    if final.get("_row_id") is None:
        # Publication committed but its caller never received row IDs. Reload
        # the exact native batch; a later close flush must not duplicate it.
        messages[:] = agent._session_db.get_messages_as_conversation(agent.session_id,
            repair_alternation=False, include_row_ids=True)
    return confirmed


@contextmanager
def turn_input_response_scope(agent, messages, *, successful):
    phase = getattr(agent, "_context_input_phase", None)
    if not successful or phase is None:
        yield
        return
    from hermes_state_input_turns import input_response_publication_scope
    validate_turn_input_authority(agent)
    final = messages[-1] if messages else {}
    attempt = (getattr(agent, "_context_response_admission", None) or {}).get("attempt_id")
    with input_response_publication_scope(agent._session_db, agent._active_session_turn_lease_holder,
            phase["phase_id"], attempt, final.get("content")):
        yield
    complete_turn_inputs(agent, messages)
