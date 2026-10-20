import contextlib
import threading
import types

import pytest

from tui_gateway import host_supervisor as hs, server
from hermes_state import SessionDB


def test_admission_captures_pipe_boot_before_send(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'original'}
    monkeypatch.setattr(host, '_ensure_started_by', lambda _: None)
    frame = {'sid': 's', 'request_id': 'A'}
    def send(payload, **kwargs):
        assert frame.get('_admitted_host_boot_id') == 'original'
        assert kwargs['expected_boot_id'] == 'original'
        host._hello = {'boot_id': 'replacement'}
    monkeypatch.setattr(host, '_send_frame', send)
    host.submit_turn(frame)
    assert frame['_admitted_host_boot_id'] == 'original'
    assert host._pending_turns['A'][2] == 'original'


def test_reserved_terminal_survives_ordinary_overload(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    done = threading.Event()
    host._pending_turns['A'] = ('s', lambda _: done.set(), host.boot_id)
    monkeypatch.setattr(hs, '_MAX_PROJECTIONS', 0)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    assert done.wait(2)
    assert 'A' not in host._pending_turns


def test_reserved_late_ack_survives_ordinary_overload(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'boot'}
    monkeypatch.setattr(host, '_ensure_started_by', lambda _: None)
    monkeypatch.setattr(host, '_send_owner_control_bounded', lambda *a, **k: None)
    done = threading.Event()
    with pytest.raises(hs.HostSendUncertain):
        host.interrupt('s', request_id='stop', target_request_id='A', wait=True,
                       timeout=0, on_late_ack=lambda _: done.set())
    monkeypatch.setattr(hs, '_MAX_PROJECTIONS', 0)
    host._handle_host_frame({'type': 'interrupt.ack', 'sid': 's', 'request_id': 'stop',
                            'target_request_id': 'A', 'applied': True, '_host_boot_id': 'boot'})
    assert done.wait(2)


def test_deferred_rpc_freezes_original_transport_and_payload(tmp_path, monkeypatch):
    from tui_gateway.transport import StdioTransport
    import io
    old, new = io.StringIO(), io.StringIO()
    old_t = StdioTransport(lambda: old, threading.Lock())
    new_t = StdioTransport(lambda: new, threading.Lock())
    record = {'transport': old_t}
    monkeypatch.setitem(server._sessions, 's', record)
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False,
                             rpc_sink=server.write_json, rpc_prepare=server._prepare_compute_host_rpc)
    host._hello = {'boot_id': 'boot'}
    monkeypatch.setattr(host, '_launch_projection_worker', lambda *a: None)
    frame = {'type': 'rpc', 'sid': 's', '_host_boot_id': 'boot', 'message': {
        'method': 'event', 'params': {'session_id': 's', 'type': 'clarify.request',
                                     'payload': {'request_id': 'original'}}}}
    host._handle_host_frame(frame)
    frame['message']['params']['payload']['request_id'] = 'mutated'
    monkeypatch.setitem(server._sessions, 's', {'transport': new_t})
    host._hello = {'boot_id': 'replacement'}
    host._settle_terminals('s')
    assert not new.getvalue()
    assert 'original' in old.getvalue() and 'mutated' not in old.getvalue()


def test_overload_disconnects_original_websocket_for_replay(tmp_path, monkeypatch):
    import asyncio
    from tui_gateway.ws import WSTransport
    from tui_gateway.event_replay import events_since
    loop = asyncio.new_event_loop()
    closed = []
    class Socket:
        async def close(self, code=1000):
            closed.append(code)
    transport = WSTransport(Socket(), loop)
    monkeypatch.setitem(server._sessions, 'overload-s', {'transport': transport})
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False,
                             rpc_prepare=server._prepare_compute_host_rpc)
    monkeypatch.setattr(hs, '_MAX_PROJECTIONS', 0)
    try:
        with pytest.raises(hs.HostProjectionFault):
            host._handle_host_frame({'type': 'rpc', 'sid': 'overload-s', 'message': {
                'method': 'event', 'params': {'session_id': 'overload-s', 'type': 'clarify.request'}}})
        loop.run_until_complete(asyncio.sleep(0))
        assert closed == [1013] and transport.closed
        assert events_since('overload-s', 0)[-1]['type'] == 'clarify.request'
    finally:
        loop.close()


@pytest.mark.parametrize('failure', ['owner_unavailable', 'before_write', 'after_commit'])
def test_canonical_retry_preserves_operation_and_original_input_cut(tmp_path, monkeypatch, failure):
    from hermes_state_continuity import context_stop_store_scope
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    token = {'missing_stop_receipt': True, 'operation_id': 'original-op',
             'store_scope': context_stop_store_scope(db.db_path), 'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'old-stop',
             'ack': {'applied': True}, 'input_release': threading.Event(),
             'terminal': {'type': 'turn.end', 'request_id': 'A', 'sid': 's', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token, 'queued_prompt': {'text': 'before-new-cut'},
               '_queued_prompt_generation': 1, 'running': False}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: types.SimpleNamespace(boot_id='boot'))
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(None if failure == 'owner_unavailable' else db))
    host = types.SimpleNamespace(boot_id='boot')
    token['host_supervisor'] = host
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: host)
    original = db.record_context_stop_operation
    calls = []
    def write(key, **kwargs):
        calls.append(key)
        assert kwargs['operation_id'] == 'original-op'
        if failure == 'after_commit':
            original(key, **kwargs)
        raise RuntimeError('private-error')
    monkeypatch.setattr(db, 'record_context_stop_operation', write)
    try:
        with pytest.raises(Exception):
            server._interrupt_owned_compute_host_turn('s', session, 'new-stop')
        assert token.get('missing_stop_receipt'), 'failure permanently erased recovery eligibility'
        monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
        committed = db.read_context_stop('stored')
        monkeypatch.setattr(db, 'record_context_stop_operation', original)
        assert server._interrupt_owned_compute_host_turn('s', session, 'another-stop')
        if committed is not None:
            assert db.read_context_stop('stored') == committed
        assert db.read_context_stop('stored')['control']['revision'] == 1
        assert session['queued_prompt']['text'] == 'before-new-cut'
        assert session['_queued_prompt_generation'] == 1
        assert len(calls) == (0 if failure == 'owner_unavailable' else 1)
    finally:
        db.close()


def test_legacy_returned_control_does_not_qualify_an_unknown_operation(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    control = db.record_context_stop('stored')
    token = {'missing_stop_receipt': True, 'canonical_stop_started': True,
             'canonical_stop_control': control, 'session_key': 'stored', 'profile_home': None,
             'host_boot_id': 'boot', 'target_request_id': 'A', 'request_id': 'original',
             'ack': {'applied': True}, 'input_release': threading.Event(),
             'terminal': {'request_id': 'A', 'sid': 's', '_host_boot_id': 'boot'}}
    session = {'history_lock': threading.Lock(), 'session_key': 'stored', 'profile_home': None,
               '_stop_uncertain': token, '_queued_prompt_generation': 1}
    monkeypatch.setitem(server._sessions, 's', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: types.SimpleNamespace(boot_id='boot'))
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    try:
        session['profile_home'] = 'changed-scope'
        with pytest.raises(RuntimeError):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert session['_stop_uncertain'] is token
        session['profile_home'] = None
        with pytest.raises(RuntimeError):
            server._interrupt_owned_compute_host_turn('s', session, 'repeat')
        assert session['_stop_uncertain'] is token
        assert db.read_context_stop('stored')['control'] == control
    finally:
        db.close()
