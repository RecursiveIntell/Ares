"""Actual Stop once settlement and later-owner controls through real gateway paths."""
from unittest.mock import Mock

import pytest

from tests.tui_gateway.test_once_restore_owner_races import (  # noqa: F401
    agent, gateway, intent_turn, live_turn, routes, turn_env, select, submit,
    initialize_inert_agent_interrupt_owner, REAL_EMIT,
)
from tui_gateway import server


def stop():
    response = server._methods['session.interrupt']('stop', {'session_id': 'selected'})
    assert not response.get('error'), response


def pick_once(session):
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    return session['_one_turn_model_runtime']


@pytest.mark.parametrize('admitted', [False, True])
def test_actual_Stop_only_restores_A_and_publishes_owned_idle_metadata(intent_turn, monkeypatch, admitted):
    session, _ = intent_turn
    value = session['agent']; sink = session['transport']
    lease = pick_once(session)
    calls = []; lock_observations = []
    real_restore = server._restore_agent_model_runtime
    def restore(*a):
        calls.append(a[1]['model']); return real_restore(*a)
    monkeypatch.setattr(server, '_restore_agent_model_runtime', restore)
    real_write = sink.write
    def write(frame):
        p = frame.get('params', {})
        if p.get('type') == 'session.info' and not session['running'] and p['payload']['model'] == 'model-a':
            acquired = session['history_lock'].acquire(blocking=False)
            lock_observations.append(acquired)
            if acquired: session['history_lock'].release()
        return real_write(frame)
    monkeypatch.setattr(sink, 'write', write)
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
    def offline(*a, persist_user_event_id=None, **kw):
        stop()
        return {'final_response': 'offline', 'interrupted': True, 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    if admitted:
        response = submit('first'); assert not response.get('error'), response
    else:
        session['running'] = True
        server._run_prompt_submit('old', 'selected', session, 'first')
    assert calls == ['model-a'] and value.model == 'model-a'
    assert session.get('_one_turn_model_runtime') is None and not lease.get('restore_failed')
    assert not session['running'] and session.get('inflight_turn') is None
    assert session['_turn_cancel_requested'] and lock_observations == [True]
    if admitted:
        assert session['_turn_outcomes'].turns[-1]['state'] == 'interrupted'


@pytest.mark.parametrize('failure', ['restore', 'publication'])
def test_actual_Stop_restore_failure_keeps_A_custody_and_refuses_Send(intent_turn, monkeypatch, failure):
    session, events = intent_turn; value = session['agent']; lease = pick_once(session)
    snapshot = lease['restore_snapshot']; calls = []
    def offline(*a, **kw):
        calls.append(value.model); stop()
        return {'final_response': 'offline', 'interrupted': True, 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    def refuse(*a): raise RuntimeError('offline restore/publication refusal')
    monkeypatch.setattr(server, '_restore_agent_model_runtime' if failure == 'restore'
        else '_persist_live_session_runtime', refuse)
    session['running'] = True
    server._run_prompt_submit('old', 'selected', session, 'first')
    assert session['_one_turn_model_runtime'] is lease and lease['restore_snapshot'] is snapshot
    assert snapshot['model'] == 'model-a' and lease['restore_failed'] and not lease['active']
    assert any(e == 'error' and p.get('error_surface', {}).get('code') ==
        'one_turn_model_restore_failed' for e, _, p in events)
    accept = Mock(side_effect=AssertionError('failed restoration accepted another input'))
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    response = submit('refused later input')
    assert response['error']['data']['error_surface']['code'] == 'one_turn_model_restore_failed'
    assert response['error']['data']['durable_input_accepted'] is False
    accept.assert_not_called(); assert calls == ['model-b']


@pytest.mark.parametrize('once', [False, True])
def test_actual_Stop_newer_explicit_C_remains_authoritative(intent_turn, monkeypatch, once):
    session, _ = intent_turn; value = session['agent']; old_lease = pick_once(session)
    seen = {}; restore = Mock(side_effect=AssertionError('old A restored over explicit C'))
    monkeypatch.setattr(server, '_restore_agent_model_runtime', restore)
    def offline(*a, **kw):
        stop()
        response = server.handle_request(select('model-c', 'endpoint-c', once=once))
        assert not response.get('error'), response
        seen['lease'] = session.get('_one_turn_model_runtime')
        return {'final_response': 'offline', 'interrupted': True, 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    session['running'] = True
    server._run_prompt_submit('old', 'selected', session, 'first')
    restore.assert_not_called(); assert value.model == 'model-c' and not session['running']
    assert session.get('_one_turn_model_runtime') is seen['lease']
    if once:
        assert seen['lease'] is not old_lease and not seen['lease'].get('restore_failed')
        assert not seen['lease']['active'] and session.get('one_turn_model_restore') is seen['lease']['restore_snapshot']
    else: assert seen['lease'] is None


@pytest.mark.parametrize('queued', [False, True])
def test_actual_Stop_new_Send_then_Stop_does_not_borrow_cleared_successor(intent_turn, monkeypatch, queued):
    session, _ = intent_turn; value = session['agent']; lease = pick_once(session)
    old_driver = server._run_prompt_submit; seen = {}; sentinel = object(); markers = []
    monkeypatch.setattr(server, '_retire_turn_marker', lambda *a: markers.append('retire'))
    restore = Mock(side_effect=AssertionError('old A restored after a newer stopped admission'))
    monkeypatch.setattr(server, '_restore_agent_model_runtime', restore)
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
    monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
    def claim(*a, **kw):
        seen['nonce'] = session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id']
        value.interim_assistant_callback = sentinel
        return True
    def offline(*a, **kw):
        stop()
        monkeypatch.setattr(server, '_run_prompt_submit', claim)
        response = submit('newer admission'); assert not response.get('error'), response
        stop(); seen['markers'] = len(markers)
        return {'final_response': 'offline', 'interrupted': True, 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    if queued:
        with session['history_lock']:
            server._enqueue_prompt(session, 'first', session['transport'])
        assert server._drain_queued_prompt('queued', 'selected', session)
    else:
        session['running'] = True
        old_driver('old', 'selected', session, 'first')
    restore.assert_not_called(); assert value.model == 'model-b'
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']
    assert session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == seen['nonce']
    assert value.interim_assistant_callback is sentinel and len(markers) == seen['markers']
