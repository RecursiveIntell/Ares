"""Owner/boot fences beyond the reused request-correlation donor tests."""
import io
import json
import os
import threading
import types
from unittest.mock import Mock

import pytest
from tui_gateway import server
from tui_gateway.host_supervisor import HostSupervisor, HostBootMismatch, HostSendNotSent, HostSendUncertain
from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection


def test_blocked_rpc_sink_does_not_hold_subscription_registry(tmp_path):
    entered, release, registered = threading.Event(), threading.Event(), threading.Event()
    def sink(_message):
        entered.set()
        release.wait(5)
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False, rpc_sink=sink)
    proc = types.SimpleNamespace(stdout=io.StringIO(json.dumps({'type':'rpc','message':{'method':'event','params':{}}})+'\n'))
    host._proc = proc
    reader = threading.Thread(target=host._drain_stdout, args=(proc,))
    def subscribe():
        host.observe_session('sid', lambda _: None, request_id='A')
        registered.set()
    subscriber = threading.Thread(target=subscribe)
    reader.start()
    try:
        assert entered.wait(2)
        subscriber.start()
        assert registered.wait(2), 'RPC output I/O held the ownership registry'
    finally:
        release.set()
        reader.join(6)
        if subscriber.ident is not None:
            subscriber.join(6)
    assert not reader.is_alive() and not subscriber.is_alive()


@pytest.mark.parametrize('kind', ['hello', 'turn.end', 'rpc', 'control.ack'])
def test_retired_process_pipe_cannot_deliver_frames(tmp_path, monkeypatch, kind):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    retired = types.SimpleNamespace(stdout=io.StringIO(json.dumps({'type':kind,'boot_id':'old'})+'\n'))
    host._proc = object()
    delivered = Mock()
    monkeypatch.setattr(host, '_handle_host_frame', delivered)
    host._drain_stdout(retired)
    delivered.assert_not_called()


def test_wrong_boot_terminal_does_not_consume_registered_owner(tmp_path):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    host._hello = {'boot_id':'boot-new'}
    callback = Mock()
    host._pending_turns['A'] = ('sid', callback, 'boot-owner')
    host._complete_turn({'type':'turn.end','sid':'sid','request_id':'A','_host_boot_id':'boot-new'})
    wait_for_terminal_projection(host)
    callback.assert_not_called()
    assert 'A' in host._pending_turns


def test_wrong_boot_terminal_does_not_consume_observer(tmp_path):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    host._hello = {'boot_id':'boot-new'}
    callback = Mock()
    host._session_observers['sid'] = ('A', callback, 'boot-owner')
    host._complete_turn({'type':'turn.end','sid':'sid','request_id':'A','_host_boot_id':'boot-new'})
    wait_for_terminal_projection(host)
    callback.assert_not_called()
    assert 'sid' in host._session_observers


def test_observer_registration_rejects_a_changed_boot(tmp_path):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    host._hello = {'boot_id':'new'}
    with pytest.raises(HostBootMismatch):
        host.observe_session('sid', Mock(), request_id='A', expected_boot_id='old')
    assert not host._session_observers


def test_lookup_rejects_an_ack_from_a_retired_boot(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    monkeypatch.setattr(host, 'is_ready', lambda: True)
    host._hello = {'boot_id': 'current'}

    def answer(frame, *, expected_boot_id, deadline):
        assert expected_boot_id == 'current'
        host._handle_host_frame({
            'type': 'session.lookup.ack', 'request_id': frame['request_id'],
            '_host_boot_id': 'retired',
            'sessions': [{'session_id': 's', 'request_id': 'A', 'running': True}],
        })

    monkeypatch.setattr(host, '_send_owner_control_bounded', answer)
    with pytest.raises(HostSendUncertain):
        host.lookup_session_key('stored')
    assert not host._session_observers


def test_lookup_carries_the_observed_boot_to_adoption(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    monkeypatch.setattr(host, 'is_ready', lambda: True)
    host._hello = {'boot_id': 'current'}

    def answer(frame, *, expected_boot_id, deadline):
        host._handle_host_frame({
            'type': 'session.lookup.ack', 'request_id': frame['request_id'],
            '_host_boot_id': 'current',
            'sessions': [{'session_id': 's', 'request_id': 'A', 'running': True}],
        })

    monkeypatch.setattr(host, '_send_owner_control_bounded', answer)
    owner = host.lookup_session_key('stored')
    assert owner is not None
    assert owner['host_boot_id'] == 'current'


def test_admission_boot_change_is_refused_before_pipe_bytes(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, 'w')
    host._proc = types.SimpleNamespace(pid=0, stdin=stream, poll=lambda: None)
    host._ready_proc = host._proc
    host._hello = {'boot_id':'before'}
    send = host._send_frame
    def changed(frame, **kwargs):
        host._hello = {'boot_id':'after'}
        return send(frame, **kwargs)
    monkeypatch.setattr(host, '_send_frame', changed)
    try:
        with pytest.raises(HostBootMismatch):
            host.submit_turn({'sid':'s','request_id':'A'})
        os.set_blocking(read_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 1024)
        assert 'A' not in host._pending_turns
    finally:
        stream.close()
        os.close(read_fd)


def test_turn_startup_deadline_never_sends_after_timeout(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    entered, release = threading.Event(), threading.Event()
    sent = Mock()
    def blocked_start():
        entered.set()
        release.wait(5)
    monkeypatch.setattr(host, 'start', blocked_start)
    monkeypatch.setattr(host, '_send_frame', sent)
    try:
        with pytest.raises(HostSendNotSent):
            host.submit_turn({'sid':'s','request_id':'A'}, timeout=0.1)
        assert entered.is_set()
        sent.assert_not_called()
    finally:
        release.set()
        if host._startup_result is not None:
            assert host._startup_result[0].wait(2)
    sent.assert_not_called()
    assert not host._pending_turns


def test_terminal_cannot_project_into_a_replaced_parent_record(monkeypatch):
    old = {'history_lock':threading.Lock(),'running':True,'history':[],
           'session_key':'stored','agent':None,'_compute_host_active_request_id':'A'}
    replacement = {'history_lock':threading.Lock(),'running':True,'_compute_host_active_request_id':'B'}
    monkeypatch.setitem(server._sessions, 'sid', replacement)
    emitted,drained = Mock(),Mock()
    monkeypatch.setattr(server,'_emit',emitted)
    monkeypatch.setattr(server,'_drain_queued_prompt',drained)
    monkeypatch.setattr(server,'_session_info',lambda *a:{})
    server._on_compute_host_turn_done('rpc','sid',old,{'type':'turn.end','sid':'sid','request_id':'A'})
    emitted.assert_not_called();drained.assert_not_called()
    assert old['running'] is True and replacement['_compute_host_active_request_id']=='B'
