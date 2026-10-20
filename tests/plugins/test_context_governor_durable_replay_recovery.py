"""The durable replay fallback remains disabled until its owner transition is proven."""

import copy
import json

import pytest

from agent.context_engine import CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
from plugins.context_engine._context_governor import ContextGovernorEngine
from plugins.context_engine._context_governor.protocol import (
    ContextGovernorCommandError,
)


class _ForbiddenSessionDB:
    def __init__(self):
        self.calls = 0

    def get_messages_as_conversation(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("lineage mismatch must not read/rebase from SessionDB")


def _engine():
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine._session_db = _ForbiddenSessionDB()
    engine.session_id = "s1"
    engine._lineage_session_id = "s1"
    engine._governor_session_id = lambda: "s1"
    engine._policy = {
        "summary_max_chars": 1000,
        "allocator": "greedy",
        "semantic_memory_enabled": False,
        "archive_memory_enabled": False,
        "budget_mode": "warn",
        "token_counter": "approx_chars",
        "unsafe_summary_policy": "reject",
        "max_lineage_generation": 32,
        "max_provenance_bytes": 4096,
    }
    engine.protect_first_n = 0
    engine.protect_last_n = 0
    engine.fallback_event_count = 0
    engine._pending_admission = None
    engine._lineage_integrity_vetoes = {}
    engine._target_tokens = lambda _current_tokens: 64
    engine._record_tool_telemetry = lambda _messages: None
    engine._score_telemetry_relevance = lambda _messages, _focus_topic: []
    engine._advisory_protect_last_n = lambda _count, _scores: 0
    engine._checkpoint_strategy_json = lambda: {}
    engine._max_checkpoints = lambda: 1
    engine._request_min_net_savings_tokens = lambda: 0
    engine._message_to_governor = lambda message, _index: {
        "role": message["role"],
        "content": message["content"],
    }
    engine._rehydrate_legacy_parent_prefix = lambda messages: messages
    engine._without_host_todo_snapshots = lambda messages: [
        dict(message) for message in messages
    ]
    engine._safe_summary_diagnostic = lambda error: str(error)
    engine._classify_subprocess_error = lambda _error: "other"
    return engine


def _mismatch():
    return ContextGovernorCommandError("compact-v2", "lineage_integrity_mismatch", {})


def _cancelled_live_messages():
    return [
        {
            "role": "user",
            "content": "Review deployment plan.\n\nCancel deployment; read only.",
            "_row_id": 11,
        },
        {"role": "assistant", "content": "prior response", "_row_id": 12},
    ]


def _hidden_provisional_live_messages():
    return [
        {"role": "user", "content": "request", "_row_id": 1},
        {
            "role": "assistant",
            "content": "final response",
            "finish_reason": "stop",
            "_row_id": 3,
        },
    ]


@pytest.mark.parametrize(
    "messages",
    [_cancelled_live_messages(), _hidden_provisional_live_messages()],
    ids=["row-stamped-user-redirect", "omitted-provisional-row"],
)
def test_lineage_mismatch_returns_original_live_transcript(messages):
    engine = _engine()
    original = copy.deepcopy(messages)
    calls = []

    def compact(request):
        calls.append(copy.deepcopy(request["messages"]))
        raise _mismatch()

    engine._compact_v2_candidate = compact
    engine._enhance_with_llm_summary = lambda *args: pytest.fail("summary must not run")
    engine._finalize_response = lambda *args: pytest.fail("finalize must not run")
    engine._prepare_response = lambda *args: pytest.fail("prepare must not run")

    returned = engine._compress_once(messages, current_tokens=128)

    assert returned is messages
    assert messages == original
    assert len(calls) == 1
    assert engine._session_db.calls == 0
    assert (
        engine.last_compaction_metrics["durable_replay_recovery"]
        == "disabled_fail_closed"
    )
    assert engine.last_outcome["kind"] == "compaction_failed_closed"
    assert engine.last_outcome["source_error_code"] == "lineage_integrity_mismatch"
    assert engine.last_outcome["source_operation"] == "compact-v2"
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )


def test_lineage_mismatch_on_host_reserve_retry_returns_original_live_transcript():
    engine = _engine()
    messages = _cancelled_live_messages()
    original = copy.deepcopy(messages)
    calls = []

    def compact(request):
        calls.append(copy.deepcopy(request["messages"]))
        if len(calls) == 1:
            return {
                "compacted_messages": [{"role": "user", "content": "candidate"}]
            }, None
        raise _mismatch()

    engine._compact_v2_candidate = compact
    engine._project_compaction_candidate = lambda _response, _source: (
        None,
        None,
        [{"role": "user", "content": "candidate"}],
    )
    engine._final_host_projection = lambda _source, candidate: (
        candidate + [{"role": "assistant", "content": "host-added boundary"}]
    )
    engine._estimate_messages_tokens = lambda projection: len(projection) * 10
    engine._enhance_with_llm_summary = lambda *args: pytest.fail("summary must not run")
    engine._finalize_response = lambda *args: pytest.fail("finalize must not run")
    engine._prepare_response = lambda *args: pytest.fail("prepare must not run")

    returned = engine._compress_once(messages, current_tokens=128)

    assert returned is messages
    assert messages == original
    assert len(calls) == 2
    assert engine._session_db.calls == 0
    assert engine.last_compaction_metrics["host_projection_reserve_retry"] is True
    assert (
        engine.last_compaction_metrics["durable_replay_recovery"]
        == "disabled_fail_closed"
    )
    assert engine.last_outcome["source_error_code"] == "lineage_integrity_mismatch"


def test_other_compaction_errors_do_not_read_or_rebase_from_database():
    engine = _engine()
    engine._lineage_integrity_vetoes["s1"] = False
    messages = _cancelled_live_messages()
    original = copy.deepcopy(messages)
    calls = []

    def compact(request):
        calls.append(copy.deepcopy(request["messages"]))
        raise ContextGovernorCommandError("compact-v2", "cannot_meet_target", {})

    engine._compact_v2_candidate = compact
    returned = engine._compress_once(messages, current_tokens=128)

    assert returned is messages
    assert messages == original
    assert len(calls) == 1
    assert engine._session_db.calls == 0
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )


