"""Correlation and recovery through the real Stop owner, without provider I/O."""
import contextlib
import io
import json
import threading
import types

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import context_stop_store_scope
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSendUncertain
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent  # noqa: F401


def test_operation_crosses_real_agent_host_and_strict_readback(durable_agent, monkeypatch):
    agent, db = durable_agent
    session = {'agent': agent, 'history_lock': threading.Lock(), 'session_key': agent.session_id,
               'running': True, '_run_thread': types.SimpleNamespace(is_alive=lambda: True)}
    monkeypatch.setattr(server, '_sessions', {'s': session})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    monkeypatch.setattr(server, '_clear_pending', lambda *a: None)
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids['s'] = 'A'
    try:
        host._handle_interrupt({'sid': 's', 'request_id': 'rpc', 'target_request_id': 'A', 'operation_id': 'op'})
        ack = json.loads(output.getvalue())
        assert ack['operation_id'] == 'op'
        receipt = ack['stop_receipt']
        assert receipt['operation'] == db.read_context_stop_operation(agent.session_id, operation_id='op')
        server._verify_host_stop_receipt(session, receipt, operation_id='op')
        with pytest.raises(RuntimeError):
            server._verify_host_stop_receipt(session, receipt, operation_id='other')
        db.record_context_stop_operation(agent.session_id, operation_id='new')
        with pytest.raises(RuntimeError):
            server._verify_host_stop_receipt(session, receipt, operation_id='op')
    finally:
        host.close()


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_parent_unknown_return_recovers_same_operation_without_new_cut(tmp_path, monkeypatch, failure):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    token = {'operation_id': 'original-op', 'store_scope': context_stop_store_scope(db.db_path),
             'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'original-rpc',
             'durable_stop_unconfirmed': True, 'input_release': threading.Event(),
             'terminal': {'type': 'turn.end', 'request_id': 'A', 'sid': 's', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token, 'queued_prompt': {'text': 'after-original-cut'},
               '_queued_prompt_generation': 1, 'running': False}
    monkeypatch.setitem(server._sessions, 's', session)
    host = types.SimpleNamespace(boot_id='boot')
    token['host_supervisor'] = host
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: host)
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    original = db.record_context_stop_operation
    def fail(key, **kw):
        assert kw['operation_id'] == 'original-op'
        if failure == 'after':
            original(key, **kw)
        raise OSError('private-error')
    monkeypatch.setattr(db, 'record_context_stop_operation', fail)
    try:
        with pytest.raises(Exception):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat-1')
        committed = db.read_context_stop('stored')
        monkeypatch.setattr(db, 'record_context_stop_operation', original)
        assert server._interrupt_owned_compute_host_turn('s', session, 'repeat-2')
        assert db.read_context_stop_operation('stored', operation_id='original-op')
        if committed:
            assert db.read_context_stop('stored') == committed
        assert session['queued_prompt']['text'] == 'after-original-cut'
        assert session['_queued_prompt_generation'] == 1
        assert token['request_id'] == 'original-rpc' and token['input_release'].is_set()
    finally:
        db.close()


