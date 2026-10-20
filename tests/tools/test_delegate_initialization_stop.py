"""Parent Stop and close must fence owned child construction."""

from concurrent.futures import Future
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from tools import delegate_tool as delegate


class Child:
    def __init__(self):
        self._subagent_id = 'sa-init-stop-' + uuid.uuid4().hex
        self.session_id = self._subagent_id
        self.closed = threading.Event()
        self.conversation_calls = []

    def interrupt(self, message=None, **kwargs):
        pass

    def close(self):
        self.closed.set()

    def run_conversation(self, **kwargs):
        self.conversation_calls.append(kwargs)
        return {'final_response': 'unexpected conversation'}


def _agent_parent(session_id):
    from run_agent import AIAgent

    parent = AIAgent.__new__(AIAgent)
    parent.session_id = session_id
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._pending_redirect_lock = threading.Lock()
    parent._interrupt_requested = False
    parent._interrupt_message = None
    parent._tool_interrupt_reason = None
    parent._pending_redirect = None
    parent._execution_thread_id = None
    parent._tool_worker_threads = None
    parent._tool_worker_threads_lock = None
    parent._active_request_abort = None
    parent.quiet_mode = True
    return parent


def _install_policy(monkeypatch, timeout, *, capacity=2):
    monkeypatch.setattr(delegate, '_get_child_lifetime_policy',
        lambda: delegate.DelegationLifetimePolicy('total', timeout))
    monkeypatch.setattr(delegate, '_get_max_concurrent_children', lambda: capacity)


def _owned_records(session_id):
    with delegate._active_subagents_lock:
        return {sid: dict(record) for sid, record in delegate._active_subagents.items()
            if record.get('owner_agent_session_id') == session_id}


def _wait_until_removed(session_id, reservation):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if reservation not in _owned_records(session_id):
            return True
        time.sleep(0.01)
    return reservation not in _owned_records(session_id)


