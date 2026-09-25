import json
from types import SimpleNamespace

import pytest

from ares_runtime.continuity.runtime import (
    AutomaticRebaseStatus,
    attempt_turn_start_context_rebase,
)
from hermes_state import SessionDB
from hermes_cli.goals import GoalState
from hermes_cli.heartbeat import HeartbeatState
from hermes_cli.loops import LoopState


@pytest.fixture
def setup(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(
        "s0",
        source="cli",
        profile_name="p1",
        model="test",
        cwd=str(tmp_path),
    )
    db.append_message("s0", "user", "Implement the queue fix. Do not push or merge.")
    db.append_message(
        "s0",
        "assistant",
        "Current state: queue.py was inspected; regression is still not run.",
        _compressed_summary=True,
    )
    assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)

    history = db.get_messages_as_conversation(
        "s0", repair_alternation=True, include_row_ids=True
    )
    current = {"role": "user", "content": "Run the concurrency regression next."}
    messages = [*history, current]

    transitions = []

    def flush(rows, conversation_history=None):
        # Persist the latest live user row under the current physical session,
        # matching the production flush boundary without replaying the durable
        # historical prefix.
        latest_user = next(
            (row for row in reversed(rows) if row.get("role") == "user"),
            None,
        )
        if latest_user is None:
            return True
        target = agent.session_id
        existing = db.get_messages_as_conversation(target)
        if not any(
            row.get("role") == "user"
            and row.get("content") == latest_user.get("content")
            for row in existing
        ):
            db.append_message(
                target,
                "user",
                latest_user.get("content"),
                display_kind=latest_user.get("display_kind"),
                display_metadata=latest_user.get("display_metadata"),
            )
        return True

    def transition_engine(**kwargs):
        transitions.append(kwargs)

    agent = SimpleNamespace(
        context_rebase_enabled=True,
        _session_db=db,
        session_id="s0",
        _active_session_turn_lease_holder="holder",
        _active_session_turn_lease_ttl_seconds=300.0,
        _run_checkpoint_custody=None,
        _flush_messages_to_session_db=flush,
        _transition_context_engine_session=transition_engine,
        context_compressor=SimpleNamespace(threshold_tokens=50_000),
        tools=[],
        api_mode="chat_completions",
        model="test",
        platform="cli",
        _session_db_created=True,
        _cached_system_prompt="trusted base prompt",
        _flushed_db_message_ids=set(),
        _last_flushed_db_idx=0,
        _flushed_db_message_session_id="s0",
    )
    yield db, agent, messages, history, transitions
    db.close()


def test_disabled_gate_is_noop_without_persisting_or_publishing(setup):
    db, agent, messages, history, _ = setup
    agent.context_rebase_enabled = False
    before = db._conn.total_changes
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.SKIPPED
    assert result.reason == "CONTEXT_REBASE_DISABLED"
    assert db._conn.total_changes == before
    assert db.get_session("s0")["ended_at"] is None


def test_unqualified_stateful_provider_is_blocked_before_flush(setup):
    db, agent, messages, history, _ = setup
    agent.api_mode = "codex_app_server"
    before = db._conn.total_changes
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.BLOCKED
    assert result.reason == "PROVIDER_CONTEXT_RESET_UNQUALIFIED"
    assert db._conn.total_changes == before
    assert db.get_session("s0")["ended_at"] is None


def test_turn_start_rebase_publishes_rebinds_and_marks_ready(setup):
    db, agent, messages, history, transitions = setup
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.READY
    assert result.after_tokens < result.before_tokens
    assert agent.session_id == result.session_id
    assert db.get_session("s0")["end_reason"] == "context_rebase"
    child = db.get_session(result.session_id)
    assert child is not None and child["ended_at"] is None
    transition = db.read_context_rebase_transition(result.transition_id)
    assert transition.state == "ready"
    assert transitions[-1]["old_session_id"] == "s0"
    assert transitions[-1]["new_session_id"] == result.session_id
    assert transitions[-1]["extra_context"]["boundary_reason"] == "context_rebase"
    assert transitions[-1]["extra_context"]["session_db"] is db
    child_rows = db.get_messages_as_conversation(result.session_id)
    assert child_rows[-1]["role"] == "user"
    assert child_rows[-1]["content"] == "Run the concurrency regression next."
    assert "Do not push or merge." in child_rows[0]["content"]


def test_post_publish_owner_failure_is_durable_reconciliation_required(setup):
    db, agent, messages, history, _ = setup

    def fail_transition(**_kwargs):
        raise RuntimeError("simulated owner failure")

    agent._transition_context_engine_session = fail_transition
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    assert db.get_session("s0")["end_reason"] == "context_rebase"
    transition = db.read_context_rebase_transition(result.transition_id)
    assert transition.state == "reconciliation_required"
    assert transition.child_session_id == result.session_id


def test_candidate_without_meaningful_reduction_does_not_close_parent(setup):
    db, agent, messages, history, _ = setup
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=1,
    )
    assert result.status is AutomaticRebaseStatus.BLOCKED
    assert result.reason in {
        "SUCCESSOR_NO_REDUCTION",
        "SUCCESSOR_INSUFFICIENT_REDUCTION",
    }
    assert db.get_session("s0")["ended_at"] is None


