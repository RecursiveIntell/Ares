"""Initialization resources remain owned until caller acknowledgment."""
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import threading
import uuid

import pytest

from tools import delegate_tool as delegate

_support_spec = importlib.util.spec_from_file_location(
    'initialization_stop_support', Path(__file__).with_name('test_delegate_initialization_stop.py'))
support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(support)


@pytest.mark.parametrize('constructor_outcome', ['child', 'error'])
def test_caller_finalization_retains_initialization_until_ack(monkeypatch, constructor_outcome):
    parent = support._agent_parent('owner-ack-' + uuid.uuid4().hex)
    support._install_policy(monkeypatch, None)
    owner_done = Future()
    child = support.Child()
    entered, release_build = threading.Event(), threading.Event()
    error_caught, release_finalization = threading.Event(), threading.Event()
    callbacks_finished, parent_retried = threading.Event(), threading.Event()
    returned = threading.Event()
    observed_future, result = [], []

    def construct(**kwargs):
        assert '_caller_initialization_done' not in kwargs
        entered.set()
        assert release_build.wait(5)
        if constructor_outcome == 'error':
            raise RuntimeError('controlled late constructor error')
        return child

    def stop_wait(future, deadline, stop_signal=None, **kwargs):
        assert entered.wait(3)
        observed_future.append(future)
        parent._close_cleanup_state = 'incomplete'
        return delegate.ChildWaitOutcome.STOPPED

    def close_parent():
        # This is an observed retry, not real provider/DB destruction.
        parent_retried.set()

    def caller():
        try:
            delegate._build_child_preserving_parent_tools(
                goal='caller-owned construction', parent_agent=parent,
                _caller_initialization_done=owner_done)
        except BaseException as exc:
            result.append(exc)
            error_caught.set()
            assert release_finalization.wait(5)
        finally:
            owner_done.set_result(None)
            returned.set()

    parent.close = close_parent
    monkeypatch.setattr(delegate, '_build_child_agent', construct)
    monkeypatch.setattr(delegate, '_wait_child_attempt', stop_wait)
    real_thread = threading.Thread
    settlement_dispatched = threading.Event()

    def observe_thread(*args, **kwargs):
        if kwargs.get('name', '').startswith('initialization-settlement-'):
            settlement_dispatched.set()
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, 'Thread', observe_thread)
    worker = real_thread(target=caller, daemon=True)
    worker.start()
    try:
        assert error_caught.wait(3), 'caller never received initialization refusal'
        future = observed_future[0]
        future.add_done_callback(lambda _: callbacks_finished.set())
        release_build.set()
        assert callbacks_finished.wait(3), 'constructor callbacks never completed'
        if settlement_dispatched.is_set():
            assert parent_retried.wait(3), 'early settlement never reached its parent retry'
        assert not parent_retried.is_set(), 'parent retirement preceded caller acknowledgment'
        records = support._owned_records(parent.session_id)
        assert len(records) == 1
        assert next(iter(records.values()))['capacity_occupied'] is True
        assert owner_done.done() is False
        assert not returned.is_set()
        release_finalization.set()
        assert returned.wait(3)
        assert parent_retried.wait(3), 'acknowledged settled initialization never retired'
        assert support._wait_until_initialization_settles(parent.session_id)
        assert support._owned_records(parent.session_id) == {}
        if constructor_outcome == 'child':
            assert child.closed.is_set()
        assert child.conversation_calls == []
        assert isinstance(result[0], ValueError)
        assert 'DELEGATION_PARENT_CLOSING' in str(result[0])
    finally:
        release_build.set()
        release_finalization.set()
        worker.join(5)
        support._cleanup_owner_records(parent.session_id)
    assert not worker.is_alive()


@pytest.mark.parametrize('bad_ack', ['cancel', 'exception', 'unexpected_result'])
def test_invalid_owner_ack_is_not_settlement(monkeypatch, bad_ack):
    parent = support._agent_parent('invalid-owner-ack-' + uuid.uuid4().hex)
    support._install_policy(monkeypatch, None)
    owner_done = Future()
    child = support.Child()
    parent_retried = threading.Event()
    parent.close = parent_retried.set
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: child)

    def stop_wait(future, deadline, stop_signal=None, **kwargs):
        assert future.result(timeout=3) is child
        parent._close_cleanup_state = 'incomplete'
        if bad_ack == 'cancel':
            owner_done.cancel()
        elif bad_ack == 'exception':
            owner_done.set_exception(RuntimeError('caller finalization failed'))
        else:
            owner_done.set_result(True)
        return delegate.ChildWaitOutcome.STOPPED

    monkeypatch.setattr(delegate, '_wait_child_attempt', stop_wait)
    try:
        with pytest.raises(ValueError, match='DELEGATION_PARENT_CLOSING'):
            delegate._build_child_preserving_parent_tools(
                goal='invalid handoff', parent_agent=parent,
                _caller_initialization_done=owner_done)
        records = support._owned_records(parent.session_id)
        assert len(records) == 1, 'invalid caller token released initialization ownership'
        row = next(iter(records.values()))
        assert row['capacity_occupied'] is True
        assert row['cleanup_state'] == 'incomplete'
        assert not parent_retried.is_set()
    finally:
        support._cleanup_owner_records(parent.session_id)