def _wait_until_initialization_settles(session_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(record.get('status') in {'initializing', 'cleanup-incomplete'}
                for record in _owned_records(session_id).values()):
            return True
        time.sleep(0.01)
    return not any(record.get('status') in {'initializing', 'cleanup-incomplete'}
        for record in _owned_records(session_id).values())


def _cleanup_owner_records(session_id):
    with delegate._active_subagents_lock:
        for sid in [sid for sid, record in delegate._active_subagents.items()
                if record.get('owner_agent_session_id') == session_id]:
            delegate._active_subagents.pop(sid, None)


def _run_builder(parent, result, returned):
    caller_done = Future()
    try:
        result.append(('child', delegate._build_child_preserving_parent_tools(
            goal='bounded initialization stop test', parent_agent=parent,
            _caller_initialization_done=caller_done)))
    except BaseException as exc:
        result.append(('error', exc))
    finally:
        # This caller has recorded the construction result/error.
        caller_done.set_result(None)
        returned.set()


def _interrupt_then_reset_flag(parent):
    parent.interrupt()
    with parent._pending_redirect_lock:
        parent._interrupt_requested = False


@pytest.mark.parametrize('late_outcome', ['child', 'constructor_error'])
def test_parent_stop_returns_while_initialization_is_blocked_and_retains_own_reservation(
        monkeypatch, late_outcome):
    import model_tools  # noqa: F401

    session_id = 'init-stop-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    sibling_id = 'sibling-init-stop-' + uuid.uuid4().hex
    sibling = SimpleNamespace()
    delegate._register_subagent({'subagent_id': sibling_id, 'agent': sibling,
        'owner_agent_session_id': session_id, 'status': 'running', 'capacity_occupied': True})
    _install_policy(monkeypatch, None)

    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    built = Child()
    constructor_calls = []

    def construct(**kwargs):
        constructor_calls.append(kwargs)
        entered.set()
        release.wait(5)
        if late_outcome == 'constructor_error':
            raise RuntimeError('late constructor failure')
        return built

    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    result = []
    reservation = None
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    try:
        assert entered.wait(2)
        parent.interrupt()
        assert returned.wait(2), 'parent Stop did not release the initialization waiter'
        assert result[0][0] == 'error'
        assert isinstance(result[0][1], ValueError)
        assert 'DELEGATION_PARENT_INTERRUPTED' in str(result[0][1])
        records = _owned_records(session_id)
        reservations = {sid for sid in records if sid.startswith('sa-init-')}
        assert len(reservations) == 1
        reservation = reservations.pop()
        assert records[reservation]['status'] == 'cleanup-incomplete'
        assert records[reservation]['capacity_occupied'] is True
        assert sibling_id in records
        assert sum(record.get('capacity_occupied') is True
            for record in records.values()) == 2
        assert not built.closed.is_set()
        assert built.conversation_calls == []
    finally:
        release.set()
        caller.join(3)
        if not caller.is_alive():
            _wait_until_initialization_settles(session_id)
            if reservation is not None:
                assert _wait_until_removed(session_id, reservation)
                assert sibling_id in _owned_records(session_id)
            delegate._unregister_subagent(sibling_id, agent=sibling)
            _cleanup_owner_records(session_id)

    assert not caller.is_alive()
    if late_outcome == 'child':
        assert built.closed.wait(2)
        assert built.conversation_calls == []


def test_parent_close_returns_while_initialization_is_blocked(monkeypatch):
    import model_tools  # noqa: F401

    session_id = 'init-close-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    parent._has_owned_tool_work = lambda: False
    parent.hard_interrupt = lambda *args, **kwargs: None
    _install_policy(monkeypatch, None)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    child = Child()

    def construct(**kwargs):
        entered.set()
        release.wait(5)
        return child

    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    result = []
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    try:
        assert entered.wait(2)
        from run_agent import AIAgent
        AIAgent.close(parent)
        assert parent._close_cleanup_state == 'incomplete'
        # Avoid invoking the skeletal test agent's close again from settlement.
        parent.close = lambda: None
        assert returned.wait(2), 'parent close did not release the initialization waiter'
        assert result[0][0] == 'error'
        assert isinstance(result[0][1], ValueError)
        assert 'DELEGATION_PARENT_CLOSING' in str(result[0][1])
        records = _owned_records(session_id)
        reservation = next(sid for sid in records if sid.startswith('sa-init-'))
        assert records[reservation]['status'] == 'cleanup-incomplete'
        assert records[reservation]['capacity_occupied'] is True
        assert not child.closed.is_set()
        assert child.conversation_calls == []
    finally:
        release.set()
        caller.join(3)
        if not caller.is_alive():
            _wait_until_initialization_settles(session_id)
            _cleanup_owner_records(session_id)

    assert not caller.is_alive()
    assert child.closed.wait(2)
    assert child.conversation_calls == []
    assert _wait_until_removed(session_id, reservation)


def test_parent_stop_while_queued_constructor_has_no_constructor_side_effects(monkeypatch):
    import model_tools  # noqa: F401

    session_id = 'init-queued-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    _install_policy(monkeypatch, None)
    constructor_calls = []
    returned = threading.Event()
    result = []

    def construct(**kwargs):
        constructor_calls.append(kwargs)
        return Child()

    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    delegate._CHILD_CONSTRUCTION_LOCK.acquire()
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    reservation = None
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            records = _owned_records(session_id)
            pending = [sid for sid, record in records.items()
                if sid.startswith('sa-init-') and 'future' in record]
            if pending:
                reservation = pending[0]
                break
            time.sleep(0.01)
        assert reservation is not None, 'initialization was not submitted to its worker'
        parent.interrupt()
        assert returned.wait(2), 'queued initialization did not observe parent Stop'
        assert result[0][0] == 'error'
        assert 'DELEGATION_PARENT_INTERRUPTED' in str(result[0][1])
    finally:
        delegate._CHILD_CONSTRUCTION_LOCK.release()
        caller.join(3)
        if not caller.is_alive():
            _wait_until_initialization_settles(session_id)
            _cleanup_owner_records(session_id)

    assert not caller.is_alive()
    assert constructor_calls == []
    assert reservation is not None
    assert _wait_until_removed(session_id, reservation)


def test_stop_generation_survives_flag_reset_before_waiter_polls(monkeypatch):
    import model_tools  # noqa: F401

    session_id = 'init-stop-reset-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    _install_policy(monkeypatch, None)
    entered, release_constructor, returned = (
        threading.Event(), threading.Event(), threading.Event())
    child = Child()
    result = []
    reservation = None

    def construct(**kwargs):
        entered.set()
        release_constructor.wait(5)
        return child

    real_wait = delegate._wait_child_attempt

    # Hold the consumer after admission, before its next is_set() poll.
    def held_wait(future, deadline, stop_signal=None, **kwargs):
        entered.wait(2)
        waiting.set()
        assert resume_waiting.wait(2)
        return real_wait(future, deadline, stop_signal, **kwargs)

    waiting, resume_waiting = threading.Event(), threading.Event()
    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    monkeypatch.setattr(delegate, '_wait_child_attempt', held_wait)
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    try:
        assert entered.wait(2), 'constructor did not start'
        assert waiting.wait(2), 'initialization waiter did not reach its barrier'
        _interrupt_then_reset_flag(parent)
        assert parent._interrupt_requested is False
        resume_waiting.set()
        assert returned.wait(2), 'old admission did not observe the reset Stop pulse'
        assert result[0][0] == 'error'
        assert 'DELEGATION_PARENT_INTERRUPTED' in str(result[0][1])
        records = _owned_records(session_id)
        reservation = next(sid for sid in records if sid.startswith('sa-init-'))
        assert records[reservation]['status'] == 'cleanup-incomplete'
        assert child.conversation_calls == []
    finally:
        resume_waiting.set()
        release_constructor.set()
        caller.join(3)
        if not caller.is_alive():
            _wait_until_initialization_settles(session_id)
            _cleanup_owner_records(session_id)

    assert not caller.is_alive()
    assert child.closed.wait(2)
    assert child.conversation_calls == []


def test_stop_generation_revokes_completed_future_before_publication(monkeypatch):
    import model_tools  # noqa: F401

    session_id = 'init-stop-completed-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    _install_policy(monkeypatch, None)
    child = Child()
    waiting, resume_waiting, returned = threading.Event(), threading.Event(), threading.Event()
    result = []

    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: child)

    def completed_wait(future, deadline, stop_signal=None, **kwargs):
        assert future.result(timeout=2) is child
        waiting.set()
        assert resume_waiting.wait(2)
        # Completed-set wins this wait; publication must still honor the
        # admission's captured generation after the flag has been cleared.
        return delegate.ChildWaitOutcome.COMPLETED

    monkeypatch.setattr(delegate, '_wait_child_attempt', completed_wait)
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    try:
        assert waiting.wait(2), 'completed initialization did not reach its barrier'
        _interrupt_then_reset_flag(parent)
        resume_waiting.set()
        assert returned.wait(2), 'stale completed child reached publication'
        assert result[0][0] == 'error'
        assert 'DELEGATION_PARENT_INTERRUPTED' in str(result[0][1])
    finally:
        resume_waiting.set()
        caller.join(3)

    assert not caller.is_alive()
    assert child.closed.wait(2)
    assert child.conversation_calls == []
    assert _wait_until_initialization_settles(session_id)
    assert _owned_records(session_id) == {}


