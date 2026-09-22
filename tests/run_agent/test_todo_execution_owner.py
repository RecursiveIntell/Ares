"""Dispatcher-to-owner witnesses; real temporary SessionDB, no provider I/O."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import invoke_tool
from agent.tool_executor import (
    execute_tool_calls_concurrent,
    execute_tool_calls_segmented,
    execute_tool_calls_sequential,
)
from hermes_state import SessionDB
from run_agent import AIAgent


ITEMS = [{"id": "one", "content": "First", "status": "pending"}]
NEXT = [{"id": "two", "content": "Second", "status": "pending"}]


@pytest.fixture
def execution(tmp_path):
    # Provider construction alone is stubbed; dispatch, normalization, lease,
    # snapshot transactions and persistence are real.
    with (
        patch("run_agent.OpenAI"),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test", base_url="http://127.0.0.1:1/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("producer", source="test")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = "producer"
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent._incremental_persistence_failed = False
    agent.tool_progress_callback = None
    agent.tool_complete_callback = None
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    holder = f"pid={os.getpid()}:todo-producer-test"
    assert db.acquire_session_turn_lease("producer", holder, ttl_seconds=60)
    agent._active_session_turn_lease_holder = holder
    try:
        yield agent, db
    finally:
        db.release_session_turn_lease("producer", holder)
        db.close()


def block(agent, args_list, names=None):
    names = names or ["todo"] * len(args_list)
    calls = [{"id": f"call-{i}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args),
    }} for i, (name, args) in enumerate(zip(names, args_list))]
    messages = [{"role": "assistant", "content": None, "tool_calls": calls}]
    assert agent._flush_messages_to_session_db(messages) is True
    assistant = SimpleNamespace(tool_calls=[SimpleNamespace(
        id=c["id"], type="function", function=SimpleNamespace(**c["function"]),
    ) for c in calls])
    return messages, assistant


def call(agent, messages, args=None, call_id="call-0"):
    return json.loads(invoke_tool(agent, "todo", args if args is not None else
                                 {"todos": ITEMS}, agent.session_id,
                                 tool_call_id=call_id, messages=messages))


def count(db):
    with db._read_ctx() as conn:
        return conn.execute("SELECT COUNT(*) FROM todo_snapshots").fetchone()[0]


@pytest.mark.parametrize("mode", ["sequential", "concurrent", "segmented"])
def test_real_dispatcher_commits_before_completion_projection(execution, mode):
    agent, db = execution
    messages, assistant = block(agent, [{"todos": ITEMS}, {"todos": NEXT, "merge": True}])
    observed = []
    def completed(*args, **kwargs):
        selected = db.get_current_todo_snapshot(agent.session_id)
        assert selected is not None
        assert any(r["role"] == "tool" for r in db.get_messages(agent.session_id))
        observed.append(selected)
    agent.tool_complete_callback = completed
    {"sequential": execute_tool_calls_sequential,
     "concurrent": execute_tool_calls_concurrent,
     "segmented": execute_tool_calls_segmented}[mode](
         agent, assistant, messages, agent.session_id)
    assert len(observed) == 2
    assert count(db) == 2
    selected = db.get_current_todo_snapshot(agent.session_id)
    assert selected["todos"] == agent._todo_store.read()
    assert selected["anchor_message_id"] == messages[0]["_row_id"]
    with db._read_ctx() as conn:
        ids = [r[0] for r in conn.execute("SELECT execution_id FROM todo_snapshots")]
    assert len(set(ids)) == 2
    assert not set(ids).intersection({"call-0", "call-1"})


@pytest.mark.parametrize("fault", ["no_lease", "stolen", "expired", "no_anchor", "wrong_call", "later_user", "wrong_session", "wrong_owner"])
def test_missing_custody_does_not_mutate_cache_or_owner(execution, fault):
    agent, db = execution
    agent._todo_store.write(NEXT)
    messages, _ = block(agent, [{"todos": ITEMS}])
    if fault == "no_lease":
        agent._active_session_turn_lease_holder = None
    elif fault == "stolen":
        db._execute_write(lambda conn: conn.execute("UPDATE session_turn_leases SET holder='other'"))
    elif fault == "expired":
        db._execute_write(lambda conn: conn.execute("UPDATE session_turn_leases SET expires_at=0"))
    elif fault == "no_anchor":
        messages[0].pop("_row_id")
    elif fault == "wrong_call":
        messages[0]["tool_calls"][0]["function"]["name"] = "terminal"
    elif fault == "later_user":
        messages.append({"role": "user", "content": "not this assistant"})
    elif fault == "wrong_session":
        db.create_session("other", source="test")
        agent.session_id = "other"
    else:
        agent._session_db = SimpleNamespace()
    payload = call(agent, messages)
    assert "error" in payload
    assert agent._todo_store.read() == NEXT
    assert count(db) == 0


def test_read_and_merge_use_owner_not_stale_cache(execution):
    agent, db = execution
    messages, _ = block(agent, [{"todos": ITEMS}, {"todos": NEXT, "merge": True}])
    assert "error" not in call(agent, messages)
    agent._todo_store.write([])
    assert call(agent, messages, {})["todos"] == ITEMS
    assert agent._todo_store.read() == []  # read-only must not publish a cache write
    assert count(db) == 1
    result = call(agent, messages, {"todos": NEXT, "merge": True}, "call-1")
    assert result["todos"] == ITEMS + NEXT
    assert count(db) == 2


def test_real_sqlite_abort_preserves_cache_and_snapshot(execution):
    agent, db = execution
    messages, _ = block(agent, [{"todos": ITEMS}, {"todos": NEXT}])
    assert "error" not in call(agent, messages)
    before = db.get_current_todo_snapshot(agent.session_id)
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER reject_todo BEFORE INSERT ON todo_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'test denial'); END"))
    result = call(agent, messages, {"todos": NEXT}, "call-1")
    assert result["disposition"] == "outcome_unknown"
    assert agent._todo_store.read() == ITEMS
    assert db.get_current_todo_snapshot(agent.session_id) == before
    assert agent._incremental_persistence_failed is True


def test_commit_then_exception_is_not_retried_or_published(execution, monkeypatch):
    agent, db = execution
    messages, _ = block(agent, [{"todos": ITEMS}])
    original = db.commit_todo_snapshot
    def uncertain(**kwargs):
        original(**kwargs)
        raise OSError("lost acknowledgement")
    monkeypatch.setattr(db, "commit_todo_snapshot", uncertain)
    result = call(agent, messages)
    assert result["disposition"] == "outcome_unknown"
    assert count(db) == 1
    assert agent._todo_store.read() == []
    assert agent._incremental_persistence_failed is True


@pytest.mark.parametrize("mode", ["sequential", "segmented"])
def test_committed_state_survives_result_publication_failure(execution, monkeypatch, mode):
    agent, db = execution
    messages, assistant = block(agent, [{"todos": ITEMS}, {"todos": NEXT}])
    events = []
    agent.tool_complete_callback = lambda *a, **kw: events.append(a)
    original = db.append_messages_batch
    def fail_result(*args, **kwargs):
        if any(m.get("role") == "tool" for m in kwargs.get("messages", [])):
            raise OSError("result publication unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "append_messages_batch", fail_result)
    {"sequential": execute_tool_calls_sequential,
     "segmented": execute_tool_calls_segmented}[mode](agent, assistant, messages, agent.session_id)
    assert count(db) == 1
    assert db.get_current_todo_snapshot(agent.session_id)["todos"] == ITEMS
    assert not events
    assert agent._incremental_persistence_failed is True
    assert not any(r["role"] == "tool" for r in db.get_messages(agent.session_id))


@pytest.mark.parametrize("isolated", [False, True])
def test_explicit_memory_only_compatibility(execution, isolated):
    agent, db = execution
    if isolated:
        agent._persist_disabled = True
    else:
        agent._session_db = None
    result = call(agent, [])
    assert result["todos"] == ITEMS
    assert agent._todo_store.read() == ITEMS
    assert count(db) == 0


def test_plugin_denial_precedes_todo_mutation(execution):
    agent, db = execution
    messages, _ = block(agent, [{"todos": ITEMS}])
    with patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=("denied", None)):
        assert "error" in call(agent, messages)
    assert count(db) == 0
    assert agent._todo_store.read() == []
