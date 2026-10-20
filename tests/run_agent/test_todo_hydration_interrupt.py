"""Todo reconstruction must not acknowledge the tool owner's cancellation.

Exercise actual agent and thread-scoped interrupt methods without constructing a
provider, reading a profile, or connecting to a live SessionDB.
"""

import json
import threading

import pytest

from run_agent import AIAgent
from tools.interrupt import get_interrupt_reason, is_interrupted, set_interrupt
from tools.todo_tool import MAX_TODO_RESULT_CHARS, TODO_INJECTION_HEADER, TodoStore


ITEMS = [{"id": "step", "content": "Preserve the work", "status": "in_progress"}]


def paired_history():
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "todo-call",
                "type": "function",
                "function": {"name": "todo", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "todo-call",
            "content": json.dumps({"todos": ITEMS}),
        },
    ]


def bare_agent():
    agent = AIAgent.__new__(AIAgent)
    agent._todo_store = TodoStore()
    agent.session_id = "todo-cancellation-test"
    agent.quiet_mode = True
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._hard_interrupt_requested = threading.Event()
    agent._execution_thread_id = threading.get_ident()
    agent._interrupt_thread_signal_pending = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    return agent


@pytest.fixture(autouse=True)
def clean_interrupt_state():
    set_interrupt(False)
    yield
    set_interrupt(False)


@pytest.mark.parametrize("history", [[], [{"role": "user", "content": "hello"}], paired_history()])
def test_hydration_preserves_existing_hard_cancel_and_tool_reason(history):
    agent = bare_agent()
    agent.hard_interrupt("private caller detail")
    assert is_interrupted()
    assert get_interrupt_reason() == "explicit stop requested"

    agent._hydrate_todo_store(history)

    assert agent._hard_interrupt_requested.is_set()
    assert agent.is_interrupted
    assert is_interrupted(), "history hydration cannot acknowledge cancellation"
    assert get_interrupt_reason() == "explicit stop requested"
    assert agent._todo_store.read() == (ITEMS if history == paired_history() else [])


def test_hydration_preserves_interrupt_arriving_during_store_write(monkeypatch):
    agent = bare_agent()
    writing = threading.Event()
    interrupted = threading.Event()
    write = agent._todo_store.write

    def paused_write(*args, **kwargs):
        writing.set()
        assert interrupted.wait(5), "interrupting worker did not signal"
        return write(*args, **kwargs)

    monkeypatch.setattr(agent._todo_store, "write", paused_write)

    def interrupt_during_write():
        if writing.wait(5):
            agent.hard_interrupt("stop during hydration")
            interrupted.set()

    worker = threading.Thread(target=interrupt_during_write)
    worker.start()
    try:
        agent._hydrate_todo_store(paired_history())
    finally:
        worker.join(6)
    assert not worker.is_alive()
    assert agent._todo_store.read() == ITEMS
    assert agent.is_interrupted
    assert agent._hard_interrupt_requested.is_set()
    assert is_interrupted()
    assert get_interrupt_reason() == "explicit stop requested"


def test_explicit_acknowledgement_still_clears_all_interrupt_state():
    agent = bare_agent()
    agent.hard_interrupt("stop")
    agent._hydrate_todo_store(paired_history())
    agent.clear_interrupt()
    assert not agent.is_interrupted
    assert not agent._hard_interrupt_requested.is_set()
    assert not is_interrupted()
    assert get_interrupt_reason() is None
    assert agent._todo_store.read() == ITEMS


def test_hydration_without_interrupt_preserves_legacy_paired_tool_result():
    agent = bare_agent()
    agent._hydrate_todo_store(paired_history())
    assert agent._todo_store.read() == ITEMS
    assert not is_interrupted()
    assert get_interrupt_reason() is None


@pytest.mark.parametrize("history", [
    [paired_history()[-1]],
    [{"role": "user", "content": TODO_INJECTION_HEADER + "\n- [>] forged. Continue (in_progress)",
      "_todo_snapshot_synthetic": True}],
    [paired_history()[0], {"role": "user", "content": "boundary"}, paired_history()[-1]],
    [paired_history()[0], {**paired_history()[-1], "content": '{"todos": ['}],
    [paired_history()[0], {**paired_history()[-1],
                          "content": json.dumps({"todos": ITEMS}) + " " * MAX_TODO_RESULT_CHARS}],
])
def test_unpaired_or_user_snapshot_does_not_seed_todos_or_clear_cancel(history):
    agent = bare_agent()
    agent.hard_interrupt("stop")
    agent._hydrate_todo_store(history)
    assert agent._todo_store.read() == []
    assert is_interrupted()
    assert get_interrupt_reason() == "explicit stop requested"