def test_new_admission_after_stop_reset_captures_fresh_generation(monkeypatch):
    import model_tools  # noqa: F401

    session_id = 'init-stop-fresh-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    _interrupt_then_reset_flag(parent)
    _install_policy(monkeypatch, None)
    child = Child()
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: child)

    caller_done = Future()
    try:
        built = delegate._build_child_preserving_parent_tools(
            goal='fresh admission after reset', parent_agent=parent,
            _caller_initialization_done=caller_done)
        assert built is child
        assert any(record.get('subagent_id') == child._subagent_id
            for record in delegate.list_active_subagents())
    finally:
        caller_done.set_result(None)
        delegate._unregister_subagent(child._subagent_id, agent=child)


@pytest.mark.parametrize('timeout', [None, 1.0])
def test_normal_no_cap_and_finite_initialization_remain_valid(monkeypatch, timeout):
    import model_tools  # noqa: F401

    session_id = 'init-normal-' + uuid.uuid4().hex
    parent = SimpleNamespace(session_id=session_id, _active_children=[],
        _active_children_lock=threading.Lock())
    child = Child()
    _install_policy(monkeypatch, timeout)
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: child)

    caller_done = Future()
    try:
        built = delegate._build_child_preserving_parent_tools(
            goal='normal initialization', parent_agent=parent,
            _caller_initialization_done=caller_done)
        assert built is child
        assert (built._delegate_deadline is None if timeout is None
            else built._delegate_deadline is not None)
        assert any(r.get('subagent_id') == child._subagent_id
            for r in delegate.list_active_subagents())
    finally:
        caller_done.set_result(None)
        delegate._unregister_subagent(child._subagent_id, agent=child)