@pytest.mark.parametrize(
    "ancestry_mode",
    [
        "known_same_root",
        "missing_child",
        "missing_parent",
        "missing_parent_id",
        "missing_end_reason",
        "missing_model_config",
        "null_model_config",
        "malformed_parent_false",
        "malformed_parent_zero",
        "malformed_parent_list",
        "empty_parent_id",
        "malformed_tool_parent_false",
        "malformed_config_false",
        "malformed_config_list",
        "malformed_branch_marker_with_delegate",
        "inherited_distinct_branch_delegate_markers",
        "api_unavailable",
        "inherited_branch_marker",
        "inherited_delegate_marker",
    ],
)
@pytest.mark.parametrize("boundary_reason", ["compression", "context_rebase"])
def test_known_veto_survives_detached_and_unresolved_ancestry_bindings(
    ancestry_mode, boundary_reason
):
    from types import SimpleNamespace

    from ares_runtime.continuity.runtime import (
        AutomaticRebaseStatus,
        attempt_turn_start_context_rebase,
    )

    class _AncestryDB:
        def __init__(self, mode):
            self.mode = mode
            self.episode_reads = 0

        def get_session(self, session_id):
            malformed_parent_ids = {
                "malformed_parent_false": False,
                "malformed_parent_zero": 0,
                "malformed_parent_list": [],
                "empty_parent_id": "",
                "malformed_tool_parent_false": False,
            }
            if self.mode in malformed_parent_ids and session_id == "physical-child":
                row = {
                    "parent_session_id": malformed_parent_ids[self.mode],
                    "model_config": {},
                }
                if self.mode == "malformed_tool_parent_false":
                    row["source"] = "tool"
                return row
            if self.mode == "malformed_config_false" and session_id == "physical-child":
                return {
                    "parent_session_id": "logical-root",
                    "model_config": False,
                }
            if self.mode == "malformed_config_list" and session_id == "physical-child":
                return {
                    "parent_session_id": "logical-root",
                    "model_config": [],
                }
            if (
                self.mode == "malformed_branch_marker_with_delegate"
                and session_id == "physical-child"
            ):
                return {
                    "parent_session_id": "logical-root",
                    "model_config": {
                        "_branched_from": False,
                        "_delegate_from": "logical-root",
                    },
                }
            if (
                self.mode == "inherited_distinct_branch_delegate_markers"
                and session_id == "physical-child"
            ):
                return {
                    "parent_session_id": "logical-root",
                    "model_config": {
                        "_branched_from": "unrelated-branch",
                        "_delegate_from": "unrelated-root",
                    },
                }
            if self.mode in {
                "inherited_branch_marker",
                "inherited_delegate_marker",
            }:
                marker = (
                    "_branched_from"
                    if self.mode == "inherited_branch_marker"
                    else "_delegate_from"
                )
                if session_id == "physical-child":
                    return {
                        "parent_session_id": "logical-root",
                        "model_config": {marker: "original-parent"},
                    }
                if session_id == "logical-root":
                    return {
                        "parent_session_id": "original-parent",
                        "end_reason": "compression",
                        "model_config": {marker: "original-parent"},
                    }
                if session_id == "original-parent":
                    return {
                        "parent_session_id": None,
                        "end_reason": None,
                        "model_config": {},
                    }
            if self.mode == "missing_child" and session_id == "physical-child":
                return None
            if self.mode == "missing_parent" and session_id == "logical-root":
                return None
            if self.mode == "missing_parent_id" and session_id == "physical-child":
                return {"model_config": {}}
            if self.mode == "missing_end_reason" and session_id == "logical-root":
                return {"parent_session_id": None}
            if self.mode == "missing_model_config" and session_id == "physical-child":
                return {"parent_session_id": "logical-root"}
            if self.mode == "null_model_config" and session_id == "physical-child":
                return {
                    "parent_session_id": "logical-root",
                    "model_config": None,
                }
            rows = {
                "physical-child": {
                    "parent_session_id": "logical-root",
                    "model_config": {},
                },
                "logical-root": {
                    "parent_session_id": None,
                    "end_reason": "compression",
                },
            }
            return rows.get(session_id)

        def read_context_rebase_episode(self, _session_id):
            self.episode_reads += 1
            raise AssertionError("veto must precede owner DB reads")

    class _NoGetterDB:
        def __init__(self):
            self.episode_reads = 0

        def read_context_rebase_episode(self, _session_id):
            self.episode_reads += 1
            raise AssertionError("veto must precede owner DB reads")

    engine = _engine()
    engine.session_id = "physical-child"
    engine._lineage_session_id = "logical-root"
    engine._governor_session_id = lambda: (
        engine._lineage_session_id or engine.session_id
    )
    engine._lineage_integrity_vetoes["logical-root"] = False
    engine._reconcile_pending_receipts = lambda *_args: None
    engine._previous_summary = None
    engine._load_prior_session_context = lambda: None

    engine.bind_session_state(session_db=None, session_id="physical-child")
    assert engine._lineage_session_id == "logical-root"
    engine.on_session_start(
        "physical-child",
        boundary_reason=boundary_reason,
        old_session_id="logical-root",
    )
    assert engine._lineage_session_id == "logical-root"

    db = (
        _NoGetterDB()
        if ancestry_mode == "api_unavailable"
        else _AncestryDB(ancestry_mode)
    )
    resolved = engine.bind_session_state(session_db=db, session_id="physical-child")
    if ancestry_mode in {
        "known_same_root",
        "null_model_config",
        "inherited_distinct_branch_delegate_markers",
        "inherited_branch_marker",
        "inherited_delegate_marker",
    }:
        assert resolved is True
    else:
        assert resolved is False
    assert engine._lineage_session_id == "logical-root"
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )

    # A continuation-boundary callback may rebind physical IDs, but unknown or
    # incomplete ancestry still cannot masquerade as a new logical session.
    engine.on_session_start(
        "physical-child",
        session_db=db,
        boundary_reason=boundary_reason,
    )
    assert engine._lineage_session_id == "logical-root"
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )

    agent = SimpleNamespace(
        context_rebase_enabled=True,
        context_compressor=engine,
        session_id="physical-child",
        _session_db=db,
    )
    result = attempt_turn_start_context_rebase(
        agent,
        _cancelled_live_messages(),
        conversation_history=None,
        active_system_prompt="system",
        before_tokens=128,
    )
    assert result.status is AutomaticRebaseStatus.BLOCKED
    assert result.reason == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    assert db.episode_reads == 0


