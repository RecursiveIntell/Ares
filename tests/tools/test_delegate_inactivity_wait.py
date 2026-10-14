"""C04 wait-owner qualification; no provider, live config or inactivity enablement.

Real progress witnesses and futures, controlled clocks. These tests cover the
consumer side only; production emitters remain a separate admission gate.
"""
from concurrent.futures import Future
import threading

import pytest

from agent.progress_witness import InactivityLimits, InactivityVerdict, ProgressWitness
from tools import delegate_tool as delegate


@pytest.fixture
def progress():
    clock = [0.0]
    witness = ProgressWitness(generation='admitted',
        limits=InactivityLimits(stall_seconds=5, request_seconds=10,
                                tool_seconds=12, total_ceiling_seconds=20),
        clock=lambda: clock[0])
    return witness, clock


def empty_wait(monkeypatch):
    calls = []
    def wait(futures, timeout):
        calls.append(timeout)
        return set(), set(futures)
    monkeypatch.setattr(delegate, 'wait_futures', wait)
    return calls


@pytest.mark.parametrize('operation, now, expected', [
    ('none', 6, 'stalled'), ('request', 11, 'request_limit'),
    ('tool', 13, 'tool_limit'), ('none', 21, 'ceiling'),
])
def test_wait_owner_consumes_distinct_inactivity_reasons(monkeypatch, progress, operation, now, expected):
    witness, clock = progress
    if operation == 'request':
        witness.open_request('real-request', generation='admitted', streaming=False)
    elif operation == 'tool':
        witness.open_tool('real-tool', generation='admitted')
    clock[0] = now
    waits = empty_wait(monkeypatch)
    result = delegate._wait_child_attempt(Future(), None, progress_witness=witness)
    assert type(result) is InactivityVerdict
    assert result.state == expected
    assert 0 < waits[0] <= 0.05


def test_completed_set_wins_over_expired_progress(monkeypatch, progress):
    witness, clock = progress
    clock[0] = 100
    future = Future()
    future.set_result('done')
    monkeypatch.setattr(witness, 'evaluate', lambda: pytest.fail('completed work consulted stale progress'))
    assert delegate._wait_child_attempt(future, None, progress_witness=witness) is delegate.ChildWaitOutcome.COMPLETED


def test_explicit_stop_wins_over_progress_cutoff(monkeypatch, progress):
    witness, clock = progress
    clock[0] = 100
    stop = threading.Event()
    stop.set()
    empty_wait(monkeypatch)
    assert delegate._wait_child_attempt(Future(), None, stop, progress_witness=witness) is delegate.ChildWaitOutcome.STOPPED


def test_total_deadline_keeps_its_existing_precedence(monkeypatch, progress):
    witness, clock = progress
    clock[0] = 100
    monkeypatch.setattr(delegate.time, 'monotonic', lambda: 100)
    empty_wait(monkeypatch)
    assert delegate._wait_child_attempt(Future(), 90, progress_witness=witness) is delegate.ChildWaitOutcome.EXPIRED


