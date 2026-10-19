import io
import json
import threading

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def test_compress_control_reconstructs_a_resumed_session_without_a_turn(monkeypatch):
    monkeypatch.setenv('HERMES_COMPUTE_HOST_CHILD', '1')
    monkeypatch.setattr(server, '_sessions', {})
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    seeded = []
    called = []
    session = {'session_key': 'durable-original', 'history_lock': threading.Lock(),
               'history': [], 'history_version': 0, 'running': False, 'agent': object()}

    def ensure(owner, snapshot):
        assert owner is server
        seeded.append(snapshot)
        owner._sessions['runtime'] = session
        return session

    def compress(rid, params):
        called.append((rid, params))
        return server._ok(rid, {'status': 'compressed', 'messages': []})

    monkeypatch.setattr(host, '_ensure_server_session', ensure)
    monkeypatch.setitem(server._methods, 'session.compress', compress)
    monkeypatch.setattr(server, '_session_info', lambda *_: {})
    snapshot = {'sid': 'runtime', 'session_key': 'durable-original', 'profile_home': '/synthetic/profile'}
    try:
        host._handle_control({'sid': 'runtime', 'request_id': 'compress-request',
            'route_name': 'session.compress', 'command': '/compress', 'session_snapshot': snapshot})
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        assert frames[-1]['type'] == 'control.ack', frames
        assert seeded == [snapshot]
        assert called == [('compress-request', {'session_id': 'runtime'})]
        assert frames[-1]['session_key'] == 'durable-original'
    finally:
        host.close()


@pytest.mark.parametrize('snapshot', [None, {}, {'sid': 'another-runtime', 'session_key': 'durable-original'}])
def test_compress_control_cannot_reconstruct_from_missing_or_wrong_owner_snapshot(monkeypatch, snapshot):
    monkeypatch.setattr(server, '_sessions', {})
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(host, '_ensure_server_session', lambda *_: pytest.fail('unowned reconstruction'))
    try:
        host._handle_control({'sid': 'runtime', 'request_id': 'request',
            'route_name': 'session.compress', 'session_snapshot': snapshot})
        frame = json.loads(output.getvalue().splitlines()[-1])
        assert frame['type'] == 'control.error'
        assert server._sessions == {}
    finally:
        host.close()


def test_parent_compress_sends_only_its_canonical_owner_snapshot(monkeypatch):
    sid = 'serving-runtime'
    session = {'session_key': 'durable-original', 'history_lock': threading.Lock(),
               'history': [], 'history_version': 0, 'running': False, 'cwd': '/synthetic', 'source': 'desktop'}
    monkeypatch.setattr(server, '_sessions', {sid: session})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _: True)
    calls = []

    def control(target, **kwargs):
        calls.append((target, kwargs))
        return {'type': 'control.ack', 'result': {'status': 'compressed', 'messages': []}}

    monkeypatch.setattr(server, '_send_compute_host_control', control)
    result = server._methods['session.compress']('request', {
        'session_id': sid, 'session_snapshot': {'sid': 'attacker', 'session_key': 'another'}})
    assert 'error' not in result
    payload = calls[0][1].get('payload', {})
    assert payload.get('session_snapshot', {}).get('sid') == sid
    assert payload['session_snapshot']['session_key'] == 'durable-original'
    assert calls[0][0] == sid