def test_unresolved_root_veto_migrates_when_same_physical_child_is_resolved(tmp_path):
    from types import SimpleNamespace

    from ares_runtime.continuity.runtime import (
        AutomaticRebaseStatus,
        attempt_turn_start_context_rebase,
    )

    class _ResolvingDB:
        def __init__(self):
            self.parent_visible = False
            self.episode_reads = 0

        def get_session(self, session_id):
            if session_id == "physical-child":
                return {
                    "parent_session_id": "logical-root",
                    "model_config": {},
                }
            if session_id == "logical-root" and self.parent_visible:
                return {
                    "parent_session_id": None,
                    "end_reason": "compression",
                }
            return None

        def read_context_rebase_episode(self, _session_id):
            self.episode_reads += 1
            raise AssertionError("refined veto must precede the episode read")

    db = _ResolvingDB()
    engine = _engine()
    engine.session_id = "physical-child"
    engine._lineage_session_id = ""
    engine._lineage_session_id_resolved = False
    engine._governor_session_id = lambda: (
        engine._lineage_session_id or engine.session_id
    )
    engine._reconcile_pending_receipts = lambda *_args: None
    engine.bind_session_state(session_db=db, session_id="physical-child")

    assert engine._lineage_session_id == "physical-child"
    assert engine._lineage_session_id_resolved is False

    def fail_integrity(_request):
        raise _mismatch()

    engine._compact_v2_candidate = fail_integrity
    assert engine._compress_once(_cancelled_live_messages(), current_tokens=128)
    assert engine._lineage_integrity_vetoes == {"physical-child": True}

    db.parent_visible = True
    engine.bind_session_state(session_db=db, session_id="physical-child")

    assert engine._lineage_session_id == "logical-root"
    assert engine._lineage_session_id_resolved is True
    assert engine._lineage_integrity_vetoes == {"logical-root": False}
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )

    agent = SimpleNamespace(
        context_rebase_enabled=True,
        context_compressor=engine,
        session_id="physical-child",
        _session_db=db,
    )
    result = attempt_turn_start_context_rebase(
        agent,
        _cancelled_live_messages(),
        conversation_history=None,
        active_system_prompt="system",
        before_tokens=128,
    )
    assert result.status is AutomaticRebaseStatus.BLOCKED
    assert result.reason == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    assert db.episode_reads == 0


def test_unresolved_veto_does_not_migrate_to_an_unrelated_resolved_lineage():
    class _TwoLineageDB:
        def __init__(self):
            self.parents_visible = False

        def get_session(self, session_id):
            rows = {
                "unresolved-child": {
                    "parent_session_id": "old-root",
                    "model_config": {},
                },
                "new-child": {
                    "parent_session_id": "new-root",
                    "model_config": {},
                },
                "old-root": {
                    "parent_session_id": None,
                    "end_reason": "compression",
                },
                "new-root": {
                    "parent_session_id": None,
                    "end_reason": "compression",
                },
            }
            if session_id == "old-root" and not self.parents_visible:
                return None
            if session_id == "unresolved-child" and not self.parents_visible:
                return rows[session_id]
            return rows.get(session_id)

    db = _TwoLineageDB()
    engine = _engine()
    engine.session_id = "unresolved-child"
    engine._lineage_session_id = ""
    engine._lineage_session_id_resolved = False
    engine._governor_session_id = lambda: (
        engine._lineage_session_id or engine.session_id
    )
    engine._reconcile_pending_receipts = lambda *_args: None
    engine.bind_session_state(session_db=db, session_id="unresolved-child")
    assert engine._lineage_session_id == "unresolved-child"

    def fail_integrity(_request):
        raise _mismatch()

    engine._compact_v2_candidate = fail_integrity
    engine._compress_once(_cancelled_live_messages(), current_tokens=128)
    assert engine._lineage_integrity_vetoes == {"unresolved-child": True}

    db.parents_visible = True
    engine.bind_session_state(session_db=db, session_id="new-child")
    assert engine._lineage_session_id == "new-root"
    assert engine._lineage_integrity_vetoes == {"unresolved-child": True}
    assert engine.automatic_rebase_block_reason() is None

    # A second mismatch on the other lineage must not overwrite the first.
    engine._compress_once(_cancelled_live_messages(), current_tokens=128)
    assert engine._lineage_integrity_vetoes == {
        "unresolved-child": True,
        "new-root": False,
    }

    engine.bind_session_state(session_db=db, session_id="unresolved-child")
    assert engine._lineage_session_id == "old-root"
    assert engine._lineage_integrity_vetoes == {
        "old-root": False,
        "new-root": False,
    }
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )

    engine.bind_session_state(session_db=db, session_id="new-child")
    assert engine._lineage_session_id == "new-root"
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )


