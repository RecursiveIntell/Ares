"""Turn-start automatic context rebase orchestration.

This module joins the already-existing continuity primitives at the safest
automatic boundary: after the current user input is durable, but before this
turn admits a provider request or tool effect.  It never treats a compiled
brief or a pressure recommendation as execution authority.

A published child remains committed_pending_activation until every local owner
that this path can prove has been reconciled.  Any post-publication ambiguity
is fail-closed and recoverable from the durable transition.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

from agent.model_metadata import estimate_request_tokens_rough
from hermes_cli.goals import GoalState

from .budget import stateless_payload_token_upper_bound
from .compiler import Mode
from .live import LiveContinuationError, build_live_candidate


class AutomaticRebaseError(RuntimeError):
    """Stable refusal code only; never carries prompt/source payloads."""


class ContextDispatchError(AutomaticRebaseError):
    """A final request was not admitted; never route through provider retry."""


def context_dispatch_required(agent):
    """Disabling publication must preserve readers and existing dispatch seals."""
    if getattr(agent, "context_rebase_enabled", False):
        return True
    db = getattr(agent, "_session_db", None)
    check = getattr(type(db), "context_dispatch_required_for_session", None)
    if not callable(check) or not getattr(agent, "session_id", None):
        return False
    try:
        return check(db, agent.session_id)
    except Exception as exc:
        raise ContextDispatchError(getattr(exc, "code", "CONTEXT_DISPATCH_OWNER_UNAVAILABLE")) from None


def prepare_context_dispatch(agent, messages, conversation_history):
    """Bind the source before request middleware may run or accept new input."""
    agent._context_response_admission = None
    agent._context_tool_control_failure = None
    if not context_dispatch_required(agent):
        return None
    db = getattr(agent, "_session_db", None)
    if db is None:
        raise ContextDispatchError("CONTEXT_DISPATCH_OWNER_UNAVAILABLE")
    if getattr(agent, "_context_stop_unacknowledged", False):
        raise ContextDispatchError("CONTEXT_DISPATCH_STOP_UNACKNOWLEDGED")
    if agent._flush_messages_to_session_db(messages, conversation_history=conversation_history) is False:
        raise ContextDispatchError("CONTEXT_DISPATCH_INPUT_NOT_DURABLE")
    try:
        db.assert_context_rebase_ready_for_turn(agent.session_id)
        snapshot = db.read_context_rebase_snapshot(agent.session_id)
        # Do not bless a transcript that was loaded before a durable human
        # correction. Check exact current human content before deriving any
        # provider roles, merging messages or running request middleware.
        from agent.context_compressor import user_originated_turn_view

        def identity(view):
            return view.get("timestamp"), view.get("content")

        materialized_users = [identity(view) for message in messages
                              if (view := user_originated_turn_view(message)) is not None]
        current_users = [identity(user_originated_turn_view({"role": "user", **source}))
                         for source in snapshot.current_users if source.get("authentic_user")]
        if current_users and materialized_users[-len(current_users):] != current_users:
            raise ContextDispatchError("CONTEXT_DISPATCH_STALE_MATERIALIZATION")
        return snapshot
    except ContextDispatchError:
        raise
    except Exception as exc:
        raise ContextDispatchError(getattr(exc, "code", "CONTEXT_DISPATCH_SOURCE_UNAVAILABLE")) from None


def context_dispatch_route_identity(agent):
    """In-process route binding; credentials never enter persisted receipts."""
    return tuple(getattr(agent, name, None) for name in ("provider", "api_mode", "model", "base_url")) + (
        id(getattr(agent, "client", None)),
        getattr(getattr(agent, "context_compressor", None), "context_length", None),
    )


def context_dispatch_payload_digest(payload):
    from .budget import BudgetError, final_request_upper_bound

    try:
        return final_request_upper_bound(route_ref="materialization", payload=payload).payload_digest
    except BudgetError as exc:
        raise ContextDispatchError(str(exc)) from None


def admit_final_context_dispatch(agent, snapshot, payload, *, attempt_id,
                                 materialization_digest=None, route_identity=None):
    """Count and seal the final text request at the real dispatch boundary."""
    if snapshot is None:
        return None
    from .budget import BudgetError, RouteBudget, final_request_upper_bound

    mode = str(getattr(agent, "api_mode", ""))
    if route_identity is not None and route_identity != context_dispatch_route_identity(agent):
        raise ContextDispatchError("CONTEXT_DISPATCH_ROUTE_CHANGED")
    if payload.get("model", payload.get("modelId")) != getattr(agent, "model", None):
        raise ContextDispatchError("CONTEXT_DISPATCH_ROUTE_CHANGED")
    extra = payload.get("extra_body")
    # The SDK merges extra_body into the actual wire body after this call.
    # Permit provider extensions, but never hidden overrides of source,
    # route, output reserve, tools or retained provider state.
    protected = {"model", "modelId", "messages", "input", "system", "instructions", "tools",
                 "max_tokens", "max_completion_tokens", "max_output_tokens", "inferenceConfig",
                 "previous_response_id", "conversation"}
    if extra is not None and (type(extra) is not dict or protected.intersection(extra)):
        raise ContextDispatchError("CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED")
    if mode not in {"chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse"}:
        raise ContextDispatchError("PROVIDER_CONTEXT_RESET_UNQUALIFIED")
    if str(getattr(agent, "base_url", "")).lower().startswith(("acp://", "acp+tcp://")):
        raise ContextDispatchError("PROVIDER_CONTEXT_RESET_UNQUALIFIED")
    route = ":".join(str(getattr(agent, name, "unknown") or "unknown").replace(" ", "_")
                     for name in ("provider", "api_mode", "model"))
    output = payload.get("max_output_tokens", payload.get("max_completion_tokens", payload.get("max_tokens")))
    if output is None:
        output = getattr(agent, "max_tokens", None)
    if type(output) is not int or output <= 0:
        raise ContextDispatchError("CONTEXT_DISPATCH_OUTPUT_BUDGET_UNQUALIFIED")
    context_limit = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    try:
        count = final_request_upper_bound(route_ref=route, payload=payload)
        budget = RouteBudget(route, None, context_limit, output, 0)
        if count.tokens > budget.usable_input:
            raise ContextDispatchError("CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE")
        if materialization_digest is not None and count.payload_digest != materialization_digest:
            raise ContextDispatchError("CONTEXT_DISPATCH_MATERIALIZATION_CHANGED")
    except BudgetError as exc:
        raise ContextDispatchError(str(exc)) from None
    lock = getattr(agent, "_pending_redirect_lock", None)
    with lock if lock is not None else nullcontext():
        if getattr(agent, "_interrupt_requested", False) or getattr(agent, "_pending_redirect", None):
            raise ContextDispatchError("CONTEXT_DISPATCH_INTERRUPTED")
        try:
            return agent._session_db.admit_context_dispatch(
                agent.session_id,
                turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
                attempt_id=attempt_id, expected_snapshot_digest=snapshot.digest,
                payload_digest=count.payload_digest, route_ref=route,
            )
        except Exception as exc:
            raise ContextDispatchError(getattr(exc, "code", "CONTEXT_DISPATCH_OWNER_UNAVAILABLE")) from None


def settle_final_context_dispatch(agent, admission):
    if admission is None:
        return
    try:
        agent._session_db.settle_context_dispatch_response(
            admission["attempt_id"],
            turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
        )
        agent._context_response_admission = admission
    except Exception as exc:
        raise ContextDispatchError(getattr(exc, "code", "CONTEXT_DISPATCH_SETTLEMENT_UNKNOWN")) from None


_tool_control = ContextVar("context_continuity_tool_control", default=None)


@contextmanager
def context_tool_control_scope(agent):
    """Bind this response across existing tool and execute_code worker threads."""
    binding = None
    if context_dispatch_required(agent):
        admission = getattr(agent, "_context_response_admission", None)
        binding = (agent, getattr(agent, "_session_db", None), agent.session_id,
                   getattr(agent, "_active_session_turn_lease_holder", None),
                   admission.get("attempt_id") if isinstance(admission, dict) else None)
    token = _tool_control.set(binding)
    try:
        yield
    finally:
        _tool_control.reset(token)


def assert_context_tool_control_current(*, session_id=None):
    """Check local control immediately before the existing tool/effect owner.

    This is a precondition, not a replacement permit or proof that an
    already-admitted external operation has been cancelled.
    """
    binding = _tool_control.get()
    if binding is None:
        return
    agent, db, bound_session, holder, attempt_id = binding
    try:
        if (getattr(agent, "_interrupt_requested", False)
                or getattr(agent, "_pending_redirect", None)
                or getattr(agent, "_context_stop_unacknowledged", False)):
            raise ContextDispatchError("CONTEXT_DISPATCH_INTERRUPTED")
        if not attempt_id or (session_id and session_id != bound_session):
            raise ContextDispatchError("CONTEXT_TOOL_RESPONSE_NOT_ADMITTED")
        db.assert_context_dispatch_control_current(
            attempt_id, session_id=bound_session, turn_lease_holder=holder,
        )
    except Exception as exc:
        code = str(exc) if isinstance(exc, ContextDispatchError) else getattr(exc, "code", "CONTEXT_DISPATCH_OWNER_UNAVAILABLE")
        agent._context_tool_control_failure = code
        raise ContextDispatchError(code) from None


class ContextDispatchStreamBuffer:
    """Keep display, TTS and plugin response consumption behind settlement.

    The provider still streams for cancellation and transport health. A turn
    has one writer; wrappers are restored before any accepted event is sent.
    The queue is bounded and discarded on uncertainty, never replayed into a
    later attempt.
    """

    def __init__(self, agent, admission):
        self.agent, self.admission = agent, admission
        self.events, self.originals = [], {}
        self.bytes, self.overflow = 0, False

    def __enter__(self):
        if self.admission is None:
            return self
        self.previous_buffered = getattr(self.agent, "_context_stream_delivery_buffered", False)
        self.agent._context_stream_delivery_buffered = True
        for name in ("_fire_stream_delta", "_fire_reasoning_delta", "_fire_tool_gen_started",
                     "_fire_streamed_codex_commentary", "interim_assistant_callback",
                     "_emit_stream_start", "_emit_stream_end", "_emit_stream_drop",
                     "_record_streamed_assistant_text", "stream_delta_callback", "_stream_callback",
                     "reasoning_callback", "tool_gen_callback"):
            callback = getattr(self.agent, name, None)
            if not callable(callback):
                continue
            self.originals[name] = (name in vars(self.agent), callback)

            def buffer(*args, _callback=callback, **kwargs):
                self.bytes += sum(len(value.encode("utf-8")) for value in (*args, *kwargs.values()) if isinstance(value, str))
                if self.bytes > 8 * 1024 * 1024 or len(self.events) >= 65536:
                    self.overflow = True
                    return
                self.events.append((_callback, args, kwargs))

            setattr(self.agent, name, buffer)
        return self

    def __exit__(self, *_):
        if self.admission is not None:
            self.agent._context_stream_delivery_buffered = self.previous_buffered
        for name, (instance_owned, callback) in self.originals.items():
            if instance_owned:
                setattr(self.agent, name, callback)
            else:
                delattr(self.agent, name)

    def deliver(self):
        if self.overflow:
            raise ContextDispatchError("CONTEXT_DISPATCH_STREAM_BUFFER_EXHAUSTED")
        for callback, args, kwargs in self.events:
            try:
                self.agent._session_db.assert_context_dispatch_current(
                    self.admission["attempt_id"],
                    turn_lease_holder=getattr(self.agent, "_active_session_turn_lease_holder", None),
                )
            except Exception as exc:
                raise ContextDispatchError(getattr(exc, "code", "CONTEXT_DISPATCH_SETTLEMENT_UNKNOWN")) from None
            try:
                callback(*args, **kwargs)
            except Exception:
                logging.getLogger(__name__).debug("Buffered stream observer failed", exc_info=True)
        self.events.clear()


class AutomaticRebaseStatus(str, Enum):
    SKIPPED = "skipped"
    READY = "ready"
    BLOCKED = "blocked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True)
class AutomaticRebaseResult:
    status: AutomaticRebaseStatus
    reason: str
    session_id: str
    messages: tuple[dict[str, Any], ...] = ()
    system_prompt: str | None = None
    transition_id: str | None = None
    before_tokens: int | None = None
    after_tokens: int | None = None
    qualified_after_tokens: int | None = None

    @property
    def ready(self) -> bool:
        return self.status is AutomaticRebaseStatus.READY


def context_rebase_failure_result(
    result: AutomaticRebaseResult, messages: list[dict[str, Any]], *, api_calls: int,
) -> dict[str, Any]:
    """Project a stopped continuation without reclassifying it as completion.

    All BLOCKED outcomes stop this pressure-recovery path. Falling through to
    ordinary admission would retry the exhausted request or bypass a failed
    source/owner/effect check. A later turn may reconsider current owner state.
    """
    if result.status not in {
        AutomaticRebaseStatus.BLOCKED, AutomaticRebaseStatus.RECONCILIATION_REQUIRED,
    }:
        raise AutomaticRebaseError("CONTEXT_REBASE_NOT_A_FAILURE")
    pending = result.status is AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    message = (
        "Context rebase committed but owner reconciliation is required before ordinary execution can continue."
        if pending else
        f"Context continuation is blocked ({result.reason}); ordinary model/tool execution is stopped."
    )
    return {
        "final_response": message,
        "messages": messages,
        "completed": False,
        "api_calls": api_calls,
        "error": result.reason,
        "partial": True,
        "failed": True,
        "context_rebase_reconciliation_required": pending,
        "context_rebase_blocked": not pending,
    }


def _merged_system_prompt(base: str | None, addendum: str) -> str:
    base = str(base or "").rstrip()
    addendum = str(addendum or "").strip()
    if not addendum:
        return base
    if addendum in base:
        return base
    return f"{base}\n\n{addendum}".strip()


def _stable_ids(continuation_digest: str) -> tuple[str, str]:
    if not continuation_digest.startswith("sha256:"):
        raise AutomaticRebaseError("INVALID_CONTINUATION_DIGEST")
    suffix = continuation_digest[7:39]
    transition_id = f"context-rebase:{suffix}"
    child_session_id = f"ctx-{suffix}"
    return transition_id, child_session_id


def _active_goal_required(db: Any, session_id: str) -> bool:
    try:
        raw = db.get_meta(f"goal:{session_id}")
    except Exception:
        raise AutomaticRebaseError("GOAL_OWNER_UNAVAILABLE") from None
    if raw is None:
        return False
    try:
        state = GoalState.from_json(raw)
    except Exception:
        raise AutomaticRebaseError("GOAL_OWNER_INVALID") from None
    return state.status != "cleared"


def _mark_reconciliation_required(db: Any, transition_id: str) -> None:
    marker = getattr(type(db), "mark_context_rebase_reconciliation_required", None)
    if not callable(marker):
        return
    try:
        marker(db, transition_id)
    except Exception:
        pass


def _carry_session_scoped_state(
    db: Any, old_session_id: str, new_session_id: str
) -> None:
    """Move existing recurring/UI state through its canonical owners.

    A missing owner row is not an error. If an active row exists, migration is
    mandatory before READY; returning False cannot be silently interpreted as
    "nothing to do".
    """
    try:
        from hermes_cli.heartbeat import HeartbeatState, migrate_heartbeat_to_session

        raw = db.get_meta(f"heartbeat:{old_session_id}")
        active = False
        if raw:
            state = HeartbeatState.from_json(raw)
            active = state.status != "cleared"
        if active and not migrate_heartbeat_to_session(
            old_session_id, new_session_id, session_db=db
        ):
            raise AutomaticRebaseError("HEARTBEAT_RECONCILIATION_FAILED")
    except AutomaticRebaseError:
        raise
    except Exception:
        raise AutomaticRebaseError("HEARTBEAT_RECONCILIATION_FAILED") from None

    try:
        from hermes_cli.loops import LoopState, migrate_loop_to_session

        raw = db.get_meta(f"loop:{old_session_id}")
        active = False
        if raw:
            state = LoopState.from_json(raw)
            active = state.status != "cleared"
        if active and not migrate_loop_to_session(
            old_session_id,
            new_session_id,
            reason="context_rebase",
            session_db=db,
        ):
            raise AutomaticRebaseError("LOOP_RECONCILIATION_FAILED")
    except AutomaticRebaseError:
        raise
    except Exception:
        raise AutomaticRebaseError("LOOP_RECONCILIATION_FAILED") from None

    try:
        title = db.get_session_title(old_session_id)
        if title:
            source = db.get_session_title_source(old_session_id)
            if not db.set_session_title(new_session_id, title):
                raise AutomaticRebaseError("TITLE_RECONCILIATION_FAILED")
            if source is not None:
                db.set_session_title_source(new_session_id, source)
    except AutomaticRebaseError:
        raise
    except Exception:
        raise AutomaticRebaseError("TITLE_RECONCILIATION_FAILED") from None


def _transfer_run_custody(agent: Any, old_session_id: str, new_session_id: str) -> None:
    custody = getattr(agent, "_run_checkpoint_custody", None)
    if custody is None:
        db = getattr(agent, "_session_db", None)
        if db is not None and db.list_run_custody_for_session(new_session_id):
            raise AutomaticRebaseError("RUN_CUSTODY_RECONCILIATION_REQUIRED")
        return
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    try:
        custody.reconcile_context_rebase(holder, new_session_id)
    except Exception:
        raise AutomaticRebaseError("RUN_CUSTODY_RECONCILIATION_FAILED") from None


def _rebind_context_engine(
    agent: Any, db: Any, old_session_id: str, new_session_id: str
) -> None:
    transition = getattr(agent, "_transition_context_engine_session", None)
    if not callable(transition):
        raise AutomaticRebaseError("CONTEXT_ENGINE_REBIND_UNAVAILABLE")
    try:
        transition(
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            previous_messages=None,
            carry_over_context=False,
            reset_engine=False,
            strict=True,
            boundary_reason="context_rebase",
            session_db=db,
        )
        engine = getattr(agent, "context_compressor", None)
        validator = getattr(engine, "validate_context_rebase_binding", None)
        if callable(validator):
            validator(session_db=db, session_id=new_session_id)
    except Exception:
        raise AutomaticRebaseError("CONTEXT_ENGINE_REBIND_FAILED") from None


def _publish_runtime_session_context(agent: Any, session_id: str) -> None:
    try:
        from gateway.session_context import set_current_session_id

        set_current_session_id(session_id)
    except Exception:
        pass
    try:
        from hermes_logging import set_session_context

        set_session_context(session_id)
    except Exception:
        pass


def attempt_turn_start_context_rebase(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    conversation_history: list[dict[str, Any]] | None,
    active_system_prompt: str | None,
    before_tokens: int,
    min_reduction_bps: int = 2000,
    max_candidate_threshold_bps: int = 7000,
) -> AutomaticRebaseResult:
    """Attempt one automatic context rebase before provider/tool admission.

    The caller invokes this only after ordinary compaction has been proved
    blocked or ineffective at turn start.  The candidate is measured using the
    host's conservative request estimator solely as a *pre-publication soft
    feasibility gate*.  Normal provider admission still re-checks the final
    request after READY; this helper never calls a provider.
    """
    if not bool(getattr(agent, "context_rebase_enabled", False)):
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.SKIPPED,
            "CONTEXT_REBASE_DISABLED",
            str(getattr(agent, "session_id", "") or ""),
        )
    if type(before_tokens) is not int or before_tokens <= 0:
        raise AutomaticRebaseError("INVALID_PRE_REBASE_COUNT")
    if (
        type(min_reduction_bps) is not int
        or not 1 <= min_reduction_bps < 10000
        or type(max_candidate_threshold_bps) is not int
        or not 1 <= max_candidate_threshold_bps < 10000
    ):
        raise AutomaticRebaseError("INVALID_REBASE_POLICY")

    db = getattr(agent, "_session_db", None)
    parent_session_id = str(getattr(agent, "session_id", "") or "")
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    if db is None or not parent_session_id or not holder:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "CONTEXT_REBASE_OWNER_UNAVAILABLE",
            parent_session_id,
            before_tokens=before_tokens,
        )
    if not callable(getattr(type(db), "publish_context_rebase_child", None)):
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "CONTEXT_REBASE_STORE_UNAVAILABLE",
            parent_session_id,
            before_tokens=before_tokens,
        )

    max_no_progress = getattr(agent, "context_rebase_max_no_progress", 2)
    if (
        type(max_no_progress) is not int
        or max_no_progress < 1
        or max_no_progress > 100
    ):
        max_no_progress = 2
    try:
        episode = db.read_context_rebase_episode(parent_session_id)
    except Exception:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "CONTEXT_REBASE_EPISODE_UNAVAILABLE",
            parent_session_id,
            before_tokens=before_tokens,
        )
    if episode.attempts_without_recovery >= max_no_progress:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "NO_PROGRESS_REBASE_LIMIT",
            parent_session_id,
            before_tokens=before_tokens,
        )

    # Only transports that rebuild their complete provider input from the
    # supplied messages/system/tools are qualified by the stateless payload
    # upper-bound contract. Codex app-server and ACP own opaque remote state.
    api_mode = str(getattr(agent, "api_mode", "") or "")
    base_url = str(getattr(agent, "base_url", "") or "").lower()
    qualified_modes = {
        "chat_completions",
        "codex_responses",
        "anthropic_messages",
        "bedrock_converse",
    }
    if (
        api_mode not in qualified_modes
        or base_url.startswith("acp://")
        or base_url.startswith("acp+tcp://")
    ):
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "PROVIDER_CONTEXT_RESET_UNQUALIFIED",
            parent_session_id,
            before_tokens=before_tokens,
        )

    # Make the current human input part of the durable source snapshot before
    # compiling the successor. _flush_messages_to_session_db honors the clean
    # transcript override and the active turn lease.
    flush = getattr(agent, "_flush_messages_to_session_db", None)
    if not callable(flush):
        raise AutomaticRebaseError("CONTEXT_REBASE_FLUSH_UNAVAILABLE")
    try:
        persisted = flush(messages, conversation_history=conversation_history)
    except Exception:
        persisted = False
    if persisted is False:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "CONTEXT_REBASE_INPUT_NOT_DURABLE",
            parent_session_id,
            before_tokens=before_tokens,
        )

    try:
        candidate = build_live_candidate(db, session_id=parent_session_id)
    except LiveContinuationError as exc:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            str(exc),
            parent_session_id,
            before_tokens=before_tokens,
        )

    if candidate.unresolved_effects:
        # A prompt warning is not an effect barrier. This ordinary route has
        # no qualified observation-only lane, so preserve the parent and refuse.
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "CONTEXT_REBASE_UNRESOLVED_EFFECTS",
            parent_session_id,
            before_tokens=before_tokens,
        )

    new_system_prompt = _merged_system_prompt(
        active_system_prompt, candidate.system_addendum
    )
    # The current qualified upper-bound contract covers serialized text/tool
    # payloads. A short remote image/file reference can expand to many model
    # tokens outside its serialized byte size, so automatic publication must
    # refuse multimodal/opaque successor content until that route supplies its
    # own qualified accounting contract.
    if any(
        not isinstance(item.get("content"), str)
        for item in candidate.child_messages
        if isinstance(item, dict)
    ):
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_MULTIMODAL_ACCOUNTING_UNQUALIFIED",
            parent_session_id,
            before_tokens=before_tokens,
        )
    try:
        after_tokens = estimate_request_tokens_rough(
            list(candidate.child_messages),
            system_prompt=new_system_prompt,
            tools=getattr(agent, "tools", None) or None,
        )
    except Exception:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_COUNT_UNAVAILABLE",
            parent_session_id,
            before_tokens=before_tokens,
        )

    threshold = int(
        getattr(getattr(agent, "context_compressor", None), "threshold_tokens", 0)
        or 0
    )
    route_ref = ":".join(
        part.replace(" ", "_")
        for part in (
            str(getattr(agent, "provider", "") or "unknown"),
            api_mode,
            str(getattr(agent, "model", "") or "unknown"),
        )
    )
    try:
        qualified_after = stateless_payload_token_upper_bound(
            route_ref=route_ref,
            system_prompt=new_system_prompt,
            messages=[dict(item) for item in candidate.child_messages],
            tools=getattr(agent, "tools", None) or None,
        )
    except Exception:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_QUALIFIED_COUNT_UNAVAILABLE",
            parent_session_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    if after_tokens >= before_tokens:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_NO_REDUCTION",
            parent_session_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )
    reduction = (before_tokens - after_tokens) * 10000 // before_tokens
    if reduction < min_reduction_bps:
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_INSUFFICIENT_REDUCTION",
            parent_session_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )
    if (
        threshold <= 0
        or qualified_after.tokens * 10000
        >= threshold * max_candidate_threshold_bps
    ):
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            "SUCCESSOR_INSUFFICIENT_RUNWAY",
            parent_session_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            qualified_after_tokens=qualified_after.tokens,
        )

    transition_id, child_session_id = _stable_ids(candidate.continuation_digest)

    # Goal migration archives the parent goal row. Refuse before publication
    # when an owned run checkpoint treats that mutable row as immutable history.
    if _active_goal_required(db, parent_session_id):
        custody = getattr(agent, "_run_checkpoint_custody", None)
        checker = getattr(custody, "assert_goal_migration_safe", None)
        if callable(checker):
            try:
                checker(holder, session_id=parent_session_id)
            except Exception:
                return AutomaticRebaseResult(
                    AutomaticRebaseStatus.BLOCKED,
                    "HISTORICAL_GOAL_ALIAS_COLLISION",
                    parent_session_id,
                    transition_id=transition_id,
                    before_tokens=before_tokens,
                    after_tokens=after_tokens,
                )

    try:
        parent = db.get_session(parent_session_id) or {}
        model_config = parent.get("model_config") or {}
        if isinstance(model_config, str):
            model_config = json.loads(model_config or "{}")
        if not isinstance(model_config, dict):
            model_config = {}
        else:
            model_config = dict(model_config)
        # These fields describe the physical continuation edge, not model
        # configuration that may be inherited into the next epoch. The
        # SessionDB owner derives the next epoch from the parent row and stamps
        # a fresh authenticated edge below.
        for reserved_key in (
            "_context_rebase_from",
            "_context_rebase_transition",
            "_context_epoch",
            "_context_rebase_snapshot_digest",
        ):
            model_config.pop(reserved_key, None)
        custody = getattr(agent, "_run_checkpoint_custody", None)
        publisher = db.publish_context_rebase_child if custody is None else (
            lambda **kwargs: custody.publish_context_rebase(holder, **kwargs)
        )
        publisher(
            transition_id=transition_id,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            continuation_digest=candidate.continuation_digest,
            expected_snapshot_digest=candidate.snapshot_digest,
            snapshot_read_limits=candidate.snapshot_read_limits,
            control_revision=candidate.control_revision,
            input_watermark=candidate.input_watermark,
            turn_lease_holder=holder,
            source=str(parent.get("source") or getattr(agent, "platform", None) or "cli"),
            messages=[dict(item) for item in candidate.child_messages],
            model=str(getattr(agent, "model", "") or parent.get("model") or ""),
            model_config=model_config,
            system_prompt=new_system_prompt,
            cwd=parent.get("cwd"),
            profile_name=parent.get("profile_name"),
        )
    except Exception as exc:
        # A transport/ACK failure can follow the native COMMIT. Read the
        # canonical transition before describing it as a precommit refusal.
        try:
            committed = db.read_context_rebase_transition(transition_id)
        except Exception:
            committed = None
        if committed is not None:
            return reconcile_context_rebase(
                agent, transition_id=transition_id, before_tokens=before_tokens,
                after_tokens=after_tokens, qualified_after_tokens=qualified_after.tokens,
            )
        code = getattr(exc, "code", "CONTEXT_REBASE_PUBLICATION_FAILED")
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            str(code),
            parent_session_id,
            transition_id=transition_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    return reconcile_context_rebase(
        agent, transition_id=transition_id, before_tokens=before_tokens,
        after_tokens=after_tokens, qualified_after_tokens=qualified_after.tokens,
    )


def reconcile_context_rebase(
    agent: Any, *, transition_id: str | None = None,
    before_tokens: int | None = None, after_tokens: int | None = None,
    qualified_after_tokens: int | None = None,
) -> AutomaticRebaseResult:
    """Resume one durable publication; never compile or publish a second child.

    The normal turn lease fences this deterministic recovery. Missing legacy
    intent, unavailable owners or exhausted recovery budgets remain explicit
    stops. No model/tool operation is used to wake or complete this path.
    """
    db = getattr(agent, "_session_db", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    child_session_id = session_id
    try:
        if db is None:
            raise AutomaticRebaseError("CONTEXT_REBASE_OWNER_UNAVAILABLE")
        if transition_id is None:
            tip = db.get_context_continuation_tip(session_id)
            transition = db.context_rebase_transition_for_session(tip)
        else:
            transition = db.read_context_rebase_transition(transition_id)
        if transition is None:
            return AutomaticRebaseResult(AutomaticRebaseStatus.SKIPPED, "NO_PENDING_CONTEXT_REBASE", session_id)
        transition_id = transition.transition_id
        parent_session_id, child_session_id = transition.parent_session_id, transition.child_session_id
        if transition.state == "ready":
            return AutomaticRebaseResult(AutomaticRebaseStatus.SKIPPED, "CONTEXT_REBASE_ALREADY_READY", child_session_id)
        reservation = db.begin_context_rebase_recovery(transition_id, turn_lease_holder=holder)
        # Old publications may not have local owner transfers. Their absent
        # recovery intent is rejected above rather than silently upgraded.
        _carry_session_scoped_state(db, parent_session_id, child_session_id)
        _transfer_run_custody(agent, parent_session_id, child_session_id)
        child = db.get_session(child_session_id)
        if not isinstance(child, dict) or child.get("ended_at") is not None:
            raise AutomaticRebaseError("CONTEXT_REBASE_CHILD_NOT_LIVE")
        new_system_prompt = child.get("system_prompt")
        if not isinstance(new_system_prompt, str):
            raise AutomaticRebaseError("CONTEXT_REBASE_SYSTEM_PROMPT_MISSING")
        _rebind_context_engine(agent, db, parent_session_id, child_session_id)
        durable_messages = db.get_messages_as_conversation(
            child_session_id, repair_alternation=False,
            include_row_ids=True, include_summary_markers=True,
        )
        if not durable_messages or durable_messages[-1].get("role") != "user":
            raise AutomaticRebaseError("SUCCESSOR_DURABLE_USER_ANCHOR_MISSING")
        db.mark_context_rebase_ready(
            transition_id, expected_continuation_digest=transition.continuation_digest,
            expected_child_session_id=child_session_id,
            before_tokens=before_tokens, after_tokens=after_tokens,
            turn_lease_holder=holder, recovery_attempt=reservation["attempts"],
        )
        agent.session_id = child_session_id
        agent._session_db_created = True
        agent._cached_system_prompt = new_system_prompt
        agent._flushed_db_message_ids = set()
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_session_id = child_session_id
        _publish_runtime_session_context(agent, child_session_id)
    except Exception as exc:
        if db is not None and transition_id is not None:
            _mark_reconciliation_required(db, transition_id)
        code = str(exc) if isinstance(exc, AutomaticRebaseError) else getattr(
            exc, "code", "CONTEXT_REBASE_RECONCILIATION_FAILED"
        )
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.RECONCILIATION_REQUIRED, str(code), child_session_id,
            transition_id=transition_id, before_tokens=before_tokens, after_tokens=after_tokens,
        )
    return AutomaticRebaseResult(
        status=AutomaticRebaseStatus.READY, reason="CONTEXT_REBASE_READY",
        session_id=child_session_id, messages=tuple(durable_messages),
        system_prompt=new_system_prompt, transition_id=transition_id,
        before_tokens=before_tokens, after_tokens=after_tokens,
        qualified_after_tokens=qualified_after_tokens,
    )