@pytest.mark.parametrize('owner_state', ['missing', 'wrong_type', 'already_done'])
def test_owner_refusal_has_no_construction_or_reservation_effects(monkeypatch, owner_state):
    parent = support._agent_parent('owner-format-' + uuid.uuid4().hex)
    support._install_policy(monkeypatch, None)
    invoked = []
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kwargs: invoked.append(kwargs))
    owner = None if owner_state == 'missing' else object()
    if owner_state == 'already_done':
        owner = Future()
        owner.set_result(None)
    before = delegate.list_active_subagents()
    with pytest.raises(ValueError, match='DELEGATION_INITIALIZATION_OWNER_REQUIRED'):
        delegate._build_child_preserving_parent_tools(
            goal='refused before construction', parent_agent=parent,
            _caller_initialization_done=owner)
    assert invoked == []
    assert delegate.list_active_subagents() == before


def test_plugin_caller_acknowledges_after_receiving_constructor_error(monkeypatch):
    from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService

    parent = support._agent_parent('plugin-owner-ack-' + uuid.uuid4().hex)
    service = SubagentLifecycleService(lambda: parent)
    captured = []

    def refuse_build(**kwargs):
        done = kwargs['_caller_initialization_done']
        assert isinstance(done, Future) and not done.done()
        captured.append(done)
        raise ValueError('controlled constructor refusal')

    monkeypatch.setattr(delegate, '_build_child_preserving_parent_tools', refuse_build)
    with pytest.raises(ValueError, match='controlled constructor refusal'):
        service.launch(SubagentLaunchRequest(goal='neutral plugin initialization'))
    assert len(captured) == 1
    assert captured[0].done() and captured[0].result() is None


@pytest.mark.parametrize('close_mode', ['raises', 'unconfirmed', 'complete'])
def test_actual_factory_db_cleanup_is_owned_through_constructor_failure(monkeypatch, tmp_path, close_mode):
    import json
    from unittest.mock import MagicMock
    import hermes_state
    import run_agent

    spec = importlib.util.spec_from_file_location('constructor_parent_fixture',
        Path(__file__).with_name('test_delegate.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    real_db = hermes_state.SessionDB
    parent_db = real_db(tmp_path / 'constructor-owner.db')
    parent = fixture._make_mock_parent()
    parent.session_id = 'constructor-resource-' + uuid.uuid4().hex
    parent._session_db = parent_db
    parent._close_cleanup_state = None
    opened = []
    retired = threading.Event()
    real_thread = threading.Thread
    caller_done = Future()
    support._install_policy(monkeypatch, None, capacity=1)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})

    def allocate_db(*args, **kwargs):
        db = real_db(*args, **kwargs)
        original_close = db.close
        def close():
            if close_mode == 'raises':
                raise OSError('controlled dedicated DB close refusal')
            if close_mode == 'complete':
                original_close()
        db.close = close
        opened.append((db, original_close))
        return db

    def observe_thread(*args, **kwargs):
        if kwargs.get('name', '').startswith('initialization-settlement-'):
            target = kwargs['target']
            def finish(*a, **k):
                try:
                    return target(*a, **k)
                finally:
                    retired.set()
            kwargs['target'] = finish
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(hermes_state, 'SessionDB', allocate_db)
    constructor = MagicMock(side_effect=RuntimeError('primary constructor failure'))
    monkeypatch.setattr(run_agent, 'AIAgent', constructor)
    monkeypatch.setattr(threading, 'Thread', observe_thread)
    try:
        with pytest.raises(RuntimeError, match='primary constructor failure'):
            delegate._build_child_preserving_parent_tools(
                task_index=0, goal='owned constructor cleanup failure', context=None,
                toolsets=None, model=None, max_iterations=1, task_count=1,
                parent_agent=parent, _caller_initialization_done=caller_done)
        assert len(opened) == 1
        assert len(support._owned_records(parent.session_id)) == 1
        caller_done.set_result(None)
        assert retired.wait(3), 'initialization retirement callback did not finish'
        rows = support._owned_records(parent.session_id)
        if close_mode == 'complete':
            assert rows == {}
            assert opened[0][0]._conn is None
        else:
            assert len(rows) == 1
            row = next(iter(rows.values()))
            assert row['capacity_occupied'] is True
            assert row['cleanup']['outcome'] == 'unknown'
            assert row['initialization_resources'].session_db is opened[0][0]
            assert opened[0][0]._conn.execute('select 1').fetchone()[0] == 1
            snapshot = delegate.list_active_subagents()
            json.dumps(snapshot)
            assert all('initialization_resources' not in x for x in snapshot)
            with pytest.raises(ValueError, match='DELEGATION_CAPACITY_OCCUPIED'):
                delegate._build_child_preserving_parent_tools(goal='must not start',
                    parent_agent=parent, _caller_initialization_done=Future())
            assert constructor.call_count == 1
    finally:
        if not caller_done.done():
            caller_done.set_result(None)
        for db, original_close in opened:
            db.close = original_close
            original_close()
        support._cleanup_owner_records(parent.session_id)
        parent_db.close()
