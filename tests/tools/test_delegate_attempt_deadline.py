import threading
from types import SimpleNamespace

import pytest

from tools import delegate_tool as delegate


class Child:
    def __init__(self, run):
        self._subagent_id = 'sa-deadline-test'
        self.session_id = self._subagent_id
        self._delegate_role = 'leaf'
        self.run = run
        self.tool_progress_callback = None
        self.closed = threading.Event()

    def run_conversation(self, **kwargs):
        return self.run(**kwargs)

    def interrupt(self, message=None, **kwargs):
        pass

    def close(self):
        self.closed.set()

    def get_activity_summary(self):
        return {'api_call_count': 1}


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    parent = SimpleNamespace(session_id='parent', _current_task_id=None,
        _active_children=[], _active_children_lock=threading.Lock())

    def call(child):
        parent._active_children.append(child)
        return delegate._run_single_child(0, 'test', child, parent)
    return call


@pytest.mark.parametrize('cap', [None, 30.0])
def test_child_timeout_exception_keeps_child_origin(monkeypatch, invoke, cap):
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: cap)
    def run(**kwargs):
        raise TimeoutError('worker timeout, not parent deadline')
    result = invoke(Child(run))
    assert result['status'] == 'error'
    assert result['timeout_seconds'] is None
    assert result['attempt_outcome'] == 'completed'
    assert result['error_origin'] == 'child'


def test_schema_repair_has_no_fresh_budget(monkeypatch, invoke):
    import model_tools  # noqa: F401
    import tools.delegation_output_schema  # noqa: F401
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: 0.2)
    # The policy clock is controlled separately from thread scheduling. A
    # 0.2-second wall-clock budget could expire during schema-validator import
    # before the repair worker even existed, testing the wrong boundary.
    clock_lock = threading.Lock()
    clock = [100.0]
    real_time = delegate.time
    def monotonic():
        with clock_lock:
            return clock[0]
    def advance(value):
        with clock_lock:
            clock[0] = value
    monkeypatch.setattr(delegate, 'time', SimpleNamespace(
        monotonic=monotonic, time=real_time.time, sleep=real_time.sleep))
    calls = []
    first_done = threading.Event()
    repair_entered = threading.Event()
    repair_release = threading.Event()
    def run(**kwargs):
        calls.append(kwargs['user_message'])
        if len(calls) == 1:
            advance(100.15)  # first attempt consumed most of the single budget
            first_done.set()
            return {'final_response': '{"x":"wrong"}', 'api_calls': 1}
        repair_entered.set()
        advance(100.25)  # exceed original deadline, not a fresh repair budget
        assert repair_release.wait(30), 'test owner never released repair worker'
        return {'final_response': '{"x":1}', 'api_calls': 1}
    child = Child(run)
    child._delegate_output_schema = {'type': 'object', 'properties': {'x': {'type': 'integer'}}, 'required': ['x']}
    try:
        result = invoke(child)
        assert first_done.is_set()
        assert repair_entered.is_set() and len(calls) == 2
        assert result['status'] == 'timeout'
        assert result['attempt_phase'] == 'schema_repair'
        assert result['cleanup']['status'] == 'incomplete'
        assert not child.closed.is_set()
    finally:
        repair_release.set()
    assert child.closed.wait(3)


def test_expired_initialization_budget_never_starts_first_turn(monkeypatch, invoke):
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: 0.1)
    calls = []
    child = Child(lambda **kw: calls.append(kw) or {'final_response': 'late'})
    child._delegate_admitted_at = delegate.time.monotonic() - 1
    child._delegate_deadline = child._delegate_admitted_at + 0.1
    result = invoke(child)
    assert result['status'] == 'timeout'
    assert not calls


def test_wait_completed_set_controls_dual_fire_order(monkeypatch):
    from concurrent.futures import Future
    future = Future()
    future.set_exception(TimeoutError('child-origin'))
    # The owner observes expiry before the late child error; neither done()
    # nor the future's exception can revise that decision.
    monkeypatch.setattr(delegate, 'wait_futures', lambda *a, **kw: (set(), {future}))
    assert delegate._wait_child_attempt(future, 0) is delegate.ChildWaitOutcome.EXPIRED
    monkeypatch.setattr(delegate, 'wait_futures', lambda *a, **kw: ({future}, set()))
    assert delegate._wait_child_attempt(future, 0) is delegate.ChildWaitOutcome.COMPLETED
    with pytest.raises(TimeoutError, match='child-origin'):
        future.result()


def test_initialization_retained_at_ceiling_and_late_child_never_runs(monkeypatch):
    import model_tools  # noqa: F401
    release = threading.Event()
    entered = threading.Event()
    closed = threading.Event()
    calls = []
    child = Child(lambda **kw: calls.append(kw) or {})
    child.close = closed.set
    parent = SimpleNamespace(session_id='init-parent', _active_children=[], _active_children_lock=threading.Lock())
    def build(**kwargs):
        entered.set()
        release.wait(5)
        parent._active_children.append(child)
        return child
    monkeypatch.setattr(delegate, '_get_child_lifetime_policy',
        lambda: delegate.DelegationLifetimePolicy('total', 0.1))
    monkeypatch.setattr(delegate, '_get_max_concurrent_children', lambda: 1)
    monkeypatch.setattr(delegate, '_build_child_agent', build)
    try:
        with pytest.raises(ValueError, match='DELEGATION_DEADLINE_EXPIRED'):
            delegate._build_child_preserving_parent_tools(goal='test', parent_agent=parent)
        assert entered.is_set()
        with pytest.raises(ValueError, match='DELEGATION_CAPACITY_OCCUPIED'):
            delegate._build_child_preserving_parent_tools(goal='refused', parent_agent=parent)
        assert any(r.get('owner_agent_session_id') == 'init-parent' and r['capacity_occupied']
            for r in delegate.list_active_subagents())
    finally:
        release.set()
    assert closed.wait(3)
    assert not calls


def test_deadline_returns_while_owned_initialization_is_blocked(monkeypatch, invoke):
    import model_tools  # noqa: F401
    import agent.delegation_context  # noqa: F401
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls, results = [], []
    def setup():
        entered.set()
        release.wait(5)
        return False
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: 0.1)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', setup)
    child = Child(lambda **kw: calls.append(kw) or {'final_response': 'unauthorized late start'})
    def call():
        results.append(invoke(child))
        returned.set()
    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    assert entered.wait(2)
    try:
        assert returned.wait(3), 'owned initialization defeated the admitted deadline'
        assert results[0]['status'] == 'timeout'
        assert results[0]['attempt_phase'] == 'initialization'
        assert results[0]['cleanup']['status'] == 'incomplete'
        assert not child.closed.is_set()
    finally:
        release.set()
        caller.join(2)
    assert child.closed.wait(3)
    assert not calls
