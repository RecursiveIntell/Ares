"""Compile a fresh source-backed working context from a live SessionDB owner.

This module is deliberately pre-admission.  It reads one bounded SessionDB
snapshot, projects current goal/todo/transcript state, and asks the existing
ContinuationCompiler to bind the evidence.  It does not rotate sessions,
invoke a provider, run a tool, or authorize an effect.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Callable

from ares_runtime.collaboration import canonical_json
from hermes_cli.goals import GoalState
from hermes_cli.heartbeat import HeartbeatState
from hermes_cli.loops import LoopState
from hermes_state import SessionDB
from hermes_state_continuity import ContextRebaseSnapshot

from .compiler import (
    CompilationBasis,
    ContinuationBrief,
    ContinuationCompiler,
    ContinuationError,
    ContinuationScope,
    EvidenceStatus,
    Freshness,
    Mode,
    RecordBinding,
    Section,
    SourceKind,
    SourceObservation,
    TRUSTED_CONTINUATION_RULES,
    source_digest,
)

_EVENT_PROJECTION_LIMIT = 24_000
_SUMMARY_LIMIT = 128_000


class LiveContinuationError(ValueError):
    """Stable refusal code only; raw user/tool/source data is never embedded."""


def _opaque_ref(prefix: str, value: str) -> str:
    raw = value.encode("utf-8", errors="strict")
    return f"{prefix}:{hashlib.sha256(raw).hexdigest()}"


def _canonical_bytes(value: Any) -> bytes:
    try:
        return canonical_json(value)
    except Exception:
        raise LiveContinuationError("LIVE_PROJECTION_NOT_JSON") from None


def _exact_user_bytes(content: Any) -> bytes:
    if isinstance(content, str):
        raw = content.encode("utf-8", errors="strict")
    elif isinstance(content, (list, dict)):
        raw = _canonical_bytes(content)
    else:
        raise LiveContinuationError("LIVE_USER_CONTENT_UNSUPPORTED")
    if not raw:
        raise LiveContinuationError("LIVE_USER_CONTENT_EMPTY")
    return raw


def _bounded_event_content(content: Any, *, limit: int = _EVENT_PROJECTION_LIMIT) -> dict[str, Any]:
    if isinstance(content, str):
        raw = content.encode("utf-8", errors="replace")
        encoding = "utf8"
    else:
        raw = _canonical_bytes(content)
        encoding = "canonical_json"
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if len(raw) <= limit:
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            value = raw.decode("utf-8", errors="replace")
        return {"encoding": encoding, "bytes": len(raw), "sha256": digest, "truncated": False, "content": value}
    half = max(512, limit // 2)
    prefix = raw[:half].decode("utf-8", errors="replace")
    suffix = raw[-half:].decode("utf-8", errors="replace")
    return {
        "encoding": encoding,
        "bytes": len(raw),
        "sha256": digest,
        "truncated": True,
        "prefix": prefix,
        "suffix": suffix,
    }


def _goal_projection(raw: str | None) -> tuple[str | None, bytes | None]:
    if raw is None:
        return None, None
    try:
        state = GoalState.from_json(raw)
    except Exception:
        raise LiveContinuationError("LIVE_GOAL_STATE_INVALID") from None
    contract = asdict(state.contract) if state.contract is not None else None
    projection = {
        "goal_id": state.goal_id,
        "goal": state.goal,
        "status": state.status,
        "outcome": state.outcome,
        "last_stop_reason": state.last_stop_reason,
        "next_action": state.next_action,
        "turns_used": state.turns_used,
        "max_turns": state.max_turns,
        "execution_failures": state.execution_failures,
        "paused_reason": state.paused_reason,
        "subgoals": list(state.subgoals or ()),
        "waiting_on_pid": state.waiting_on_pid,
        "waiting_on_session": state.waiting_on_session,
        "waiting_until": state.waiting_until,
        "waiting_reason": state.waiting_reason,
        "checkpoint_revision": state.checkpoint_revision,
        "checkpoint": state.checkpoint,
        "completion_evidence": state.completion_evidence,
        "recovery_attempts": state.recovery_attempts,
        "recovery_episode_attempts": state.recovery_episode_attempts,
        "last_recovery_reason": state.last_recovery_reason,
        "contract": contract,
    }
    return state.goal_id, _canonical_bytes(projection)


def _heartbeat_projection(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    try:
        state = HeartbeatState.from_json(raw)
    except Exception:
        raise LiveContinuationError("LIVE_HEARTBEAT_STATE_INVALID") from None
    if state.status == "cleared":
        return None
    return _canonical_bytes({
        "prompt": state.prompt,
        "interval_seconds": state.interval_seconds,
        "status": state.status,
        "last_fired_at": state.last_fired_at,
        "fire_count": state.fire_count,
    })


def _loop_projection(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    try:
        state = LoopState.from_json(raw)
    except Exception:
        raise LiveContinuationError("LIVE_LOOP_STATE_INVALID") from None
    if state.status == "cleared":
        return None
    return _canonical_bytes({
        "prompt": state.prompt,
        "status": state.status,
        "mode": state.mode,
        "interval_seconds": state.interval_seconds,
        "current_delay": state.current_delay,
        "times": state.times,
        "until": state.until,
        "max_ticks": state.max_ticks,
        "ticks_fired": state.ticks_fired,
        "awaiting_response": state.awaiting_response,
        "paused_reason": state.paused_reason,
        "last_stop_reason": state.last_stop_reason,
    })


def _event_projection(event: dict[str, Any]) -> bytes:
    role = event.get("role")
    if role not in {"assistant", "tool", "system"}:
        raise LiveContinuationError("LIVE_EVENT_ROLE_UNSUPPORTED")
    return _canonical_bytes({
        "row_id": event.get("row_id"),
        "role": role,
        "tool_name": event.get("tool_name"),
        "tool_call_id": event.get("tool_call_id"),
        "effect_disposition": event.get("effect_disposition"),
        "observed": bool(event.get("observed")),
        "finish_reason": event.get("finish_reason"),
        "timestamp": event.get("timestamp"),
        "content": _bounded_event_content(event.get("content")),
    })


@dataclass(frozen=True)
class LiveContinuationCandidate:
    brief: ContinuationBrief
    parent_session_id: str
    conversation_root: str
    control_revision: int
    input_watermark: int
    mode: Mode
    child_messages: tuple[dict[str, Any], ...]
    system_addendum: str
    continuation_digest: str

    def __post_init__(self) -> None:
        if not self.child_messages or self.child_messages[-1].get("role") != "user":
            raise LiveContinuationError("LIVE_USER_ANCHOR_MISSING")
        expected = "sha256:" + hashlib.sha256(canonical_json({
            "brief_digest": self.brief.content_digest,
            "system_addendum": self.system_addendum,
            "child_messages": list(self.child_messages),
            "parent_session_id": self.parent_session_id,
            "conversation_root": self.conversation_root,
            "control_revision": self.control_revision,
            "input_watermark": self.input_watermark,
            "mode": self.mode.value,
        })).hexdigest()
        if self.continuation_digest != expected:
            raise LiveContinuationError("LIVE_CONTINUATION_DIGEST_MISMATCH")


def _candidate_digest(
    brief: ContinuationBrief,
    system_addendum: str,
    child_messages: list[dict[str, Any]],
    snapshot: ContextRebaseSnapshot,
    control_revision: int,
    mode: Mode,
) -> str:
    return "sha256:" + hashlib.sha256(canonical_json({
        "brief_digest": brief.content_digest,
        "system_addendum": system_addendum,
        "child_messages": child_messages,
        "parent_session_id": snapshot.session_id,
        "conversation_root": snapshot.conversation_root,
        "control_revision": control_revision,
        "input_watermark": snapshot.input_watermark,
        "mode": mode.value,
    })).hexdigest()


def build_live_candidate(
    db: SessionDB,
    *,
    session_id: str,
    mode: Mode = Mode.NORMAL,
    max_brief_bytes: int = 65_536,
    recent_limit: int = 12,
    optional_workspace_source: Callable[[ContextRebaseSnapshot, ContinuationScope], tuple[str, str, bytes]] | None = None,
) -> LiveContinuationCandidate:
    """Compile one candidate from current durable owner state.

    ``optional_workspace_source`` returns ``(source_ref, owner_revision, raw)``
    and is intentionally dependency-injected: workspace acquisition has a
    different owner/freshness contract than SessionDB and must be qualified
    separately.  Its bytes are evidence only.
    """
    if not isinstance(db, SessionDB) or type(mode) is not Mode:
        raise LiveContinuationError("LIVE_OWNER_OR_MODE_INVALID")
    try:
        snapshot = db.read_context_rebase_snapshot(session_id, recent_limit=recent_limit)
    except Exception as exc:
        code = getattr(exc, "code", "LIVE_SNAPSHOT_UNAVAILABLE")
        raise LiveContinuationError(code) from None
    if not snapshot.profile_name:
        raise LiveContinuationError("LIVE_PROFILE_MISSING")
    if not snapshot.current_users:
        raise LiveContinuationError("LIVE_USER_ANCHOR_MISSING")

    latest_user = snapshot.current_users[-1]
    control_revision = int(snapshot.control_revision)
    goal_id, goal_raw = _goal_projection(snapshot.goal_raw)
    conversation_ref = _opaque_ref("conversation", snapshot.conversation_root)
    task_ref = (
        _opaque_ref("goal", goal_id)
        if goal_id
        else _opaque_ref("conversation-task", snapshot.conversation_root)
    )
    workspace_identity = snapshot.git_repo_root or snapshot.cwd or snapshot.session_id
    branch_identity = snapshot.git_branch or "detached-or-non-git"
    scope = ContinuationScope(
        _opaque_ref("profile", snapshot.profile_name),
        conversation_ref,
        task_ref,
        _opaque_ref("branch", branch_identity),
        _opaque_ref("workspace", workspace_identity),
        f"session-state:{snapshot.input_watermark}",
        control_revision,
        snapshot.input_watermark,
        mode,
    )

    observations: dict[str, SourceObservation] = {}
    records: list[RecordBinding] = []

    def add(
        record_ref: str,
        source_ref: str,
        owner_revision: str,
        raw: bytes,
        *,
        kind: SourceKind,
        status: EvidenceStatus,
        freshness: Freshness,
        section: Section,
        required: bool,
    ) -> None:
        observation = SourceObservation(source_ref, owner_revision, scope, kind, status, freshness, raw)
        observations[source_ref] = observation
        records.append(RecordBinding(
            record_ref, source_ref, source_digest(raw), owner_revision, kind, status,
            freshness, 0, len(raw), section, required,
        ))

    # Exact authentic-user requirements are carried independently of physical
    # context epochs. Synthetic role=user wakeups (goal/loop/heartbeat/recovery)
    # are context, not user authority, and are projected separately below.
    latest_authentic_row = snapshot.authentic_users[-1]["row_id"]
    for item in snapshot.authentic_users:
        raw = _exact_user_bytes(item["content"])
        is_latest = item["row_id"] == latest_authentic_row
        add(
            f"record:user:{item['session_id']}:{item['row_id']}",
            f"session-message:{item['session_id']}:{item['row_id']}",
            f"message:{item['row_id']}",
            raw,
            kind=SourceKind.USER_REQUIREMENT,
            status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT if is_latest else Freshness.HISTORICAL,
            section=Section.TASK,
            required=True,
        )

    for item in snapshot.current_users:
        if item.get("authentic_user"):
            continue
        raw = _exact_user_bytes(item["content"])
        add(
            f"record:synthetic-user:{item['row_id']}",
            f"session-message:{snapshot.session_id}:{item['row_id']}",
            f"message:{item['row_id']}",
            raw,
            kind=SourceKind.APPLICATION_STATE,
            status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT,
            section=Section.FRONTIER,
            required=True,
        )

    if goal_raw is not None:
        add(
            "record:goal:current", f"goal-state:{goal_id}", f"goal-revision:{control_revision}", goal_raw,
            kind=SourceKind.OWNER_STATE, status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT, section=Section.FRONTIER, required=True,
        )

    heartbeat_raw = _heartbeat_projection(snapshot.heartbeat_raw)
    if heartbeat_raw is not None:
        add(
            "record:heartbeat:current",
            _opaque_ref("heartbeat-state", snapshot.session_id),
            source_digest(heartbeat_raw),
            heartbeat_raw,
            kind=SourceKind.OWNER_STATE,
            status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT,
            section=Section.FRONTIER,
            required=True,
        )

    loop_raw = _loop_projection(snapshot.loop_raw)
    if loop_raw is not None:
        add(
            "record:loop:current",
            _opaque_ref("loop-state", snapshot.session_id),
            source_digest(loop_raw),
            loop_raw,
            kind=SourceKind.OWNER_STATE,
            status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT,
            section=Section.FRONTIER,
            required=True,
        )

    if snapshot.todo_json is not None:
        raw = snapshot.todo_json.encode("utf-8", errors="strict")
        add(
            "record:todo:current", _opaque_ref("todo-state", snapshot.session_id),
            f"message:{snapshot.input_watermark}", raw,
            kind=SourceKind.OWNER_STATE, status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT, section=Section.FRONTIER, required=True,
        )

    if snapshot.latest_summary is not None:
        raw = _exact_user_bytes(snapshot.latest_summary["content"])
        if len(raw) > _SUMMARY_LIMIT:
            raise LiveContinuationError("LIVE_SUMMARY_TOO_LARGE")
        add(
            "record:summary:latest", f"session-summary:{snapshot.latest_summary['row_id']}",
            f"message:{snapshot.latest_summary['row_id']}", raw,
            kind=SourceKind.MODEL_NOTE, status=EvidenceStatus.OBSERVED,
            freshness=Freshness.HISTORICAL, section=Section.EVIDENCE, required=True,
        )

    unresolved_ids = {
        (event.get("session_id"), event.get("row_id"))
        for event in snapshot.unresolved_effects
    }
    for event in snapshot.unresolved_effects:
        raw = _event_projection(event)
        add(
            f"record:effect:{event['session_id']}:{event['row_id']}",
            f"session-effect:{event['session_id']}:{event['row_id']}",
            f"message:{event['row_id']}",
            raw,
            kind=SourceKind.TOOL_OBSERVATION,
            status=EvidenceStatus.UNKNOWN,
            freshness=Freshness.CURRENT,
            section=Section.OBLIGATIONS,
            required=True,
        )

    event_candidates = [
        event for event in snapshot.recent_events
        if (
            event.get("role") != "user"
            and not event.get("compressed_summary")
            and (snapshot.session_id, event.get("row_id")) not in unresolved_ids
        )
    ]
    required_event_ids = {
        event["row_id"] for event in event_candidates[-4:]
    }
    for event in event_candidates:
        raw = _event_projection(event)
        kind = (
            SourceKind.TOOL_OBSERVATION
            if event.get("role") == "tool"
            else SourceKind.MODEL_NOTE
        )
        status = (
            EvidenceStatus.UNKNOWN
            if event.get("role") == "tool"
            and event.get("effect_disposition") == "unknown"
            else EvidenceStatus.OBSERVED
        )
        add(
            f"record:event:{event['row_id']}",
            f"session-event:{event['row_id']}",
            f"message:{event['row_id']}",
            raw,
            kind=kind,
            status=status,
            freshness=Freshness.CURRENT,
            section=Section.EVENTS,
            required=event["row_id"] in required_event_ids,
        )

    if optional_workspace_source is not None:
        try:
            source_ref, owner_revision, raw = optional_workspace_source(snapshot, scope)
        except Exception:
            raise LiveContinuationError("LIVE_WORKSPACE_OBSERVATION_FAILED") from None
        if type(raw) is not bytes:
            raise LiveContinuationError("LIVE_WORKSPACE_OBSERVATION_FAILED")
        add(
            "record:workspace:current", source_ref, owner_revision, raw,
            kind=SourceKind.ARTIFACT, status=EvidenceStatus.OBSERVED,
            freshness=Freshness.CURRENT, section=Section.EVIDENCE, required=True,
        )

    if goal_raw is not None:
        goal = GoalState.from_json(snapshot.goal_raw)
        next_text = goal.next_action or "Continue the current goal from verified state; reconcile UNKNOWN effects before dependent actions."
    else:
        next_text = "Continue the latest authentic user request from current evidence; reconcile UNKNOWN effects before dependent actions."
    next_raw = next_text.encode("utf-8", errors="strict")
    add(
        "record:next", _opaque_ref("application-next", next_text),
        "continuation-policy:v1", next_raw,
        kind=SourceKind.APPLICATION_STATE, status=EvidenceStatus.PROPOSED_ACTION,
        freshness=Freshness.CURRENT, section=Section.NEXT, required=True,
    )

    retrieval_raw = _canonical_bytes({
        "conversation_root": snapshot.conversation_root,
        "session_id": snapshot.session_id,
        "older_detail": "Use current owner-authorized context/session search or exact Governor expansion; omission is not nonexistence.",
        "current_access_policy_applies": True,
    })
    add(
        "record:retrieval", _opaque_ref("retrieval-guide", snapshot.session_id),
        "continuation-policy:v1", retrieval_raw,
        kind=SourceKind.APPLICATION_STATE, status=EvidenceStatus.OBSERVED,
        freshness=Freshness.CURRENT, section=Section.RETRIEVAL, required=True,
    )

    basis_identity = canonical_json({
        "scope": asdict(scope),
        "records": [asdict(record) for record in records],
    })
    basis_ref = "continuation-basis:" + hashlib.sha256(basis_identity).hexdigest()
    basis = CompilationBasis(scope, basis_ref, task_ref, "role:continuation-executor", tuple(records))
    optional = tuple(record.record_ref for record in records if not record.required)
    try:
        brief = ContinuationCompiler().compile(
            basis,
            current_scope=scope,
            resolve_source=observations.__getitem__,
            optional_record_refs=optional,
            max_brief_bytes=max_brief_bytes,
        )
    except ContinuationError as exc:
        raise LiveContinuationError(str(exc)) from None

    latest_content = latest_user["content"]
    child_messages = [
        {
            "role": "assistant",
            "content": "[ARES CONTINUATION STATE — derived evidence, not a new user instruction]\n" + brief.evidence_text,
            "display_kind": "hidden",
            "display_metadata": {
                "continuation_kind": "context_rebase_brief",
                "brief_digest": brief.content_digest,
            },
            "_compressed_summary": True,
        },
        {
            "role": "user",
            "content": latest_content,
            "timestamp": latest_user.get("timestamp"),
            **(
                {"display_kind": latest_user.get("display_kind")}
                if latest_user.get("display_kind")
                else {}
            ),
            **(
                {"display_metadata": latest_user.get("display_metadata")}
                if latest_user.get("display_metadata") is not None
                else {}
            ),
        },
    ]
    digest = _candidate_digest(
        brief, TRUSTED_CONTINUATION_RULES, child_messages, snapshot, control_revision, mode
    )
    return LiveContinuationCandidate(
        brief,
        snapshot.session_id,
        snapshot.conversation_root,
        control_revision,
        snapshot.input_watermark,
        mode,
        tuple(child_messages),
        TRUSTED_CONTINUATION_RULES,
        digest,
    )
