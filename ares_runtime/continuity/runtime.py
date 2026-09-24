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
from typing import Any

from agent.model_metadata import estimate_request_tokens_rough
from hermes_cli.goals import GoalState, migrate_goal_to_session

from .budget import stateless_payload_token_upper_bound
from .compiler import Mode
from .live import LiveContinuationError, build_live_candidate


class AutomaticRebaseError(RuntimeError):
    """Stable refusal code only; never carries prompt/source payloads."""


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
        return
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    ttl = float(
        getattr(agent, "_active_session_turn_lease_ttl_seconds", 300.0) or 300.0
    )
    try:
        custody.transfer_session(
            holder,
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            ttl_seconds=ttl,
        )
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
            extra_context={
                "boundary_reason": "context_rebase",
                "session_db": db,
            },
        )
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

    new_system_prompt = _merged_system_prompt(
        active_system_prompt, candidate.system_addendum
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
        db.publish_context_rebase_child(
            transition_id=transition_id,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            continuation_digest=candidate.continuation_digest,
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
        code = getattr(exc, "code", "CONTEXT_REBASE_PUBLICATION_FAILED")
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.BLOCKED,
            str(code),
            parent_session_id,
            transition_id=transition_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    try:
        if _active_goal_required(db, parent_session_id):
            if not migrate_goal_to_session(
                parent_session_id,
                child_session_id,
                reason="context_rebase",
                session_db=db,
            ):
                raise AutomaticRebaseError("GOAL_RECONCILIATION_FAILED")
        _carry_session_scoped_state(db, parent_session_id, child_session_id)
        _transfer_run_custody(agent, parent_session_id, child_session_id)

        agent.session_id = child_session_id
        agent._session_db_created = True
        agent._cached_system_prompt = new_system_prompt
        _publish_runtime_session_context(agent, child_session_id)
        _rebind_context_engine(agent, db, parent_session_id, child_session_id)

        durable_messages = db.get_messages_as_conversation(
            child_session_id,
            repair_alternation=True,
            include_row_ids=True,
            include_summary_markers=True,
        )
        if not durable_messages or durable_messages[-1].get("role") != "user":
            raise AutomaticRebaseError("SUCCESSOR_DURABLE_USER_ANCHOR_MISSING")

        # The child rows were inserted by publish_context_rebase_child.  Reset
        # flush cursors and use born-durable materialized rows so the turn-end
        # flush cannot append the bootstrap a second time.
        agent._flushed_db_message_ids = set()
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_session_id = child_session_id

        db.mark_context_rebase_ready(
            transition_id,
            expected_continuation_digest=candidate.continuation_digest,
            expected_child_session_id=child_session_id,
        )
    except Exception as exc:
        _mark_reconciliation_required(db, transition_id)
        code = (
            str(exc)
            if isinstance(exc, AutomaticRebaseError)
            else getattr(exc, "code", "CONTEXT_REBASE_RECONCILIATION_FAILED")
        )
        return AutomaticRebaseResult(
            AutomaticRebaseStatus.RECONCILIATION_REQUIRED,
            str(code),
            child_session_id,
            transition_id=transition_id,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    return AutomaticRebaseResult(
        status=AutomaticRebaseStatus.READY,
        reason="CONTEXT_REBASE_READY",
        session_id=child_session_id,
        messages=tuple(durable_messages),
        system_prompt=new_system_prompt,
        transition_id=transition_id,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        qualified_after_tokens=qualified_after.tokens,
    )
