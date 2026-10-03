"""Registered RPCs and real receipt/resume helpers; disposable SQLite only."""
import concurrent.futures
import threading
import time

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tests.tui_gateway.test_prompt_recovery_contract import _session


class Peer:
    def __init__(self):
        self.frames = []
        self.ready = threading.Condition()

    def write(self, frame):
        with self.ready:
            self.frames.append(frame)
            self.ready.notify_all()
        return True

    def response(self, rid):
        with self.ready:
            assert self.ready.wait_for(lambda: any(f.get('id') == rid for f in self.frames), 5)
            return next(f for f in self.frames if f.get('id') == rid)


def rpc(peer, rid, method, **params):
    result = server.dispatch({'id': rid, 'method': method, 'params': params}, peer)
    return result if result is not None else peer.response(rid)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('stored', source='desktop', cwd=str(tmp_path))
    peer = Peer()
    record = _session(session_key='stored', cwd=str(tmp_path), transport=peer, created_at=time.time())
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    for name in ('_pending', '_answers', '_pending_prompt_payloads', '_batch_clarify',
                 '_clarify_request_owners', '_clarify_response_receipts'):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, '_sessions', {'old': record})
    monkeypatch.setattr(server, '_pool', pool)
    monkeypatch.setattr(server, '_db', db)
    monkeypatch.setattr(server, '_hermes_home', tmp_path)
    monkeypatch.setattr(server, '_compute_host_supervisor', None)
    monkeypatch.setattr(server, '_profile_home', lambda _p: None)
    monkeypatch.setattr(server, '_profile_configured_cwd', lambda _p: str(tmp_path))
    monkeypatch.setattr(server, '_fallback_session_info', lambda *_a: {})
    monkeypatch.setattr(server, '_pending_approval_request_payload', lambda *_a: None)
    monkeypatch.setattr(server, '_enable_gateway_prompts', lambda: None)
    monkeypatch.setattr(server, '_tts_stream_stop', lambda: None)
    monkeypatch.setattr(server, '_schedule_resume_hydration', lambda *_a, **_k: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_lazy_resume_info', lambda *_a, **_k: {})
    yield peer, record, db
    server._clear_pending()
    pool.shutdown(wait=True, cancel_futures=True)
    db.close()


def fake_host(monkeypatch, record, action):
    class Host:
        boot_id = 'boot'
        def is_ready(self):
            return True
        def interrupt(self, *_a, **_kw):
            return None
        def control(self, sid, **kw):
            action(sid, kw)
            return {'type': 'control.ack', 'sid': sid,
                    'request_id': kw['payload']['request_id'], 'route_name': kw['route_name'],
                    'response': {'id': kw['payload']['request_id'],
                                 'result': {'generation': 'child-generation',
                                            'pending_clarify': {'request_id': 'child-question'}}}}
    host = Host()
    record['_compute_host_active'] = True
    monkeypatch.setattr(server, '_compute_host_supervisor', host)
    return host


def test_activation_wait_leaves_registered_reader_controls_responsive(gateway, monkeypatch):
    peer, record, _ = gateway
    entered, release = threading.Event(), threading.Event()
    def block(_sid, _kw):
        entered.set()
        assert release.wait(5)
    fake_host(monkeypatch, record, block)
    record['running'] = True
    try:
        # This call itself must return without entering the child-control wait.
        assert server.dispatch({'id': 'activate', 'method': 'session.activate',
                                'params': {'session_id': 'old', 'omit_messages': True}}, peer) is None
        assert entered.wait(5)
        assert rpc(peer, 'approval', 'approval.respond', session_id='old',
                   request_id='absent', choice='deny')['result']['resolved'] == 0
        # Real interrupt helper with only the child-control side effect faked.
        assert rpc(peer, 'stop', 'session.interrupt', session_id='old')['result']['status'] == 'interrupted'
        assert record['_turn_cancel_requested'] is True
    finally:
        release.set()
    assert peer.response('activate')['result']['pending_clarify']['request_id'] == 'child-question'


@pytest.mark.parametrize('change', ['record', 'transport', 'profile', 'boot'])
def test_queued_activation_cannot_adopt_changed_owner(gateway, monkeypatch, change):
    peer, record, _ = gateway
    calls = []
    host = fake_host(monkeypatch, record, lambda *_a: calls.append(True))
    entered, release = threading.Event(), threading.Event()
    def occupy():
        entered.set()
        assert release.wait(5)
    server._pool.submit(occupy)
    assert entered.wait(5)
    try:
        assert server.dispatch({'id': 'activate', 'method': 'session.activate',
                                'params': {'session_id': 'old', 'omit_messages': True}}, peer) is None
        if change == 'record':
            server._sessions['old'] = _session(transport=peer)
        elif change == 'transport':
            record['transport'] = Peer()
        elif change == 'profile':
            record['profile_home'] = '/synthetic/other-profile'
        else:
            host.boot_id = 'successor'
    finally:
        release.set()
    assert peer.response('activate')['error']['code'] == 4001
    assert calls == []
    assert '_activation_pending' not in record


def accept(gateway, batch=False):
    peer, _record, _ = gateway
    emitted = threading.Event()
    results = []
    payload = {'questions': [{'qid': 'q0'}, {'qid': 'q1'}]} if batch else {'question': 'Target?'}
    original = peer.write
    def capture(frame):
        if frame.get('params', {}).get('type') == 'clarify.request':
            emitted.set()
        return original(frame)
    peer.write = capture
    worker = threading.Thread(target=lambda: results.append(server._block(
        'clarify.request', 'old', payload, timeout=10,
        batch_qids=['q0', 'q1'] if batch else None)), daemon=True)
    worker.start()
    try:
        assert emitted.wait(5)
        request_id = payload['request_id']
        if batch:
            assert rpc(peer, 'q0', 'clarify.respond', session_id='old', request_id=request_id,
                       question_id='q0', answer='first')['result']['remaining'] == ['q1']
        params = {'session_id': 'old', 'request_id': request_id, 'answer': 'accepted'}
        if batch:
            params['question_id'] = 'q1'
        assert rpc(peer, 'lost-ack', 'clarify.respond', **params)['result']['status'] == 'ok'
        worker.join(5)
        assert not worker.is_alive() and len(results) == 1
        return params, results
    finally:
        server._clear_pending()
        worker.join(5)


@pytest.mark.parametrize('batch', [False, True])
def test_explicit_stored_resume_confirms_receipt_after_runtime_recreation(gateway, batch):
    peer, _record, _ = gateway
    params, results = accept(gateway, batch)
    server._sessions.pop('old')
    replacement_peer = Peer()
    assert rpc(replacement_peer, 'unattached', 'clarify.respond', **params)['error']['code'] == 4030
    resumed = rpc(replacement_peer, 'resume', 'session.resume', session_id='stored',
                  source='desktop', defer_history=True, omit_messages=True)['result']
    replacement = resumed['session_id']
    assert replacement != 'old'
    assert 'pending_clarify' not in resumed
    for sid in ('old', replacement):
        params['session_id'] = sid
        assert rpc(replacement_peer, 'confirm-' + sid, 'clarify.respond', **params)['result']['status'] == 'ok'
    params['answer'] = 'changed'
    assert rpc(replacement_peer, 'conflict', 'clarify.respond', **params)['result']['status'] == 'conflict'
    assert len(results) == 1 and server._pending == {} and server._answers == {}


@pytest.mark.parametrize('change', ['record', 'transport', 'profile', 'boot'])
def test_activation_rejects_owner_change_during_child_ack(gateway, monkeypatch, change):
    peer, record, _ = gateway
    def change_owner(_sid, _kw):
        if change == 'record':
            server._sessions['old'] = _session(transport=peer, _compute_host_active=True)
        elif change == 'transport':
            record['transport'] = Peer()
        elif change == 'profile':
            record['profile_home'] = '/synthetic/other-profile'
        else:
            host.boot_id = 'successor'
    host = fake_host(monkeypatch, record, change_owner)
    response = rpc(peer, 'activation', 'session.activate', session_id='old', omit_messages=True)
    assert response['error']['code'] == 4001
    assert 'result' not in response and '_activation_pending' not in record


def test_activation_rechecks_owner_immediately_before_attachment(gateway, monkeypatch):
    peer, record, _ = gateway
    other = Peer()
    real = server._methods['session.activate']  # HandlerRegistry-installed function
    def change_before_real(rid, params):
        record['transport'] = other
        return real(rid, params)
    monkeypatch.setitem(server._methods, 'session.activate', change_before_real)
    response = rpc(peer, 'activation', 'session.activate', session_id='old', omit_messages=True)
    assert 'error' in response
    assert record['transport'] is other and '_activation_pending' not in record


def test_queued_activation_cancel_releases_reservation_without_late_rebind(gateway, monkeypatch):
    peer, record, _ = gateway
    calls, timers = [], []
    fake_host(monkeypatch, record, lambda *_a: calls.append(True))
    class Timer(threading.Timer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            timers.append(self)
        def start(self):
            pass  # Advance the real Future.cancel boundary explicitly below.
    monkeypatch.setattr(server.threading, 'Timer', Timer)
    entered, release = threading.Event(), threading.Event()
    def occupy():
        entered.set()
        assert release.wait(5)
    server._pool.submit(occupy)
    assert entered.wait(5)
    try:
        assert server.dispatch({'id': 'queued', 'method': 'session.activate',
                                'params': {'session_id': 'old', 'omit_messages': True}}, peer) is None
        assert rpc(peer, 'duplicate', 'session.activate', session_id='old')['error']['code'] == 4009
        assert timers[0].interval == 25.0
        timers[0].function()  # Exact queued Future, no sleeps or host cancellation.
        assert peer.response('queued')['error']['code'] == 4009
        assert '_activation_pending' not in record and calls == []
    finally:
        release.set()
    server._pool.submit(lambda: None).result(5)
    assert len([f for f in peer.frames if f.get('id') == 'queued']) == 1
    assert calls == [] and record['transport'] is peer
    assert rpc(peer, 'next', 'session.activate', session_id='old', omit_messages=True)['result']
    assert len(calls) == 1


def test_activation_submit_failure_cleans_reservation(gateway, monkeypatch):
    peer, record, _ = gateway
    class Unavailable:
        def submit(self, *_a):
            raise RuntimeError('synthetic unavailable worker')
    monkeypatch.setattr(server, '_pool', Unavailable())
    assert rpc(peer, 'failed', 'session.activate', session_id='old')['error']['code'] == 4009
    assert '_activation_pending' not in record


@pytest.mark.parametrize('change', ['profile', 'stored', 'transport', 'resume_proof', 'closing'])
def test_recreated_receipt_confirmation_rejects_cross_owner_changes(gateway, change):
    peer, record, db = gateway
    params, results = accept(gateway)
    server._sessions.pop('old')
    resumed = rpc(peer, 'resume', 'session.resume', session_id='stored',
                  source='desktop', defer_history=True, omit_messages=True)['result']
    replacement = server._sessions[resumed['session_id']]
    if change == 'profile':
        replacement['profile_home'] = '/synthetic/other-profile'
    elif change == 'stored':
        replacement['session_key'] = 'different-stored-session'
    elif change == 'transport':
        replacement['transport'] = Peer()
        replacement['viewers'] = {}
    elif change == 'resume_proof':
        replacement.pop('_clarify_receipt_resumes', None)
    else:
        replacement['_closing'] = True
    assert rpc(peer, 'forbidden', 'clarify.respond', **params)['error']['code'] == 4030
    assert len(results) == 1 and server._pending == {}


def test_reused_runtime_without_resume_cannot_confirm_or_answer_successor(gateway):
    peer, _record, _ = gateway
    params, results = accept(gateway)
    successor = _session(transport=peer, session_key='stored')
    server._sessions['old'] = successor
    assert rpc(peer, 'forbidden', 'clarify.respond', **params)['error']['code'] == 4030
    # Confirmation may become authorized only by explicit canonical resume.
    assert rpc(peer, 'resume', 'session.resume', session_id='stored',
               defer_history=True, omit_messages=True)['result']['session_id'] == 'old'
    assert rpc(peer, 'confirm', 'clarify.respond', **params)['result']['status'] == 'ok'
    assert len(results) == 1 and server._pending == {} and server._answers == {}


def test_receipt_loss_is_unavailable_proof_and_never_acknowledges_or_replays(gateway):
    peer, _record, _ = gateway
    params, results = accept(gateway)
    server._sessions.pop('old')
    server._clarify_response_receipts.clear()  # Process-local proof lost on restart/TTL.
    resumed = rpc(peer, 'resume', 'session.resume', session_id='stored',
                  defer_history=True, omit_messages=True)['result']
    params['session_id'] = resumed['session_id']
    assert rpc(peer, 'unavailable', 'clarify.respond', **params)['result']['status'] == 'expired'
    assert len(results) == 1 and server._pending == {} and server._answers == {}


# The real child fixture executes ComputeHost._handle_control and the registered
# child receipt helper; only pipe/provider/owner-query boundaries are synthetic.
from tests.tui_gateway.test_host_clarify_bridge import bridge  # noqa: E402,F401


def test_child_receipt_confirms_after_explicit_serving_mirror_recreation(bridge, tmp_path, monkeypatch):
    old, host, _pipe, _rid, respond, inspect, writes, drop, _transport, _batch = bridge
    key = old['session_key']
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session(key, source='desktop', cwd=str(tmp_path))
    peer = Peer()
    monkeypatch.setattr(server, '_db', db)
    monkeypatch.setattr(server, '_hermes_home', tmp_path)
    monkeypatch.setattr(server, '_profile_home', lambda _p: None)
    monkeypatch.setattr(server, '_profile_configured_cwd', lambda _p: str(tmp_path))
    monkeypatch.setattr(server, '_enable_gateway_prompts', lambda: None)
    monkeypatch.setattr(server, '_schedule_resume_hydration', lambda *_a, **_kw: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_lazy_resume_info', lambda *_a, **_kw: {})
    monkeypatch.setattr(server, '_turn_isolation_enabled', lambda: True)
    monkeypatch.setattr(host, 'wait_ready', lambda **_kw: None)
    queries = []
    def lookup(stored, **_kw):
        queries.append(stored)
        assert stored == key
        return {'session_id': 's', 'running': False, 'host_boot_id': host.boot_id}
    monkeypatch.setattr(host, 'lookup_session_key', lookup)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(server, '_pool', pool)
        try:
            server._live_session_payload('s', old, omit_messages=True)
            binding = old['_host_clarify_binding']
            drop.append(True)
            assert respond()['error']['code'] == 5032
            assert inspect() == {'answers': ['yes'], 'pending': False}
            # Use the canonical registry pop, rather than creating a duplicate
            # runtime in a synthetic host-disabled resume topology.
            assert server._pop_session_by_id('s') is old
            resumed = rpc(peer, 'resume-child', 'session.resume', session_id=key,
                          source='desktop', defer_history=True, omit_messages=True)['result']
            assert resumed['session_id'] == 's' and queries == [key]
            replacement = server._sessions['s']
            assert replacement is not old and replacement['_compute_host_active']
            assert replacement['_host_clarify_binding'] == binding
            assert 'pending_clarify' not in resumed
            assert respond(caller=peer)['result'] == {'status': 'ok'}
            assert respond('changed', caller=peer)['result'] == {'status': 'conflict'}
            assert inspect() == {'answers': ['yes'], 'pending': False}
            assert len([f for f in writes if f['route_name'] == 'clarify.respond']) == 3
        finally:
            db.close()
