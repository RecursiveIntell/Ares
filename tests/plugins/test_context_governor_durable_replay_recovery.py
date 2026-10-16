"""The durable replay fallback remains disabled until its owner transition is proven."""

import copy

import pytest

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