def test_ready_rebase_carries_goal_recurring_state_and_title(setup):
    db, agent, messages, history, _ = setup
    goal = GoalState(goal="Fix the queue", created_at=1.0)
    db.set_meta("goal:s0", goal.to_json())
    db.set_meta(
        "heartbeat:s0",
        HeartbeatState("check queue", 600, created_at=1.0).to_json(),
    )
    db.set_meta(
        "loop:s0",
        LoopState("keep testing", interval_seconds=60, created_at=1.0).to_json(),
    )
    assert db.set_session_title("s0", "Queue repair")

    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.READY

    parent_goal = GoalState.from_json(db.get_meta("goal:s0"))
    child_goal = GoalState.from_json(db.get_meta(f"goal:{result.session_id}"))
    assert parent_goal.status == "cleared"
    assert child_goal.status == "active"
    assert HeartbeatState.from_json(
        db.get_meta(f"heartbeat:{result.session_id}")
    ).status == "active"
    assert LoopState.from_json(db.get_meta(f"loop:{result.session_id}")).status == "active"
    assert db.get_session_title("s0") is None
    assert db.get_session_title(result.session_id) == "Queue repair"


def test_multimodal_successor_is_not_published_without_qualified_accounting(setup):
    db, agent, _messages, _history, _ = setup
    db.append_message(
        "s0",
        "user",
        [
            {
                "type": "image_url",
                "image_url": {"url": "https://example.invalid/short-ref.png"},
            },
            {"type": "text", "text": "Inspect this image and continue."},
        ],
    )
    messages = db.get_messages_as_conversation(
        "s0", repair_alternation=False, include_row_ids=True
    )
    before = db.get_session("s0")
    result = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=messages[:-1],
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert result.status is AutomaticRebaseStatus.BLOCKED
    assert result.reason == "SUCCESSOR_MULTIMODAL_ACCOUNTING_UNQUALIFIED"
    after = db.get_session("s0")
    assert after["ended_at"] == before["ended_at"] is None


def test_runtime_blocks_third_rebase_without_provider_recovery(setup):
    db, agent, messages, history, _ = setup
    agent.context_rebase_max_no_progress = 2

    first = attempt_turn_start_context_rebase(
        agent,
        messages,
        conversation_history=history,
        active_system_prompt=agent._cached_system_prompt,
        before_tokens=100_000,
    )
    assert first.ready
    assert db.read_context_rebase_episode(agent.session_id).attempts_without_recovery == 1

    second = attempt_turn_start_context_rebase(
        agent,
        list(first.messages),
        conversation_history=list(first.messages),
        active_system_prompt=first.system_prompt,
        before_tokens=100_000,
    )
    assert second.ready
    assert db.read_context_rebase_episode(agent.session_id).attempts_without_recovery == 2

    third = attempt_turn_start_context_rebase(
        agent,
        list(second.messages),
        conversation_history=list(second.messages),
        active_system_prompt=second.system_prompt,
        before_tokens=100_000,
    )
    assert third.status is AutomaticRebaseStatus.BLOCKED
    assert third.reason == "NO_PROGRESS_REBASE_LIMIT"
    assert db.get_session(agent.session_id)["ended_at"] is None


def test_twelve_runtime_rebases_preserve_requirement_after_each_recovery(tmp_path):
    db = SessionDB(db_path=tmp_path / "long.db")
    db.create_session(
        "s0", source="cli", profile_name="p1", model="test", cwd=str(tmp_path)
    )
    db.append_message(
        "s0", "user",
        "Implement the queue fix. Do not push or merge under any circumstance.",
    )
    db.append_message(
        "s0", "assistant", "Initial verified state.", _compressed_summary=True
    )
    db.append_message("s0", "user", "Continue the exact task.")
    assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
    db.set_meta("goal:s0", GoalState(goal="Fix queue", created_at=1.0).to_json())

    agent = SimpleNamespace(
        context_rebase_enabled=True,
        context_rebase_max_no_progress=2,
        _session_db=db,
        session_id="s0",
        _active_session_turn_lease_holder="holder",
        _active_session_turn_lease_ttl_seconds=300.0,
        _run_checkpoint_custody=None,
        _flush_messages_to_session_db=lambda *_args, **_kwargs: True,
        _transition_context_engine_session=lambda **_kwargs: None,
        context_compressor=SimpleNamespace(threshold_tokens=50_000),
        tools=[],
        api_mode="chat_completions",
        provider="test",
        model="test",
        base_url="",
        platform="cli",
        _session_db_created=True,
        _cached_system_prompt="trusted base prompt",
        _flushed_db_message_ids=set(),
        _last_flushed_db_idx=0,
        _flushed_db_message_session_id="s0",
    )

    messages = db.get_messages_as_conversation(
        "s0", repair_alternation=True, include_row_ids=True
    )
    system_prompt = agent._cached_system_prompt
    transition_ids = []
    for _index in range(12):
        result = attempt_turn_start_context_rebase(
            agent,
            list(messages),
            conversation_history=list(messages),
            active_system_prompt=system_prompt,
            before_tokens=100_000,
        )
        assert result.ready, result.reason
        transition_ids.append(result.transition_id)
        messages = list(result.messages)
        system_prompt = result.system_prompt
        assert "Do not push or merge under any circumstance." in messages[0]["content"]
        assert db.read_context_rebase_episode(
            agent.session_id
        ).attempts_without_recovery == 1
        # This represents the provider-confirmed prompt-below-threshold event.
        db.reset_context_rebase_episode(agent.session_id)
        assert db.read_context_rebase_episode(
            agent.session_id
        ).attempts_without_recovery == 0

    assert len(set(transition_ids)) == 12
    assert db.get_context_continuation_tip("s0", max_depth=20) == agent.session_id
    assert json.loads(db.get_session(agent.session_id)["model_config"])[
        "_context_epoch"
    ] == 12
    active_goals = []
    for key, raw in db.list_meta_prefix("goal:"):
        state = GoalState.from_json(raw)
        if state.status != "cleared":
            active_goals.append((key, state.goal_id))
    assert len(active_goals) == 1
    assert active_goals[0][0] == f"goal:{agent.session_id}"
    db.close()
