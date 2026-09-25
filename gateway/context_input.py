"""Gateway ingress binding to the existing SessionDB authentic-input owner."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import hashlib
import json
import uuid
import weakref
from types import SimpleNamespace

from hermes_state_continuity import ContextContinuationError
from hermes_state_inbox import ContextInputReceipt, _GATEWAY_ROUTE_FIELDS


@dataclass(frozen=True)
class GatewayContextInput:
    receipt: ContextInputReceipt
    session_key: str
    session_id: str
    authorize: object = None


def gateway_origin(runner, source, entry):
    adapter = runner._adapter_for_source(source)
    owner = None
    if adapter is (getattr(runner, "adapters", None) or {}).get(source.platform):
        owner = "transport:primary"
    else:
        for profile, adapters in (getattr(runner, "_profile_adapters", None) or {}).items():
            if adapter is adapters.get(source.platform):
                owner = profile
                break
    if adapter is None or owner is None:
        raise ContextContinuationError("GATEWAY_INPUT_TRANSPORT_UNAVAILABLE")
    fingerprint = runner._adapter_credential_fingerprint(adapter)
    route = {key: getattr(source, key) for key in _GATEWAY_ROUTE_FIELDS}
    route["platform"] = source.platform.value
    return {"schema": "SessionDBGatewayInputOriginV1", "session_key": entry.session_key,
        "route": route, "transport_profile": owner, "transport_fingerprint": fingerprint,
        "routed_profile": runner._profile_name_for_source(source),
        "cold_recoverable": bool(fingerprint and source.user_id
            and not source.role_authorized and not source.delivered_via_upstream_relay
            and getattr(adapter, "authorization_is_upstream", False) is not True)}


def input_authorizer(runner, db, session_key, *, live_adapter=None):
    """Reconstruct actors, never historical role/upstream authorization flags."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_cli.profiles import get_profile_dir
    from gateway.run import _profile_runtime_scope

    def authorize(session_id, receipts):
        if runner.session_store._db is not db:
            raise ContextContinuationError("GATEWAY_INPUT_PROFILE_CHANGED")
        entry = runner.session_store.lookup_by_session_key(session_key)
        if entry is None or entry.suspended:
            raise ContextContinuationError("GATEWAY_INPUT_ROUTE_CHANGED")
        if _control_pending(runner, SimpleNamespace(allow_gateway_control=True), session_key):
            raise ContextContinuationError("GATEWAY_INPUT_CONTROL_PENDING")
        for receipt in receipts:
            if db.read_context_input(entry.session_id, source=receipt.source, event_id=receipt.event_id) != receipt:
                raise ContextContinuationError("GATEWAY_INPUT_ROOT_CHANGED")
            origin = db.read_context_input_origin(session_id, receipt)
            if origin is None or origin["session_key"] != session_key:
                raise ContextContinuationError("GATEWAY_INPUT_PROVENANCE_REQUIRED")
            platform = Platform(origin["route"]["platform"])
            owner = origin["transport_profile"]
            if owner == "transport:primary":
                adapter = (getattr(runner, "adapters", None) or {}).get(platform)
            else:
                adapter = (getattr(runner, "_profile_adapters", None) or {}).get(owner, {}).get(platform)
            if adapter is None:
                raise ContextContinuationError("GATEWAY_INPUT_TRANSPORT_UNAVAILABLE")
            if runner._adapter_credential_fingerprint(adapter) != origin["transport_fingerprint"]:
                raise ContextContinuationError("GATEWAY_INPUT_TRANSPORT_CHANGED")
            if not origin["cold_recoverable"] and adapter is not live_adapter:
                raise ContextContinuationError("GATEWAY_INPUT_COLD_AUTH_UNAVAILABLE")
            source = SessionSource(**dict(origin["route"], platform=platform))
            source._transport_adapter_ref = weakref.ref(adapter)
            source._authorization_profile_home = runner._context_primary_home if owner == "transport:primary" else get_profile_dir(owner)
            # Re-evaluate explicit routing. A historical namespace never
            # overrides a changed route or a profile no longer being served.
            routed = runner._profile_name_for_source(source)
            if routed != origin["routed_profile"]:
                raise ContextContinuationError("GATEWAY_INPUT_ROUTE_CHANGED")
            if source.profile and getattr(runner.config, "multiplex_profiles", False):
                from gateway.run import _multiplex_profile_homes
                if source.profile not in {name for name, _ in _multiplex_profile_homes(runner.config)}:
                    raise ContextContinuationError("GATEWAY_INPUT_PROFILE_CHANGED")
            if runner._session_key_for_source(source) != session_key:
                raise ContextContinuationError("GATEWAY_INPUT_ROUTE_CHANGED")
            with _profile_runtime_scope(source._authorization_profile_home):
                if not runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
                    raise ContextContinuationError("GATEWAY_INPUT_ACTOR_REVOKED")
        return True
    return authorize


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
    adapter = runner._adapter_for_source(source)
    if (source.role_authorized or source.delivered_via_upstream_relay
            or getattr(adapter, "authorization_is_upstream", False) is True):
        # Those transports do not expose a current, reconstructible actor
        # authorization check. Refuse the selected durable route before
        # accepting bytes; never persist an old upstream/role trust flag.
        raise ContextContinuationError("GATEWAY_INPUT_AUTH_POLICY_UNQUALIFIED")
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
        source=source.platform.value, event_id=event_id, content=clean_text,
        gateway_origin=gateway_origin(runner, source, entry))
    event._context_input = GatewayContextInput(receipt, entry.session_key, entry.session_id,
        input_authorizer(runner, db, entry.session_key, live_adapter=runner._adapter_for_source(source)))
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
            "persist_user_input_receipt": bound.receipt,
            "persist_user_input_authorizer": bound.authorize}


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