def test_qualified_delta_prevents_stall_without_resetting_total(monkeypatch, progress):
    witness, clock = progress
    witness.open_request('request', generation='admitted', streaming=True)
    clock[0] = 6
    future = Future()
    calls = []
    def wait(futures, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            assert witness.record_model_delta('request', 10, generation='admitted')
            return set(), {future}
        future.set_result('validated done')
        return {future}, set()
    monkeypatch.setattr(delegate, 'wait_futures', wait)
    assert delegate._wait_child_attempt(future, None, progress_witness=witness) is delegate.ChildWaitOutcome.COMPLETED
    assert len(calls) == 2
    assert witness.admitted_at == 0


def test_closed_progress_is_unavailable_not_completion(monkeypatch, progress):
    witness, _ = progress
    witness.close()
    empty_wait(monkeypatch)
    result = delegate._wait_child_attempt(Future(), None, progress_witness=witness)
    assert result.state == 'unavailable'
    assert result.detail['reason'] == 'closed_progress_owner'


@pytest.mark.parametrize('invalid', [object(), {'state': 'ok'}, True])
def test_unqualified_progress_object_is_not_borrowed(monkeypatch, invalid):
    empty_wait(monkeypatch)
    result = delegate._wait_child_attempt(Future(), None, progress_witness=invalid)
    assert result.state == 'unavailable'
    assert result.detail['reason'] == 'invalid_progress_owner'


def test_progress_observer_failure_is_unknown_not_pass(monkeypatch, progress):
    witness, _ = progress
    empty_wait(monkeypatch)
    def broken():
        raise OSError('neutral observer failure')
    monkeypatch.setattr(witness, 'evaluate', broken)
    result = delegate._wait_child_attempt(Future(), None, progress_witness=witness)
    assert result.state == 'unavailable'
    assert result.detail['reason'] == 'progress_observation_failed'


@pytest.mark.parametrize('value', [None, True, -1, float('nan'), float('inf'), 10 ** 400])
def test_bad_cutoff_limit_returns_unknown_instead_of_escaping_owner(monkeypatch, progress, value):
    witness, _ = progress
    empty_wait(monkeypatch)
    monkeypatch.setattr(witness, 'evaluate', lambda: InactivityVerdict('stalled', {'limit': value}))
    result = delegate._wait_child_attempt(Future(), None, progress_witness=witness)
    assert result.state == 'unavailable'
    assert result.detail['reason'] == 'invalid_progress_limit'


def test_total_mode_without_progress_keeps_unbounded_completed_wait(monkeypatch):
    future = Future()
    seen = []
    def wait(futures, timeout):
        seen.append(timeout)
        future.set_result('done')
        return {future}, set()
    monkeypatch.setattr(delegate, 'wait_futures', wait)
    assert delegate._wait_child_attempt(future, None) is delegate.ChildWaitOutcome.COMPLETED
    assert seen == [None]


@pytest.mark.parametrize('observer_failure', [False, True])
def test_live_worker_cutoff_retains_owner_until_actual_settlement(monkeypatch, progress, observer_failure):
    from types import SimpleNamespace
    from hermes_cli.delegation_policy import DelegationLifetimePolicy
    from tests.tools.test_delegate_attempt_deadline import Child

    witness, clock = progress
    entered, release = threading.Event(), threading.Event()
    def run(**kwargs):
        entered.set()
        clock[0] = 6
        assert release.wait(30), 'test owner failed to release retained worker'
        return {'final_response': 'neutral completed', 'api_calls': 1}
    child = Child(run)
    child._delegate_timeout = None
    child._delegate_lifetime_policy = DelegationLifetimePolicy('inactivity', None)
    child._delegate_progress_witness = witness
    child._delegate_progress_generation = 'admitted'
    parent = SimpleNamespace(session_id='inactivity-unit-parent', _current_task_id=None,
        _active_children=[child], _active_children_lock=threading.Lock())
    registry = {child._subagent_id: {'agent': child, 'owner_agent_session_id': parent.session_id,
        'capacity_occupied': True, 'accepting_steer': True}}
    monkeypatch.setattr(delegate, '_active_subagents', registry)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    if observer_failure:
        evaluate = witness.evaluate
        def fail_only_after_worker_entered():
            if entered.is_set():
                raise OSError('neutral observer failure')
            return evaluate()
        monkeypatch.setattr(witness, 'evaluate', fail_only_after_worker_entered)
    try:
        result = delegate._run_single_child(0, 'neutral', child, parent)
        assert entered.is_set()
        assert result['status'] == ('error' if observer_failure else 'timeout')
        assert result['error_origin'] == ('progress_owner' if observer_failure else 'parent_inactivity')
        assert result['cleanup']['status'] == 'incomplete'
        assert registry[child._subagent_id]['capacity_occupied'] is True
        assert child in parent._active_children
        assert not child.closed.is_set()
        assert witness.snapshot().closed is True
        if not observer_failure:
            assert result['progress_cutoff'] == {'state': 'stalled', 'limit_seconds': 5.0}
            assert result['timeout_seconds'] == 5.0
    finally:
        release.set()
    assert child.closed.wait(5)
    assert child._delegate_settled_event.wait(5)
    assert child._subagent_id not in registry
    assert child not in parent._active_children


def test_generation_mismatch_refuses_before_worker_and_never_closes_foreign_witness(monkeypatch, progress):
    from hermes_cli.delegation_policy import DelegationLifetimePolicy
    from tests.tools.test_delegate_attempt_deadline import Child
    witness, _ = progress
    calls = []
    child = Child(lambda **kwargs: calls.append(kwargs) or {'final_response': 'forbidden'})
    child._delegate_timeout = None
    child._delegate_lifetime_policy = DelegationLifetimePolicy('inactivity', None)
    child._delegate_progress_witness = witness
    child._delegate_progress_generation = 'different-generation'
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    result = delegate._run_single_child(0, 'neutral', child, None)
    assert result['status'] == 'error'
    assert 'DELEGATION_PROGRESS_UNAVAILABLE' in result['error']
    assert calls == []
    assert witness.snapshot().closed is False


def test_total_policy_never_borrows_attached_progress_witness(monkeypatch, progress):
    from hermes_cli.delegation_policy import DelegationLifetimePolicy
    from tests.tools.test_delegate_attempt_deadline import Child
    witness, clock = progress
    clock[0] = 100
    child = Child(lambda **kwargs: {'final_response': 'neutral completed', 'api_calls': 1})
    child._delegate_timeout = None
    child._delegate_lifetime_policy = DelegationLifetimePolicy('total', None)
    child._delegate_progress_witness = witness
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    result = delegate._run_single_child(0, 'neutral', child, None)
    assert result['status'] == 'completed'
    assert witness.snapshot().closed is False
