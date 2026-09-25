"""Gateway ingress binding to the existing SessionDB authentic-input owner."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import hashlib
import json
import uuid

from hermes_state_continuity import ContextContinuationError
from hermes_state_inbox import ContextInputReceipt


@dataclass(frozen=True)
class GatewayContextInput:
    receipt: ContextInputReceipt
    session_key: str
    session_id: str


def accepted_input(event):
    value = getattr(event, "_context_input", None)
    if value is not None and type(value) is not GatewayContextInput:
        raise ContextContinuationError("GATEWAY_INPUT_BINDING_INVALID")
    return value


def _control_pending(runner, event, key):
    if not event.allow_gateway_control:
        return False
    from tools import clarify_gateway, slash_confirm
    from tools.approval import has_blocking_approval
    state = runner._peek_session_state(key)
    return bool(
        state is not None and state.persistent.update_prompt_pending
        or clarify_gateway.get_pending_for_session(key, include_choice_prompts=True) is not None
        or slash_confirm.get_pending(key)
        or has_blocking_approval(key)
    )


async def accept_gateway_input(runner, event, *, controls_resolved=False):
    """Await native acceptance before ordinary adapter queue/spawn/ACK.

    The runner calls this in the same profile and transport authorization scope
    as its message handler. Native controls retain their existing owners. Wire
    metadata is never interpreted as a receipt or an authorization decision.
    """
    from gateway.platforms.base import MessageType
    from gateway.run import _is_slack_ignored_channel, _load_gateway_config
    from gateway.config import Platform
    from utils import is_truthy_value

    bound = accepted_input(event)
    if bound is not None:
        return True
    if (event.internal or not event.allow_gateway_control or event.message_type != MessageType.TEXT
            or event.media_urls or event.media_types or type(event.text) is not str):
        return True
    if event.get_command():
        return True
    config = _load_gateway_config() or {}
    enabled = is_truthy_value((config.get("compression") or {}).get("context_rebase_enabled"), default=False)
    source = event.source
    if getattr(source, "profile_route_rejected", False) is True:
        return False
    if source.platform == Platform.SLACK and _is_slack_ignored_channel(getattr(runner, "config", None), source.chat_id):
        return False
    key = runner._session_key_for_source(source)
    if not enabled:
        store = getattr(runner, "session_store", None)
        if not callable(getattr(type(store), "lookup_by_session_key", None)):
            return True
        entry = await runner.async_session_store.lookup_by_session_key(key)
        if entry is None:
            return True
        db = store._db
        check = getattr(type(db), "context_dispatch_required_for_session", None)
        if not callable(check) or not await asyncio.to_thread(check, db, entry.session_id):
            return True
    if not runner._is_user_authorized_for_source(source):
        # Preserve the ordinary handler's pairing/plugin behavior, without
        # admitting unauthorized data into the native conversation inbox.
        return True
    if not controls_resolved and _control_pending(runner, event, key):
        return True
    clean_text = event._context_original_text
    if clean_text is None:
        clean_text = event.text
        event._context_original_text = clean_text
    if not getattr(event, "_context_pre_dispatch_applied", False):
        source_identity = copy.deepcopy(vars(source))
        if not runner._apply_pre_gateway_dispatch(event):
            return False
        if event.source is not source or vars(source) != source_identity:
            raise ContextContinuationError("GATEWAY_INPUT_ROUTE_CHANGED")
        event._context_pre_dispatch_applied = True
    if event.get_command():
        return True
    resolved = await runner._resolve_message_session(event, source)
    if resolved is None:
        raise ContextContinuationError("GATEWAY_INPUT_SESSION_UNAVAILABLE")
    entry, source = resolved
    db = runner.session_store._db
    if not callable(getattr(type(db), "accept_context_input", None)):
        raise ContextContinuationError("CONTEXT_INPUT_OWNER_UNAVAILABLE")
    event_id = getattr(event, "_context_event_id", None)
    if event_id is None:
        native_id = event.message_id
        if native_id is None and event.platform_update_id is not None:
            native_id = str(event.platform_update_id)
        if native_id is None:
            native_id = "invocation:" + uuid.uuid4().hex
        identity = [source.platform.value, source.scope_id, source.chat_id,
                    source.thread_id, source.user_id, entry.session_key, str(native_id)]
        event_id = "gateway:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        event._context_event_id = event_id
    # Adapter receive-time defaults are unstable under redelivery. The native
    # owner assigns first-acceptance time; transport IDs bind the original text.
    receipt = await asyncio.to_thread(db.accept_context_input, entry.session_id,
        source=source.platform.value, event_id=event_id, content=clean_text)
    event._context_input = GatewayContextInput(receipt, entry.session_key, entry.session_id)
    return True


async def validate_gateway_input(runner, event, entry):
    """Refuse route/reset drift before history preparation or agent creation."""
    bound = accepted_input(event)
    if bound is None:
        return
    if entry.session_key != bound.session_key:
        raise ContextContinuationError("GATEWAY_INPUT_ROUTE_CHANGED")
    db = runner.session_store._db
    if not callable(getattr(type(db), "read_context_input", None)):
        raise ContextContinuationError("CONTEXT_INPUT_OWNER_UNAVAILABLE")
    current = await asyncio.to_thread(db.read_context_input, entry.session_id,
        source=bound.receipt.source, event_id=bound.receipt.event_id)
    if current != bound.receipt:
        raise ContextContinuationError("GATEWAY_INPUT_ROOT_CHANGED")


def turn_input_kwargs(bound):
    if bound is None:
        return {}
    if type(bound) is not GatewayContextInput:
        raise ContextContinuationError("GATEWAY_INPUT_BINDING_INVALID")
    return {"persist_user_message": bound.receipt.content,
            "persist_user_timestamp": bound.receipt.timestamp,
            "persist_user_display_metadata": bound.receipt.display_metadata,
            "persist_user_event_id": bound.receipt.event_id,
            "persist_user_input_receipt": bound.receipt}


def turn_input_api_content(bound, content):
    """Keep the actual input present when a plugin supplies derived context."""
    if bound is None:
        return content
    if type(bound) is not GatewayContextInput or type(content) is not str:
        raise ContextContinuationError("GATEWAY_INPUT_TEXT_ROUTE_REQUIRED")
    original = bound.receipt.content
    if original in content:
        return content
    return original + "\n\n[Gateway context]\n" + content