def test_legacy_uncertain_token_cannot_create_an_operation(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    token = {'missing_stop_receipt': True, 'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'old',
             'ack': {'applied': True}, 'input_release': threading.Event(),
             'terminal': {'request_id': 'A', 'sid': 's', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: types.SimpleNamespace(boot_id='boot'))
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    try:
        with pytest.raises(RuntimeError):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert db.read_context_stop('stored') is None
        assert session['_stop_uncertain'] is token
    finally:
        db.close()


def test_unsupported_adapter_still_cancels_but_never_qualifies(monkeypatch):
    from agent.interrupt_compat import request_hard_interrupt
    calls = []
    class Legacy:
        def interrupt(self):
            calls.append(True)
    agent = Legacy()
    assert request_hard_interrupt(agent, operation_id='op')
    assert calls == [True]
    assert not getattr(agent, '_context_stop_receipt', None)


def test_recovery_cannot_move_original_operation_to_another_store(tmp_path, monkeypatch):
    original = SessionDB(tmp_path / 'original.db')
    replacement = SessionDB(tmp_path / 'replacement.db')
    for db in (original, replacement):
        db.create_session('stored', source='desktop')
    import hashlib
    scope = 'sha256:' + hashlib.sha256(str(original.db_path.resolve()).encode()).hexdigest()
    token = {'operation_id': 'op', 'store_scope': scope, 'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'rpc',
             'input_release': threading.Event(), 'terminal': {
                 'type': 'turn.end', 'sid': 's', 'request_id': 'A', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: types.SimpleNamespace(boot_id='boot'))
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(replacement))
    host = types.SimpleNamespace(boot_id='boot')
    token['host_supervisor'] = host
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: host)
    try:
        with pytest.raises(HostSendUncertain, match='storage scope changed'):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert replacement.read_context_stop('stored') is None
        assert original.read_context_stop('stored') is None
    finally:
        original.close()
        replacement.close()


def test_retained_failed_terminal_can_reconcile_but_receipt_alone_cannot(tmp_path, monkeypatch):
    from tui_gateway.host_supervisor import HostSupervisor
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    operation = db.record_context_stop_operation('stored', operation_id='op')['receipt']
    host = HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'boot'}
    token = {'operation_id': 'op', 'store_scope': operation['store_scope'],
             'host_supervisor': host,
             'session_key': 'stored', 'profile_home': None, 'host_boot_id': 'boot',
             'target_request_id': 'A', 'request_id': 'rpc', 'input_release': threading.Event()}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token, '_compute_host_active_request_id': 'A', 'running': True}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: host)
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    try:
        with pytest.raises(RuntimeError):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert session['running'] is True
        done = threading.Event()
        def refuse(frame):
            done.set()
            return False
        host._pending_turns['A'] = ('s', refuse, 'boot')
        host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A', '_host_boot_id': 'boot'})
        assert done.wait(2)
        for _ in range(100):
            if host.read_owner_observation('s', request_id='A', boot_id='boot'):
                break
            threading.Event().wait(.01)
        # Both the terminal and the original ACK can fail presentation. Their
        # bounded slots retire only after canonical + execution reconciliation.
        host._enqueue_projection('s', {'type': 'interrupt.ack', 'sid': 's', 'request_id': 'rpc',
            'target_request_id': 'A', 'operation_id': 'op', 'applied': True, '_host_boot_id': 'boot'},
            lambda frame: False, reserved=True)
        for _ in range(100):
            if host.read_owner_observation('s', request_id='rpc', boot_id='boot'):
                break
            threading.Event().wait(.01)
        assert server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert not session['running'] and not session.get('_compute_host_active_request_id')
        assert db.read_context_stop('stored')['control'] == operation['control']
        assert host._reserved_projections == 0, 'verified recovery must release its retained delivery slot'
    finally:
        db.close()


def test_qualified_supervisor_ack_must_match_operation(tmp_path, monkeypatch):
    from tui_gateway.host_supervisor import HostSupervisor
    host = HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'boot'}
    monkeypatch.setattr(host, '_ensure_started_by', lambda _: None)
    def send(frame, **kw):
        assert frame['operation_id'] == 'original'
        host._handle_host_frame({'type': 'interrupt.ack', 'sid': 's', 'request_id': 'rpc',
            'target_request_id': 'A', 'operation_id': 'foreign', 'applied': True, '_host_boot_id': 'boot'})
    monkeypatch.setattr(host, '_send_owner_control_bounded', send)
    with pytest.raises(HostSendUncertain):
        host.interrupt('s', request_id='rpc', target_request_id='A', operation_id='original',
                       wait=True, timeout=0, on_late_ack=lambda _: pytest.fail('foreign operation'))
    assert 'rpc' in host._pending_controls


