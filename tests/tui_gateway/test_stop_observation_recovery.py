"""Causal delivery witnesses with real threads and pipe-reader interleavings."""
import io
import json
import queue
import threading
import types

import pytest

from tui_gateway import host_supervisor as hs, server
from tui_gateway.transport import StdioTransport


def test_oversize_terminal_preserves_retirement_proof(tmp_path):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    observed, done = [], threading.Event()
    def receive(frame):
        observed.append(frame)
        done.set()
    host._pending_turns['A'] = ('s', receive, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A',
                         'session_info': {'presentation': 'x' * 100000}})
    assert done.wait(2)
    assert observed[0]['request_id'] == 'A'
    assert observed[0]['projection_degraded'] is True
    assert 'session_info' not in observed[0]


def test_finally_ack_refusal_retains_the_observation(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'boot'}
    monkeypatch.setattr(host, '_ensure_started_by', lambda _: None)
    ack = {'type': 'interrupt.ack', 'sid': 's', 'request_id': 'stop',
           'target_request_id': 'A', 'applied': True, '_host_boot_id': 'boot'}
    def send(*a, **kw):
        host._pending_controls['stop'].put_nowait(ack)
        raise hs.HostSendUncertain('unknown send')
    monkeypatch.setattr(host, '_send_owner_control_bounded', send)
    original = host._append_projection_locked
    def refuse(*a, **kw):
        raise hs.HostProjectionFault('refused')
    monkeypatch.setattr(host, '_append_projection_locked', refuse)
    with pytest.raises(hs.HostSendUncertain):
        host.interrupt('s', request_id='stop', target_request_id='A', wait=True,
                       timeout=0, on_late_ack=lambda _: None)
    retained = host._pending_controls['stop']
    assert retained.get_nowait() == ack
    monkeypatch.setattr(host, '_append_projection_locked', original)