def test_completed_future_callback_cannot_retire_inline_before_caller_handoff(monkeypatch):
    from run_agent import AIAgent

    session_id = 'init-inline-callback-' + uuid.uuid4().hex
    parent = _agent_parent(session_id)
    parent._has_owned_tool_work = lambda: False
    parent.hard_interrupt = lambda *args, **kwargs: None
    _install_policy(monkeypatch, None)
    child = Child()
    constructor_entered, release_constructor = threading.Event(), threading.Event()
    stop_classified, cleanup_entered = threading.Event(), threading.Event()
    release_cleanup, returned = threading.Event(), threading.Event()
    parent_retry_done = threading.Event()
    parent_retries, cleanup_threads, result = [], [], []
    original_close = child.close

    def construct(**kwargs):
        constructor_entered.set()
        assert release_constructor.wait(5)
        return child

    def held_close():
        cleanup_threads.append(threading.get_ident())
        cleanup_entered.set()
        assert release_cleanup.wait(5)
        original_close()

    original_wait = delegate._wait_child_attempt

    def complete_between_stop_and_registration(future, deadline, stop_signal=None, **kwargs):
        outcome = original_wait(future, deadline, stop_signal, **kwargs)
        assert outcome is delegate.ChildWaitOutcome.STOPPED
        stop_classified.set()
        assert future.result(timeout=3) is child
        return outcome

    child.close = held_close
    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    monkeypatch.setattr(delegate, '_wait_child_attempt', complete_between_stop_and_registration)
    caller = threading.Thread(target=_run_builder, args=(parent, result, returned), daemon=True)
    caller.start()
    try:
        assert constructor_entered.wait(2)
        AIAgent.close(parent)
        assert parent._close_cleanup_state == 'incomplete'
        def observed_parent_retry():
            parent_retries.append(threading.get_ident())
            parent_retry_done.set()
        parent.close = observed_parent_retry
        assert stop_classified.wait(2)
        release_constructor.set()
        assert returned.wait(2), 'completed-Future callback blocked its Stop caller inline'
        assert result[0][0] == 'error'
        assert cleanup_entered.wait(2)
        assert cleanup_threads != [caller.ident]
        assert parent_retries == []
        assert not child.closed.is_set()
        assert any(row['capacity_occupied'] for row in _owned_records(session_id).values())
    finally:
        release_constructor.set()
        release_cleanup.set()
        caller.join(3)
        if not caller.is_alive():
            assert _wait_until_initialization_settles(session_id)
            _cleanup_owner_records(session_id)
    assert not caller.is_alive()
    assert child.closed.is_set()
    assert parent_retry_done.wait(2)
    assert parent_retries == cleanup_threads
    assert result[0][0] == 'error'
    assert 'DELEGATION_PARENT_CLOSING' in str(result[0][1])


def test_initialization_settlement_start_failure_retains_capacity_and_child(monkeypatch):
    parent = _agent_parent('init-settlement-start-' + uuid.uuid4().hex)
    _install_policy(monkeypatch, None)
    child = Child()
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: child)
    real_thread = threading.Thread

    def refuse_settlement_thread(*args, **kwargs):
        if kwargs.get('name', '').startswith('initialization-settlement-'):
            raise RuntimeError('controlled settlement start failure')
        return real_thread(*args, **kwargs)

    def stop_after_construction(future, deadline, stop_signal=None, **kwargs):
        assert future.result(timeout=2) is child
        parent._close_cleanup_state = 'incomplete'
        monkeypatch.setattr(threading, 'Thread', refuse_settlement_thread)
        return delegate.ChildWaitOutcome.STOPPED

    monkeypatch.setattr(delegate, '_wait_child_attempt', stop_after_construction)
    reservation = None
    caller_done = Future()
    try:
        with pytest.raises(ValueError, match='DELEGATION_PARENT_CLOSING'):
            delegate._build_child_preserving_parent_tools(
                goal='failed settlement supervisor', parent_agent=parent,
                _caller_initialization_done=caller_done)
        caller_done.set_result(None)
        records = _owned_records(parent.session_id)
        assert len(records) == 1
        reservation, row = next(iter(records.items()))
        assert row['capacity_occupied'] is True
        assert row['cleanup_state'] == 'incomplete'
        assert row['cleanup']['retirement_phase'] == 'initialization_settlement_start'
        assert row['cleanup']['outcome'] == 'unknown'
        assert row['future'].result() is child
        assert not child.closed.is_set()
        assert child.conversation_calls == []
    finally:
        monkeypatch.setattr(threading, 'Thread', real_thread)
        if reservation is not None:
            assert delegate._retire_unstarted_child(child, parent, reservation_id=reservation)
        _cleanup_owner_records(parent.session_id)
