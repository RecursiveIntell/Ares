"""F06: a live lineage mismatch retries once from the durable SessionDB rows."""
from types import SimpleNamespace

import pytest

from plugins.context_engine._context_governor import ContextGovernorEngine
from plugins.context_engine._context_governor.protocol import ContextGovernorCommandError


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_messages_as_conversation(self, session_id, **kwargs):
        self.calls.append((session_id, kwargs))
        return [dict(r) for r in self.rows]


def _engine(rows):
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine._session_db = _DB(rows)
    engine.session_id = "s1"
    engine._lineage_session_id = "s1"
    engine._governor_session_id = lambda: "s1"
    engine._message_to_governor = lambda m, i: {"role": m["role"], "content": m["content"]}
    engine._rehydrate_legacy_parent_prefix = lambda msgs: msgs
    engine._without_host_todo_snapshots = lambda msgs: [dict(m) for m in msgs]
    return engine


DURABLE = [
    {"role": "user", "content": "parent prefix", "_row_id": 1},
    {"role": "assistant", "content": "answer", "_row_id": 2},
]


def _mismatch():
    return ContextGovernorCommandError("compact-v2", "lineage_integrity_mismatch", {})


def test_durable_source_keeps_the_unpersisted_tail():
    engine = _engine(DURABLE)
    live = [
        {"role": "user", "content": "parent prefix + in-memory drift", "_row_id": 1},
        {"role": "assistant", "content": "answer", "_row_id": 2},
        {"role": "user", "content": "new, not yet persisted"},
    ]
    recovered = engine._durable_replay_source(live)
    assert [m["content"] for m in recovered] == [
        "parent prefix", "answer", "new, not yet persisted"
    ]
    _, kwargs = engine._session_db.calls[0]
    assert kwargs["include_summary_markers"] is True
    assert kwargs["repair_alternation"] is False


@pytest.mark.parametrize(
    "live",
    [
        # a persisted live row unknown to SessionDB
        [{"role": "user", "content": "x", "_row_id": 99}],
        # an unpersisted message in the middle cannot be placed
        [
            {"role": "user", "content": "parent prefix", "_row_id": 1},
            {"role": "user", "content": "unplaced"},
            {"role": "assistant", "content": "answer", "_row_id": 2},
        ],
        # nothing persisted at all
        [{"role": "user", "content": "only tail"}],
    ],
)
def test_durable_source_refuses_ambiguous_mappings(live):
    assert _engine(DURABLE)._durable_replay_source(live) is None


def test_durable_source_refuses_rows_written_by_another_actor():
    rows = DURABLE + [{"role": "user", "content": "concurrent", "_row_id": 3}]
    live = [dict(r) for r in DURABLE]
    assert _engine(rows)._durable_replay_source(live) is None


def test_mismatch_retries_once_from_durable_rows():
    engine = _engine(DURABLE)
    seen = []

    def compact(request):
        seen.append([m["content"] for m in request["messages"]])
        if len(seen) == 1:
            raise _mismatch()
        return {"ok": True}, None

    engine._compact_v2_candidate = compact
    live = [
        {"role": "user", "content": "parent prefix + in-memory drift", "_row_id": 1},
        {"role": "assistant", "content": "answer", "_row_id": 2},
    ]
    request = {"messages": [engine._message_to_governor(m, i) for i, m in enumerate(live)]}
    metrics = {}
    response, _, source = engine._compact_with_durable_replay_recovery(
        request, live, live, metrics
    )
    assert response == {"ok": True}
    assert seen == [["parent prefix + in-memory drift", "answer"], ["parent prefix", "answer"]]
    assert [m["content"] for m in source] == ["parent prefix", "answer"]
    assert metrics["durable_replay_recovery"] == "recovered"
    assert metrics["durable_replay_divergence"]["index"] == 0
    assert metrics["durable_replay_divergence"]["fields"] == ["content"]


def test_second_mismatch_is_not_retried_again():
    engine = _engine(DURABLE)
    calls = []

    def compact(request):
        calls.append(1)
        raise _mismatch()

    engine._compact_v2_candidate = compact
    live = [
        {"role": "user", "content": "drift", "_row_id": 1},
        {"role": "assistant", "content": "answer", "_row_id": 2},
    ]
    request = {"messages": [engine._message_to_governor(m, i) for i, m in enumerate(live)]}
    with pytest.raises(ContextGovernorCommandError):
        engine._compact_with_durable_replay_recovery(request, live, live, {})
    assert len(calls) == 2


@pytest.mark.parametrize("code", ["cannot_meet_target", "lineage_generation_limit"])
def test_other_failures_are_not_retried(code):
    engine = _engine(DURABLE)
    calls = []

    def compact(request):
        calls.append(1)
        raise ContextGovernorCommandError("compact-v2", code, {})

    engine._compact_v2_candidate = compact
    live = [dict(r) for r in DURABLE]
    with pytest.raises(ContextGovernorCommandError):
        engine._compact_with_durable_replay_recovery(
            {"messages": [{"role": "user", "content": "x"}]}, live, live, {}
        )
    assert len(calls) == 1


def test_identical_projection_is_not_retried():
    engine = _engine(DURABLE)
    calls = []

    def compact(request):
        calls.append(1)
        raise _mismatch()

    engine._compact_v2_candidate = compact
    live = [dict(r) for r in DURABLE]
    request = {"messages": [engine._message_to_governor(m, i) for i, m in enumerate(live)]}
    metrics = {}
    with pytest.raises(ContextGovernorCommandError):
        engine._compact_with_durable_replay_recovery(request, live, live, metrics)
    assert len(calls) == 1
    assert metrics["durable_replay_recovery"] == "identical"
