"""Retirement requires positive settlement; absence is not permission."""
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def parent_with_child(monkeypatch, child):
    from run_agent import AIAgent

    parent = AIAgent.__new__(AIAgent)
    parent.session_id = "retirement-guard-parent"
    parent._active_children_lock = threading.Lock()
    parent._active_children = [child]
    parent.client = object()
    closes = []
    parent.shutdown_memory_provider = lambda *a, **k: None
    parent._close_openai_client = lambda *a, **k: closes.append("parent-client")
    monkeypatch.setattr("run_agent.request_hard_interrupt", lambda *a, **k: True)
    return parent, closes


@pytest.mark.parametrize("state", [None, "incomplete", "unexpected", "complete"])
def test_parent_only_retires_explicitly_settled_children(monkeypatch, state):
    child = SimpleNamespace(_close_cleanup_state="complete")
    if state is not None:
        child._delegate_cleanup_state = state
    child_closes = []
    child.close = lambda: child_closes.append("child")
    parent, closes = parent_with_child(monkeypatch, child)
    parent.close()
    if state == "complete":
        assert child_closes == ["child"]
        assert parent._active_children == []
        assert closes == ["parent-client"]
        assert parent._close_cleanup_state == "complete"
    else:
        assert child_closes == []
        assert parent._active_children == [child]
        assert closes == []
        assert parent._close_cleanup_state == "incomplete"


@pytest.mark.parametrize("outcome", ["incomplete", "missing", "raises"])
def test_child_close_cannot_drop_unresolved_owner(monkeypatch, outcome):
    child = SimpleNamespace(_delegate_cleanup_state="complete")
    calls = []

    def close_child():
        calls.append("child")
        if outcome == "raises":
            raise OSError("controlled close refusal")
        if outcome != "missing":
            child._close_cleanup_state = outcome

    child.close = close_child
    parent, closes = parent_with_child(monkeypatch, child)
    parent.close()
    assert calls == ["child"]
    assert parent._active_children == [child]
    assert parent._close_cleanup_state == "incomplete"
    assert closes == []


def test_child_admitted_during_close_remains_owned(monkeypatch):
    child = SimpleNamespace(_delegate_cleanup_state="complete", _close_cleanup_state="complete")
    replacement = SimpleNamespace()
    parent, closes = parent_with_child(monkeypatch, child)
    child.close = lambda: parent._active_children.append(replacement)
    parent.close()
    assert parent._active_children == [replacement]
    assert parent._close_cleanup_state == "incomplete"
    assert closes == []


@pytest.mark.parametrize("observed", [None, True, 0, MagicMock()])
def test_gateway_only_accepts_literal_false_work_observation(observed):
    from tui_gateway import server

    agent = SimpleNamespace(session_id="guard-scope", _has_owned_tool_work=lambda: observed)
    assert server._session_cleanup_unsettled({"agent": agent}) is True


def test_gateway_accepts_declared_settled_observation():
    from tui_gateway import server

    agent = SimpleNamespace(session_id="guard-scope", _has_owned_tool_work=lambda: False)
    assert server._session_cleanup_unsettled({"agent": agent}) is False


def test_gateway_keeps_work_when_observer_raises():
    from tui_gateway import server

    def fail():
        raise OSError("controlled observer refusal")

    agent = SimpleNamespace(session_id="guard-scope", _has_owned_tool_work=fail)
    assert server._session_cleanup_unsettled({"agent": agent}) is True


def test_mock_generated_method_cannot_hide_retained_work():
    from tui_gateway import server

    agent = MagicMock()
    agent.session_id = "guard-scope"
    agent._owned_tool_work_unknown = True
    # Access manufactures a dynamic callable. It is not a declared protocol.
    agent._has_owned_tool_work.return_value = False
    assert server._session_cleanup_unsettled({"agent": agent}) is True


def test_native_release_failure_cannot_swallow_relay_failure(tmp_path, monkeypatch):
    from agent import relay_runtime
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(tmp_path / "native-release.db")
    db.create_session("retire-exception", source="subagent")
    agent = AIAgent.__new__(AIAgent)
    agent.__dict__.update(
        session_id="retire-exception", platform="subagent", model="harness",
        _session_db=db, _owns_session_db=True, _session_db_created=True,
        _session_init_model_config=None, _cached_system_prompt=None,
        _persist_disabled=False, _parent_session_id=None, _relay_pending_turn_id=None,
        _conversation_root_id=lambda: "retire-exception", log_prefix="",
        _vprint=lambda *a, **kw: None, status_callback=None,
        _interrupt_requested=False, _interrupt_message=None, _pending_redirect=None,
        _execution_thread_id=None, _interrupt_thread_signal_pending=False,
        _reset_activity_labels_after_turn=lambda: None,
    )
    coordinator = relay_runtime.SESSION_COORDINATOR
    turn = SimpleNamespace(relay_enabled=False)
    monkeypatch.setattr(coordinator, "acquire_conversation", lambda **kw: object())
    monkeypatch.setattr(coordinator, "begin_turn", lambda *a, **kw: turn)
    monkeypatch.setattr(coordinator, "finish_logical_calls", lambda *a, **kw: None)
    monkeypatch.setattr(coordinator, "release_conversation", lambda *a, **kw: None)

    def relay_failure(*a, **kw):
        raise RuntimeError("controlled relay retirement failure")

    def release_failure(*a, **kw):
        raise OSError("controlled native lease release failure")

    monkeypatch.setattr(coordinator, "end_turn", relay_failure)
    monkeypatch.setattr(db, "release_session_turn_lease", release_failure)
    monkeypatch.setattr("agent.conversation_loop.run_conversation", lambda *a, **kw: {
        "final_response": "test response", "messages": [],
    })
    try:
        with pytest.raises(RuntimeError, match="controlled relay retirement failure"):
            agent.run_conversation("fixture", conversation_history=[])
        assert agent._owned_turn_retirement_state == "incomplete"
        assert agent._active_session_turn_lease_holder
        assert db._conn.execute("SELECT COUNT(*) FROM session_turn_leases").fetchone()[0] == 1
    finally:
        db.close()