def test_factory_failure_retries_only_unstarted_callback(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    called, done = [], threading.Event()
    factory = hs._Thread
    count = []
    def flaky_factory(*a, **kw):
        count.append(True)
        if len(count) == 1:
            raise RuntimeError('no thread constructed')
        return factory(*a, **kw)
    monkeypatch.setattr(hs, '_Thread', flaky_factory)
    def callback(frame):
        called.append(threading.get_ident())
        done.set()
    host._pending_turns['A'] = ('s', callback, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    assert done.wait(2), 'positively unstarted work was stranded'
    assert called != [threading.get_ident()] and len(called) == 1
    assert len(count) == 2


def test_unknown_start_does_not_duplicate_callback(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    entered, release, done = (threading.Event() for _ in range(3))
    calls = []
    factory = hs._Thread
    class StartedThenRaised:
        def __init__(self, *a, **kw):
            self.thread = factory(*a, **kw)
        def start(self):
            self.thread.start()
            assert entered.wait(2)
            raise RuntimeError('ambiguous start')
    monkeypatch.setattr(hs, '_Thread', StartedThenRaised)
    def receive(frame):
        calls.append(frame)
        entered.set()
        release.wait(3)
        done.set()
    host._pending_turns['A'] = ('s', receive, host.boot_id)
    try:
        host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
        assert 's' in host._terminal_workers, 'ambiguous launch lost its running ownership'
        assert len(calls) == 1
    finally:
        release.set()
        done.wait(3)


@pytest.mark.parametrize('replacement', ['pipe', 'session', 'transport'])
def test_source_capture_precedes_registry_release(tmp_path, monkeypatch, replacement):
    old_out, new_out = io.StringIO(), io.StringIO()
    old = {'transport': StdioTransport(lambda: old_out, threading.Lock())}
    new = {'transport': StdioTransport(lambda: new_out, threading.Lock())}
    monkeypatch.setitem(server._sessions, 's', old)
    prepared, resume = threading.Event(), threading.Event()
    def prepare(message, **kwargs):
        prepared.set()
        assert resume.wait(3)
        return server._prepare_compute_host_rpc(message, **kwargs)
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False, rpc_prepare=prepare,
                             rpc_capture=server._capture_compute_host_rpc)
    frame = {'type': 'rpc', 'sid': 's', 'message': {'method': 'event', 'params': {
        'session_id': 's', 'type': 'message.delta', 'payload': {'text': 'old-frame'}}}}
    proc = types.SimpleNamespace(stdout=io.StringIO(json.dumps(frame) + '\n'))
    host._proc = proc
    host._hello = {'boot_id': 'old-boot'}
    thread = threading.Thread(target=host._drain_stdout, args=(proc,))
    thread.start()
    try:
        assert prepared.wait(2)
        # This replacement occurs inside the actual drain/prepare window.
        if replacement == 'pipe':
            host._proc = object()
            host._hello = {'boot_id': 'new-boot'}
        elif replacement == 'session':
            monkeypatch.setitem(server._sessions, 's', new)
        else:
            old['transport'] = new['transport']
    finally:
        resume.set()
        thread.join(3)
    host._settle_terminals('s')
    assert not new_out.getvalue(), 'old pipe published through replacement custody'


def test_callback_refusal_retains_authoritative_evidence(tmp_path):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    done = threading.Event()
    def refuse(frame):
        done.set()
        return False
    host._pending_turns['A'] = ('s', refuse, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    assert done.wait(2)
    # Synchronize with worker completion using the registry-owned lookup.
    for _ in range(100):
        evidence = host.read_owner_observation('s', request_id='A', boot_id=host.boot_id)
        if evidence:
            break
        threading.Event().wait(.01)
    assert evidence['type'] == 'turn.end'


@pytest.mark.parametrize('wrong_type', ['interrupt.ack', 'error'])
def test_wrong_early_ack_cannot_evict_the_matching_ack(tmp_path, monkeypatch, wrong_type):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    host._hello = {'boot_id': 'boot'}
    monkeypatch.setattr(host, '_ensure_started_by', lambda _: None)
    def send(*a, **kw):
        for target in ('foreign', 'A'):
            host._handle_host_frame({'type': wrong_type if target == 'foreign' else 'interrupt.ack', 'sid': 's', 'request_id': 'stop',
                'target_request_id': target, 'applied': True, '_host_boot_id': 'boot'})
    monkeypatch.setattr(host, '_send_owner_control_bounded', send)
    ack = host.interrupt('s', request_id='stop', target_request_id='A', wait=True,
                          timeout=1, on_late_ack=lambda _: None)
    assert ack['target_request_id'] == 'A'


def test_refusal_handler_failure_cannot_erase_terminal(tmp_path):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    def callback(frame):
        return False
    def fail():
        raise OSError('private-data')
    callback.refuse = fail
    host._pending_turns['A'] = ('s', callback, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    for _ in range(100):
        evidence = host.read_owner_observation('s', request_id='A', boot_id=host.boot_id)
        if evidence:
            break
        threading.Event().wait(.01)
    assert evidence and evidence['request_id'] == 'A'


def test_explicit_recovery_restarts_only_retained_unstarted_work(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    factory = hs._Thread
    attempts, calls = [], []
    done = threading.Event()
    def unavailable(*a, **kw):
        attempts.append(True)
        raise RuntimeError('factory unavailable')
    monkeypatch.setattr(hs, '_Thread', unavailable)
    def callback(frame):
        calls.append(threading.get_ident())
        done.set()
    host._pending_turns['A'] = ('s', callback, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    assert len(attempts) == 2 and not calls
    monkeypatch.setattr(hs, '_Thread', factory)
    host.recover_projections('s')
    assert done.wait(2)
    assert len(calls) == 1 and calls[0] != threading.get_ident()
    host.recover_projections('s')
    assert len(calls) == 1


def test_retrospective_delayed_ambiguous_start_cannot_enter_revoked_ticket(tmp_path, monkeypatch):
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False)
    original = hs._Thread
    delayed, calls = [], []
    done = threading.Event()
    class DelayedStart:
        def __init__(self, *, target, **kw):
            self.target = target
        def start(self):
            delayed.append(self.target)
            raise RuntimeError('launch return unknown')
    attempts = []
    def factory(*a, **kw):
        attempts.append(True)
        return DelayedStart(*a, **kw) if len(attempts) == 1 else original(*a, **kw)
    monkeypatch.setattr(hs, '_Thread', factory)
    def receive(frame):
        calls.append(frame)
        done.set()
    host._pending_turns['A'] = ('s', receive, host.boot_id)
    host._complete_turn({'type': 'turn.end', 'sid': 's', 'request_id': 'A'})
    assert done.wait(2)
    late = original(target=delayed[0])
    late.start()
    late.join(2)
    assert not late.is_alive() and len(calls) == 1


def test_rpc_refusal_exception_cannot_stop_ack_reader(tmp_path, monkeypatch):
    def prepare(message):
        def callback(frame):
            pytest.fail('refused RPC must not be delivered')
        def refuse():
            raise OSError('private-refusal-detail')
        callback.refuse = refuse
        return callback
    host = hs.HostSupervisor(registry_path=tmp_path / 'host.json', autostart=False, rpc_prepare=prepare)
    monkeypatch.setattr(hs, '_MAX_PROJECTIONS', 0)
    waiter = queue.Queue(maxsize=1)
    host._pending_controls['stop'] = waiter
    frames = [{'type': 'rpc', 'sid': 's', 'message': {'method': 'event', 'params': {'session_id': 's'}}},
              {'type': 'interrupt.ack', 'sid': 's', 'request_id': 'stop', 'applied': True}]
    proc = types.SimpleNamespace(stdout=io.StringIO(''.join(json.dumps(frame) + '\n' for frame in frames)))
    host._proc = proc
    host._drain_stdout(proc)
    assert waiter.get_nowait()['request_id'] == 'stop'