@pytest.mark.parametrize('iteration', range(5))
def test_retrospective_readback_cut_race_refuses_historical_operation(tmp_path, monkeypatch, iteration):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    operation = db.record_context_stop_operation('stored', operation_id='original')['receipt']
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    original = db.read_context_stop
    raced = []
    def read(key):
        result = original(key)
        if not raced:
            raced.append(db.record_context_stop_operation(key, operation_id='competitor'))
        return result
    monkeypatch.setattr(db, 'read_context_stop', read)
    try:
        with pytest.raises(RuntimeError, match='scope or value'):
            server._verify_host_stop_receipt({'session_key': 'stored'}, {
                'session_key': 'stored', 'control': operation['control'], 'operation': operation},
                operation_id='original')
        assert original('stored')['control'] == raced[0]['receipt']['control']
        assert db.record_context_stop_operation('stored', operation_id='original')['receipt'] == operation
        assert original('stored')['control']['revision'] == 2
    finally:
        db.close()


def test_retrospective_agent_unknown_commit_keeps_local_cancellation_and_redacts_error(durable_agent, monkeypatch, caplog):
    agent, db = durable_agent
    original = db.record_context_stop_operation
    def commit_then_fail(*a, **kw):
        assert agent._hard_interrupt_requested.is_set()
        assert agent._interrupt_requested is True
        original(*a, **kw)
        raise OSError('private-storage-data')
    monkeypatch.setattr(db, 'record_context_stop_operation', commit_then_fail)
    agent.hard_interrupt(operation_id='unknown-return')
    assert agent._context_stop_receipt is None and agent._context_stop_unacknowledged
    assert db.read_context_stop_operation(agent.session_id, operation_id='unknown-return')
    assert 'private-storage-data' not in caplog.text


@pytest.mark.parametrize('changed', ['session', 'profile', 'boot', 'supervisor'])
def test_recovery_rechecks_ownership_after_blocking_storage(tmp_path, monkeypatch, changed):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    host = types.SimpleNamespace(boot_id='boot')
    hosts = [host]
    token = {'operation_id': 'op', 'store_scope': context_stop_store_scope(db.db_path),
             'host_supervisor': host, 'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'rpc',
             'input_release': threading.Event(), 'terminal': {
                 'type': 'turn.end', 'sid': 's', 'request_id': 'A', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: hosts[0])
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    original = db.record_context_stop_operation
    def write(*a, **kw):
        result = original(*a, **kw)
        if changed == 'session':
            server._sessions['s'] = {'session_key': 'successor'}
        elif changed == 'profile':
            session['profile_home'] = 'replacement'
        elif changed == 'boot':
            host.boot_id = 'replacement'
        else:
            hosts[0] = types.SimpleNamespace(boot_id='boot')
        return result
    monkeypatch.setattr(db, 'record_context_stop_operation', write)
    try:
        with pytest.raises(HostSendUncertain):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert session['_stop_uncertain'] is token
        assert not token['input_release'].is_set()
        assert db.read_context_stop_operation('stored', operation_id='op')
    finally:
        db.close()


def test_retrospective_legacy_adapter_receipt_is_not_upgraded_by_host(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    class Legacy:
        def interrupt(self):
            self._context_stop_receipt = {'session_key': 'stored', 'control': db.record_context_stop('stored')}
    agent = Legacy()
    session = {'agent': agent, 'history_lock': threading.Lock(), 'session_key': 'stored',
               'running': True, '_run_thread': types.SimpleNamespace(is_alive=lambda: True)}
    monkeypatch.setattr(server, '_sessions', {'s': session})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    monkeypatch.setattr(server, '_clear_pending', lambda *a: None)
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids['s'] = 'A'
    try:
        host._handle_interrupt({'sid': 's', 'request_id': 'rpc', 'target_request_id': 'A', 'operation_id': 'op'})
        ack = json.loads(output.getvalue())
        assert ack['applied'] is True
        assert 'operation' not in ack['stop_receipt']
        assert db.read_context_stop_operation('stored', operation_id='op') is None
        with pytest.raises(RuntimeError, match='operation mismatch'):
            server._verify_host_stop_receipt(session, ack['stop_receipt'], operation_id='op')
        assert server._verify_host_stop_receipt(session, ack['stop_receipt']) == db.read_context_stop('stored')
    finally:
        host.close()
        db.close()