def test_close_fences_child_admission_before_final_resource_retirement(monkeypatch):
    from hermes_cli.delegation_policy import DelegationLifetimePolicy
    from tools import delegate_tool as delegate

    parent, closes = parent_with_child(monkeypatch, SimpleNamespace())
    parent._active_children.clear()
    reached, release = threading.Event(), threading.Event()
    failures = []

    def observe():
        reached.set()
        assert release.wait(3)
        return False

    parent._has_owned_tool_work = observe
    monkeypatch.setattr(delegate, "_get_child_lifetime_policy", lambda: DelegationLifetimePolicy("total", None))
    monkeypatch.setattr(delegate, "_build_child_agent", lambda **kw: pytest.fail("closed parent constructed child"))

    def close_parent():
        try:
            parent.close()
        except BaseException as exc:
            failures.append(exc)

    closer = threading.Thread(target=close_parent)
    closer.start()
    try:
        assert reached.wait(3)
        assert parent._close_cleanup_state == "incomplete"
        with pytest.raises(ValueError, match="DELEGATION_PARENT_CLOSING"):
            delegate._build_child_preserving_parent_tools(parent_agent=parent, goal="forbidden")
        assert closes == []
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive() and not failures
    assert closes == ["parent-client"]


@pytest.mark.parametrize("child_close_fails", [False, True])
def test_initialization_reservation_prevents_parent_retirement(monkeypatch, child_close_fails):
    from hermes_cli.delegation_policy import DelegationLifetimePolicy
    from tools import delegate_tool as delegate

    parent, closes = parent_with_child(monkeypatch, SimpleNamespace())
    parent._active_children.clear()
    child = SimpleNamespace(_subagent_id="reserved-child-fixture")
    child_closes = []

    def close_child():
        child_closes.append("child")
        if child_close_fails:
            raise OSError("controlled initialization cleanup refusal")
        child._close_cleanup_state = "complete"

    child.close = close_child

    def initialize(**kw):
        # The constructor has not yet returned a child, but the canonical
        # reservation must already charge the parent and prevent retirement.
        parent.close()
        assert parent._close_cleanup_state == "incomplete"
        assert closes == []
        return child

    monkeypatch.setattr(delegate, "_get_child_lifetime_policy", lambda: DelegationLifetimePolicy("total", None))
    monkeypatch.setattr(delegate, "_build_child_agent", initialize)
    caller_done = Future()
    retirement_done, parent_retry_done = threading.Event(), threading.Event()
    original_retire = delegate._retire_unstarted_child
    original_parent_close = parent.close

    def observed_retire(*args, **kwargs):
        try:
            return original_retire(*args, **kwargs)
        finally:
            retirement_done.set()

    def observed_parent_close():
        original_parent_close()
        parent_retry_done.set()

    monkeypatch.setattr(delegate, '_retire_unstarted_child', observed_retire)
    parent.close = observed_parent_close
    with pytest.raises(ValueError, match="DELEGATION_PARENT_CLOSING"):
        delegate._build_child_preserving_parent_tools(parent_agent=parent, goal="construction race",
            _caller_initialization_done=caller_done)
    assert child_closes == []
    assert closes == []
    parent_retry_done.clear()
    caller_done.set_result(None)
    assert retirement_done.wait(3), 'acknowledged construction cleanup never settled'
    if not child_close_fails:
        assert parent_retry_done.wait(3)
    assert child_closes == ["child"]
    with delegate._active_subagents_lock:
        retained = {key: row for key, row in delegate._active_subagents.items()
                    if row.get("owner_agent_session_id") == parent.session_id}
    if child_close_fails:
        assert len(retained) == 1
        reservation, row = next(iter(retained.items()))
        assert row["capacity_occupied"] is True and row["agent"] is child
        child.close = lambda: setattr(child, "_close_cleanup_state", "complete")
        assert delegate._retire_unstarted_child(child, parent, reservation_id=reservation)
    else:
        assert retained == {}
    parent.close()
    assert closes == ["parent-client"]


def test_lightweight_nonresource_record_does_not_crash_cleanup():
    from tui_gateway import server

    assert server._session_cleanup_unsettled({"agent": object()}) is False