def _new_context_rebase_test_db(tmp_path):
    from hermes_state import SessionDB
    from tests.ares_runtime.test_context_rebase_state import _publish

    db = SessionDB(db_path=tmp_path / "lineage.db")
    db.create_session("root", source="cli", profile_name="p1", model="test")
    db.append_message("root", "user", "original task")
    assert db.try_acquire_session_turn_lease("root", "holder", ttl_seconds=300)
    _publish(db, transition="tx-lineage", parent="root", child="child")
    return db


def test_valid_context_rebase_transition_resolves_logical_root(tmp_path):
    db = _new_context_rebase_test_db(tmp_path)
    try:
        root, resolved = ContextGovernorEngine._context_lineage_root_with_evidence(
            db, "child"
        )
        assert root == "root"
        assert resolved is True
    finally:
        db.close()


def _reset_child_db(tmp_path, *, explicit_marker):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / f"reset-{explicit_marker}.db")
    db.create_session(
        "before-reset",
        source="cli",
        session_key="reset-route",
        profile_name="p1",
        model="test",
    )
    db.append_message("before-reset", "user", "old task")
    db.end_session("before-reset", "session_reset")
    reset_config = {"_reset_from": "before-reset"} if explicit_marker else None
    db.create_session(
        "reset-child",
        source="cli",
        session_key="reset-route",
        parent_session_id="before-reset",
        model_config=reset_config,
        profile_name="p1",
        model="test",
    )
    db.append_message("reset-child", "user", "new task")
    return db


@pytest.mark.parametrize("explicit_marker", [True, False])
def test_reset_child_is_a_new_resolved_logical_root(tmp_path, explicit_marker):
    db = _reset_child_db(tmp_path, explicit_marker=explicit_marker)
    try:
        root, resolved = ContextGovernorEngine._context_lineage_root_with_evidence(
            db, "reset-child"
        )
        assert root == "reset-child"
        assert resolved is True
    finally:
        db.close()


@pytest.mark.parametrize("explicit_marker", [True, False])
def test_reset_marker_remains_authoritative_after_parent_reopen(
    tmp_path, explicit_marker
):
    db = _reset_child_db(tmp_path, explicit_marker=explicit_marker)
    try:
        db.reopen_session("before-reset")
        parent = db.get_session("before-reset")
        child = db.get_session("reset-child")
        assert parent["end_reason"] is None
        assert json.loads(child["model_config"])["_reset_from"] == "before-reset"

        root, resolved = ContextGovernorEngine._context_lineage_root_with_evidence(
            db, "reset-child"
        )
        assert root == "reset-child"
        assert resolved is True
    finally:
        db.close()


def test_compression_continuation_inherits_reset_marker_without_rejoining_old_root(
    tmp_path,
):
    db = _reset_child_db(tmp_path, explicit_marker=True)
    try:
        db.reopen_session("before-reset")
        db.end_session("reset-child", "compression")
        db.create_session(
            "compression-tip",
            source="cli",
            session_key="reset-route",
            parent_session_id="reset-child",
            model_config={"_reset_from": "before-reset"},
            profile_name="p1",
            model="test",
        )
        root, resolved = ContextGovernorEngine._context_lineage_root_with_evidence(
            db, "compression-tip"
        )
        assert root == "reset-child"
        assert resolved is True
    finally:
        db.close()


