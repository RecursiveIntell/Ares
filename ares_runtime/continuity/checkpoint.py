"""Read a cold context from the existing SessionDB checkpoint owner.

This is an inspection/export consumer, not resume or egress authority. It can
read a released or crashed run, but never claims custody, refreshes a lease,
changes a goal, sends a prompt, or retries an effect. Scope is the checkpoint's
recorded scope, not proof it is still the live policy/control revision.

The canonical checkpoint must contain a closed ``continuation-scope-v1`` member
and may contain ``continuation-working-set-v1``. Files supplied by the caller
are only locators: the independently read native checkpoint supplies expected
hashes. Existing V1 claims and their historical-goal checks remain unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import sqlite3
from typing import Mapping

from ares_runtime.collaboration import canonical_json
from hermes_state import SessionDB
from hermes_state_runs import RunCustodyError
from scripts.run_checkpoint_resume import (
    BoundFileReader, ResumeRefusal, strict_json, verify_checkpoint_files,
)

from .compiler import (
    CompilationBasis, ContinuationBrief, ContinuationCompiler, ContinuationError,
    ContinuationScope, EvidenceStatus, Freshness, Mode, RecordBinding, Section,
    SourceKind, SourceObservation, source_digest,
)


class CheckpointContextError(ValueError):
    """Payload-free refusal; contains no paths, source text or custody token."""


@dataclass(frozen=True)
class CheckpointContext:
    brief: ContinuationBrief
    run_id: str
    custody_generation: int
    checkpoint_digest: str
    current_session_id: str
    source_files: int

    def summary(self) -> dict:
        return {
            "schema": "ares.checkpoint-context-inspection/v1",
            "run_id": self.run_id,
            "custody_generation": self.custody_generation,
            "checkpoint_digest": self.checkpoint_digest,
            "brief_digest": self.brief.content_digest,
            "source_files": self.source_files,
            "scope_currentness": "checkpoint_recorded_only",
            "resume_authorized": False,
            "effects_executed": False,
        }


class _ObservedReader(BoundFileReader):
    def __init__(self):
        super().__init__()
        self.observed: dict[str, bytes] = {}

    def read(self, path):
        raw = super().read(path)
        previous = self.observed.get(path)
        if previous is not None and raw != previous:
            raise ResumeRefusal("FILE_CHANGED_BETWEEN_READS")
        self.observed[path] = raw
        return raw


def _scope(member: str, owner, session: Mapping) -> ContinuationScope:
    value = strict_json(member.encode("utf-8"))
    if type(value) is not dict or set(value) != set(ContinuationScope.__dataclass_fields__):
        raise CheckpointContextError("CHECKPOINT_SCOPE_SCHEMA")
    try:
        scope = ContinuationScope(**{**value, "mode": Mode(value["mode"])})
    except (ValueError, TypeError):
        raise CheckpointContextError("CHECKPOINT_SCOPE_SCHEMA") from None
    profile = session.get("profile_name")
    if (type(profile) is not str or not profile
            or scope.profile_ref != "profile:" + profile
            or scope.task_ref != "run:" + owner.run_id
            or scope.conversation_ref != "session:" + owner.origin_session_id
            or scope.source_revision != "sha256:" + owner.checkpoint.source_digest):
        raise CheckpointContextError("CHECKPOINT_SCOPE_BINDING")
    return scope


def read_checkpoint_context(
    db: SessionDB,
    *,
    run_id: str,
    expected_generation: int,
    files: Mapping,
    max_brief_bytes: int = 65536,
) -> CheckpointContext:
    """Compile a read-only inspection from exact native state and file bindings.

    The same immutable owner generation is checked after bounded file reads.
    This detects owner movement, not a global filesystem snapshot or current
    disclosure authorization. The final runtime must independently revalidate
    current user/policy/route state before any use by a model or tool.
    """
    if not isinstance(db, SessionDB):
        raise CheckpointContextError("SESSIONDB_OWNER_REQUIRED")
    if type(expected_generation) is not int or expected_generation < 1:
        raise CheckpointContextError("INVALID_GENERATION")
    if type(files) is not dict:
        raise CheckpointContextError("INVALID_FILE_BINDINGS")
    try:
        return _read(db, run_id, expected_generation, files, max_brief_bytes)
    except CheckpointContextError:
        raise
    except (RunCustodyError, ResumeRefusal, ContinuationError):
        # Source/path-bearing exceptions are intentionally not included.
        raise CheckpointContextError("CHECKPOINT_CONTEXT_REFUSED") from None
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        raise CheckpointContextError("CHECKPOINT_CONTEXT_UNAVAILABLE") from None


def _read(db, run_id, generation, files, max_brief_bytes):
    owner = db.read_run_custody(run_id)
    if owner is None or owner.generation != generation:
        raise CheckpointContextError("CHECKPOINT_GENERATION_MISMATCH")
    checkpoint = owner.checkpoint
    members = dict(checkpoint.members)
    scope_member = members.get("continuation-scope-v1")
    if scope_member is None:
        raise CheckpointContextError("CHECKPOINT_SCOPE_MISSING")
    session = db.get_session(owner.current_session_id)
    if session is None:
        raise CheckpointContextError("CHECKPOINT_SESSION_MISSING")
    scope = _scope(scope_member, owner, session)
    # Keep the historical goal binding check for existing V1 custody, including
    # when inspecting a released owner. Reading is not permission to resume.
    goal_key = members.get("historical-goal-key")
    goal_raw = db.get_meta(goal_key) if goal_key else None
    if goal_raw is None or hashlib.sha256(goal_raw.encode()).hexdigest() != owner.historical_goal_digest:
        raise CheckpointContextError("HISTORICAL_GOAL_MISMATCH")
    reader = _ObservedReader()
    source_files = verify_checkpoint_files(checkpoint, files, reader)
    inventory = strict_json(reader.observed[files["source"]])
    bindings: list[RecordBinding] = []
    observations: dict[str, SourceObservation] = {}
    revision = "custody:" + str(generation)

    def include(name, raw, section, status, kind=SourceKind.OWNER_STATE, start=0, end=None):
        ref = "checkpoint:" + run_id + ":" + name
        observation = SourceObservation(ref, revision, scope, kind, status, Freshness.HISTORICAL, raw)
        observations[ref] = observation
        bindings.append(RecordBinding("record:" + name, ref, source_digest(raw), revision,
            kind, status, Freshness.HISTORICAL, start, len(raw) if end is None else end, section, True))

    include("contract", reader.observed[files["contract"]], Section.TASK, EvidenceStatus.OBSERVED)
    # A persisted plan is not necessarily an accepted design or verified result.
    include("plan", reader.observed[files["plan"]], Section.FRONTIER, EvidenceStatus.OBSERVED)
    include("next", checkpoint.next_action.encode(), Section.NEXT, EvidenceStatus.PROPOSED_ACTION)
    include("obligations", canonical_json({
        "unresolved_effects": checkpoint.unresolved_effects,
        "unresolved_findings": checkpoint.unresolved_findings,
        "restrictions": checkpoint.restrictions,
    }), Section.OBLIGATIONS, EvidenceStatus.UNKNOWN)
    include("inspection", canonical_json({
        "scope_currentness": "checkpoint_recorded_only",
        "custody_disposition": owner.disposition,
        "session_ended": session.get("ended_at") is not None,
        "current_policy_and_control_not_checked": True,
        "source_inventory_digest": "sha256:" + checkpoint.source_digest,
        "source_files_observed": source_files,
        "resume_authorized": False,
    }), Section.EVIDENCE, EvidenceStatus.OBSERVED)
    working = strict_json(members.get("continuation-working-set-v1", "[]").encode())
    if type(working) is not list or len(working) > 64:
        raise CheckpointContextError("WORKING_SET_SCHEMA")
    seen = set()
    for item in working:
        if type(item) is not dict or set(item) != {"path", "start_byte", "end_byte"}:
            raise CheckpointContextError("WORKING_SET_SCHEMA")
        path = item["path"]
        if type(path) is not str or path not in inventory or path in seen:
            raise CheckpointContextError("WORKING_SET_BINDING")
        seen.add(path)
        raw = reader.observed[path]
        name = "source-" + hashlib.sha256(path.encode()).hexdigest()
        include(name, raw, Section.EVIDENCE, EvidenceStatus.OBSERVED, SourceKind.ARTIFACT,
                item["start_byte"], item["end_byte"])
    basis = CompilationBasis(scope, "checkpoint:" + run_id + ":" + str(generation),
                             "run:" + run_id, "role:checkpoint-inspector", tuple(bindings))
    brief = ContinuationCompiler().compile(basis, current_scope=scope,
        resolve_source=observations.__getitem__, max_brief_bytes=max_brief_bytes)
    if db.read_run_custody(run_id) != owner:
        raise CheckpointContextError("CHECKPOINT_CHANGED_DURING_READ")
    if db.get_session(owner.current_session_id) != session or db.get_meta(goal_key) != goal_raw:
        raise CheckpointContextError("CHECKPOINT_BINDING_CHANGED_DURING_READ")
    checkpoint_digest = "sha256:" + hashlib.sha256(canonical_json(asdict(checkpoint))).hexdigest()
    return CheckpointContext(brief, run_id, generation, checkpoint_digest, owner.current_session_id, source_files)