def test_context_rebase_successor_of_reset_child_binds_against_new_root(tmp_path):
    from tests.ares_runtime.test_context_rebase_state import _publish

    db = _reset_child_db(tmp_path, explicit_marker=True)
    try:
        db.reopen_session("before-reset")
        assert db.try_acquire_session_turn_lease(
            "reset-child", "holder", ttl_seconds=300
        )
        _publish(
            db,
            transition="tx-reset-rebase",
            parent="reset-child",
            child="rebase-child",
        )

        engine = _engine()
        engine.session_id = "rebase-child"
        engine._lineage_session_id = ""
        engine._lineage_session_id_resolved = False
        engine._governor_session_id = lambda: (
            engine._lineage_session_id or engine.session_id
        )
        engine._reconcile_pending_receipts = lambda *_args: None
        engine.store_dir = tmp_path
        engine._run_certified_json = lambda *_args: []

        assert (
            engine.bind_session_state(session_db=db, session_id="rebase-child") is True
        )
        assert engine._lineage_session_id == "reset-child"
        engine.validate_context_rebase_binding(session_db=db, session_id="rebase-child")
    finally:
        db.close()


def test_mixed_fork_markers_follow_sessiondb_owner_or_semantics(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "mixed-fork.db")
    try:
        db.create_session("ancestor", source="delegate", profile_name="p1")
        db.create_session(
            "parent",
            source="delegate",
            parent_session_id="ancestor",
            model_config={"_delegate_from": "ancestor"},
            profile_name="p1",
        )
        db.create_session(
            "branch",
            source="cli",
            parent_session_id="parent",
            model_config={
                "_branched_from": "parent",
                "_delegate_from": "ancestor",
            },
            profile_name="p1",
        )
        row = db.get_session("branch")
        assert db._is_explicit_fork_child_row(row) is True
        root, resolved = ContextGovernorEngine._context_lineage_root_with_evidence(
            db, "branch"
        )
        assert root == "branch"
        assert resolved is True
    finally:
        db.close()


@pytest.mark.parametrize("bad_marker", [True, 7, ["bad"], {"bad": "type"}, " "])
def test_malformed_context_rebase_marker_preserves_veto_and_fails_closed(
    tmp_path, bad_marker
):
    from types import SimpleNamespace

    from ares_runtime.continuity.runtime import (
        AutomaticRebaseStatus,
        attempt_turn_start_context_rebase,
    )

    db = _new_context_rebase_test_db(tmp_path)
    try:
        db.patch_session_model_config(
            "child", {"_context_rebase_transition": bad_marker}
        )
        engine = _engine()
        engine.session_id = "child"
        engine._lineage_session_id = "root"
        engine._governor_session_id = lambda: (
            engine._lineage_session_id or engine.session_id
        )
        engine._lineage_integrity_vetoes["root"] = False
        engine._session_db = db
        engine._reconcile_pending_receipts = lambda *_args: None
        engine._previous_summary = None
        engine._load_prior_session_context = lambda: None
        engine.bind_session_state(session_db=db, session_id="child")
        assert engine._lineage_session_id == "root"
        assert (
            engine.automatic_rebase_block_reason()
            == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
        )

        with pytest.raises(RuntimeError, match="GOVERNOR_REBASE_LINEAGE_UNRESOLVED"):
            engine.validate_context_rebase_binding(session_db=db, session_id="child")

        episode_reads = 0

        def forbidden_episode_read(_session_id):
            nonlocal episode_reads
            episode_reads += 1
            raise AssertionError("unresolved veto must precede the episode read")

        db.read_context_rebase_episode = forbidden_episode_read
        agent = SimpleNamespace(
            context_rebase_enabled=True,
            context_compressor=engine,
            session_id="child",
            _session_db=db,
        )
        result = attempt_turn_start_context_rebase(
            agent,
            _cancelled_live_messages(),
            conversation_history=None,
            active_system_prompt="system",
            before_tokens=128,
        )
        assert result.status is AutomaticRebaseStatus.BLOCKED
        assert result.reason == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
        assert episode_reads == 0
    finally:
        db.close()


def test_veto_is_scoped_to_lineage_and_survives_starting_another_session():
    engine = _engine()
    engine._governor_session_id = lambda: (
        engine._lineage_session_id or engine.session_id
    )
    engine._lineage_integrity_vetoes["s1"] = False
    engine._previous_summary = None
    engine._load_prior_session_context = lambda: None

    engine.on_session_start("s2")
    assert engine._lineage_session_id == "s2"
    assert engine.automatic_rebase_block_reason() is None
    assert engine._lineage_integrity_vetoes == {"s1": False}

    engine.on_session_start("s1")
    assert engine._lineage_session_id == "s1"
    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )


def test_lineage_veto_clears_on_explicit_reset():
    engine = _engine()
    engine._lineage_integrity_vetoes["s1"] = False

    engine.on_session_reset()

    assert engine._lineage_integrity_vetoes == {}
    assert engine.automatic_rebase_block_reason() is None


def test_lineage_reset_clears_only_the_active_veto_entry():
    engine = _engine()
    engine._lineage_integrity_vetoes = {"s1": False, "s2": False}

    engine.on_session_reset()

    assert engine._lineage_integrity_vetoes == {"s2": False}
    assert engine.automatic_rebase_block_reason() is None


def test_lineage_veto_survives_empty_compression_and_cancellation_rollback():
    from agent.conversation_compression import (
        _restore_compressor_attempt_state,
        _snapshot_compressor_attempt_state,
    )

    engine = _engine()
    attempt_state = _snapshot_compressor_attempt_state(engine)
    engine._lineage_integrity_vetoes["s1"] = False

    assert engine.compress([], current_tokens=0) == []
    _restore_compressor_attempt_state(engine, attempt_state)

    assert (
        engine.automatic_rebase_block_reason()
        == CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )


def test_same_error_code_from_non_compact_operation_does_not_set_veto():
    engine = _engine()
    messages = _cancelled_live_messages()

    def fail_other_operation(_request):
        raise ContextGovernorCommandError(
            "context-search", "lineage_integrity_mismatch", {}
        )

    engine._compact_v2_candidate = fail_other_operation
    assert engine._compress_once(messages, current_tokens=128) is messages
    assert engine.automatic_rebase_block_reason() is None


def test_cannot_meet_target_does_not_set_integrity_veto():
    engine = _engine()
    messages = _cancelled_live_messages()
    engine._compact_v2_candidate = lambda _request: (_ for _ in ()).throw(
        ContextGovernorCommandError("compact-v2", "cannot_meet_target", {})
    )

    assert engine._compress_once(messages, current_tokens=128) is messages
    assert engine.automatic_rebase_block_reason() is None


def test_lineage_veto_is_copied_to_same_lineage_clone(monkeypatch):
    from pathlib import Path

    engine = _engine()
    engine.binary = "/tmp/context-governor"
    engine.store_dir = Path("/tmp/context-governor-test")
    engine.timeout_sec = 30
    engine._lineage_integrity_vetoes["s1"] = False
    defaults = {
        "last_prompt_tokens": 0,
        "last_completion_tokens": 0,
        "last_total_tokens": 0,
        "threshold_tokens": 0,
        "context_length": 0,
        "compression_count": 0,
        "last_receipt_id": None,
        "last_error": None,
        "last_warning": None,
        "last_outcome": None,
        "last_summary_safety": None,
        "last_compaction_metrics": None,
        "fallback_event_count": 0,
        "max_tokens": None,
        "_last_compress_aborted": False,
        "_last_summary_error": None,
        "_last_summary_fallback_used": False,
        "_last_compression_made_progress": False,
        "last_real_prompt_tokens": 0,
        "last_compression_rough_tokens": 0,
        "awaiting_real_usage_after_compression": False,
        "_ineffective_compression_count": 0,
        "_last_compression_savings_pct": 100.0,
        "_last_rough_tokens_when_real_fit": 0,
        "_previous_summary": None,
        "_summary_mode": "extractive",
        "_summary_model": "",
        "_summary_provider": "",
        "_summary_api_key": "",
        "_summary_base_url": "",
        "_llm_checkpoint_count": 0,
        "_unsafe_configured_hmac_path": None,
        "model": "test-model",
        "base_url": "https://example.invalid",
        "api_key": "",
        "provider": "test",
        "api_mode": "chat_completions",
        "_policy": {},
    }
    vars(engine).update(defaults)

    def init_clone(clone, binary, store_dir, timeout_sec):
        clone.binary = binary
        clone.store_dir = Path(store_dir)
        clone.timeout_sec = timeout_sec

    monkeypatch.setattr(ContextGovernorEngine, "__init__", init_clone)
    cloned = copy.deepcopy(engine)

    assert cloned._lineage_session_id == "s1"
    assert cloned.automatic_rebase_block_reason() == (
        CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
    )
