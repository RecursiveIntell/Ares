"""Reviewed CPO owner schedules, with actual admission and inert provider boundaries."""
import copy
import threading
from unittest.mock import Mock

import pytest

from tests.tui_gateway.test_once_restore_admission import (  # noqa: F401
    failed_once, intent_turn, agent, gateway, live_turn, routes, turn_env, select, submit,
)
from hermes_cli import model_switch as ms
from tui_gateway import server

REAL_EMIT = server._emit


@pytest.fixture(autouse=True)
def initialize_inert_agent_interrupt_owner(intent_turn):
    # The route fixture uses AIAgent.__new__. Supply its normal pre-execution
    # thread state so the real Stop implementation can run without tool I/O.
    value = intent_turn[0]['agent']
    value._execution_thread_id = None
    value._hard_interrupt_requested = threading.Event()
    value._active_children_lock = threading.Lock()
    value._active_children = []


def test_unexpected_agent_replacement_retains_consumed_A(intent_turn, monkeypatch):
    session, _ = intent_turn
    value = session['agent']
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    lease = session['_one_turn_model_runtime']
    replacement = copy.copy(value)
    replacement.model = 'replacement'
    def offline(*a, **kw):
        session['agent'] = replacement
        session['running'] = False
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    session['running'] = True
    server._run_prompt_submit('old', 'selected', session, 'first')
    assert session.get('_one_turn_model_runtime') is lease
    assert lease['restore_snapshot']['model'] == 'model-a' and lease['restore_failed']
    response = submit()
    assert response['error']['data']['durable_input_accepted'] is False
    assert replacement.model == 'replacement'


def test_Stop_then_new_Send_during_restore_publication_keeps_successor(intent_turn, monkeypatch):
    session, events = intent_turn
    value = session['agent']
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    lease = session['_one_turn_model_runtime']
    monkeypatch.setattr(value, 'run_conversation', lambda *a, **kw: {
        'final_response': 'offline', 'completed': False, 'api_calls': 0})
    old_driver = server._run_prompt_submit
    successor = {}
    sentinel = object()
    markers = []
    monkeypatch.setattr(server, '_retire_turn_marker', lambda *a: markers.append('retire'))
    def claim_only(*a, **kw):
        successor['turn'] = session['inflight_turn']
        successor['nonce'] = session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id']
        return True
    def restart(*a):
        successor['entered'] = True
        try:
            successor['stop'] = server._methods['session.interrupt']('stop', {'session_id': 'selected'})
            monkeypatch.setattr(server, '_run_prompt_submit', claim_only)
            monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
            monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
            successor['response'] = submit('successor')
            value.interim_assistant_callback = sentinel
            successor['markers'] = len(markers)
        except Exception as exc:
            successor['exception'] = repr(exc)
            raise
    monkeypatch.setattr(server, '_restart_slash_worker', restart)
    session['running'] = True
    old_driver('old', 'selected', session, 'first')
    assert successor.get('entered') and 'exception' not in successor, successor
    assert not successor['stop'].get('error'), successor
    assert 'turn' in successor, successor
    assert not successor['response'].get('error'), successor
    assert session['running'] and session['inflight_turn'] is successor['turn']
    assert value.interim_assistant_callback is sentinel
    assert len(markers) == successor['markers']
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']
    assert not any(e == 'error' and p.get('error_surface', {}).get('code') ==
        'one_turn_model_restore_failed' for e, _, p in events)


@pytest.mark.parametrize('changed', ['session', 'agent', 'transport', 'Stop'])
def test_pending_resolution_rechecks_before_acceptance(failed_once, monkeypatch, changed):
    session, events, lease, _ = failed_once
    session['pending_model_switch'] = {'raw': 'model-c --provider endpoint-c --session',
        'confirm_expensive_model': True}
    original = copy.deepcopy(session['history'])
    session['attached_images'] = ['owned-image']
    real = ms.switch_model
    replacement = copy.copy(session)
    def resolve(*a, **kw):
        result = real(*a, **kw)
        if changed == 'session': server._sessions['selected'] = replacement
        elif changed == 'agent': session['agent'] = copy.copy(session['agent'])
        elif changed == 'transport': session['transport'] = object()
        else:
            assert not server._methods['session.interrupt']('stop', {'session_id': 'selected'}).get('error')
            events.clear()
        return result
    monkeypatch.setattr(ms, 'switch_model', resolve)
    accept = Mock(side_effect=AssertionError('stale acceptance'))
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    events.clear()
    response = submit('stale input')
    accept.assert_not_called()
    assert response.get('error')
    assert response['error']['data']['durable_input_accepted'] is False
    assert session['history'] == original and session['attached_images'] == ['owned-image']
    assert not events
    assert session['_one_turn_model_runtime'] is lease


@pytest.mark.parametrize('changed', ['session', 'transport'])
def test_explicit_suffix_owner_loss_is_refusal_without_successor_delivery(failed_once, monkeypatch, changed):
    session, events, lease, _ = failed_once
    def restart(*a):
        if changed == 'session': server._sessions['selected'] = copy.copy(session)
        else: session['transport'] = object()
        events.clear()
    monkeypatch.setattr(server, '_restart_slash_worker', restart)
    response = server.handle_request(select('model-c', 'endpoint-c'))
    assert response.get('error') and 'owner changed' in response['error']['message']
    assert not events
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']


def test_explicit_slash_commit_cannot_bypass_changed_owner(failed_once, monkeypatch):
    session, _, lease, _ = failed_once
    real = ms.switch_model
    def resolve(*a, **kw):
        result = real(*a, **kw)
        server._sessions['selected'] = copy.copy(session)
        return result
    monkeypatch.setattr(ms, 'switch_model', resolve)
    with pytest.raises(ValueError, match='owner changed'):
        server._apply_model_switch('selected', session, 'model-c --provider endpoint-c --session',
            confirm_expensive_model=True, explicit_model_intent=True, defer_if_running=False)
    assert session['agent'].model == 'model-b' and session['_one_turn_model_runtime'] is lease


@pytest.mark.parametrize('stop_after_first', [False, True])
@pytest.mark.parametrize('host_policy', [False, True])
def test_each_queued_image_refusal_keeps_receipt_and_Stop_cut(failed_once, monkeypatch, tmp_path, stop_after_first, host_policy):
    session, events, lease, _ = failed_once
    history = copy.deepcopy(session['history'])
    # Canonical governed text receipts are accepted before the inert image
    # envelopes are constructed. Fresh governed image RPCs remain refused by
    # the existing context owner; this does not certify that separate route.
    session['agent'].context_rebase_enabled = True
    paths = []
    receipts = []
    for n in range(2):
        p = tmp_path / f'image{n}.png';p.write_bytes(b'inert-image');paths.append(str(p))
        receipt = server._accept_tui_context_input(session, f'owned input {n}')
        receipts.append(receipt.event_id)
        with session['history_lock']:
            server._enqueue_prompt(session, f'owned input {n}', session['transport'],
                image_paths=[str(p)], context_input_event_id=receipt.event_id)
    session['attached_images'] = ['unclaimed-attachment']
    provider = Mock(side_effect=AssertionError('refused provider dispatch'))
    monkeypatch.setattr(session['agent'], 'run_conversation', provider)
    host = Mock(side_effect=AssertionError('refused host dispatch'))
    monkeypatch.setattr(server, '_submit_prompt_to_compute_host', host)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a, **kw: host_policy)
    def capture(event, sid, payload=None):
        events.append((event, sid, payload))
        REAL_EMIT(event, sid, payload)
        if stop_after_first and event == 'message.complete':
            assert not server._methods['session.interrupt']('stop', {'session_id': 'selected'}).get('error')
    monkeypatch.setattr(server, '_emit', capture)
    events.clear()
    assert server._drain_queued_prompt('drain', 'selected', session)
    terminal = [p for e, _, p in events if e == 'message.complete']
    assert [p.get('input_event_id') for p in terminal] == receipts[:1 if stop_after_first else 2]
    assert all(p['durable_input_accepted'] and p['execution_started'] is False
        and p['error_surface']['code'] == 'one_turn_model_restore_failed' for p in terminal)
    provider.assert_not_called();host.assert_not_called()
    db = session['agent']._session_db
    assert [db.read_context_input(session['session_key'], source='tui', event_id=e).content
        for e in receipts] == ['owned input 0', 'owned input 1']
    wire = [f['params']['payload'] for f in session['transport'].frames
        if f.get('params', {}).get('type') == 'message.complete']
    assert [p['input_event_id'] for p in wire] == receipts[:1 if stop_after_first else 2]
    assert len({p['accepted_turn']['request_id'] for p in wire}) == len(wire)
    assert all(t['state'] == 'error' for t in session['_turn_outcomes'].turns)
    assert all(__import__('pathlib').Path(p).read_bytes() == b'inert-image' for p in paths)
    assert session['history'] == history and session['attached_images'] == ['unclaimed-attachment']
    assert session['_one_turn_model_runtime'] is lease and not session['running']


def test_busy_Send_other_transport_settles_original_then_drains(intent_turn, monkeypatch):
    from tests.tui_gateway.test_config_dispatch_responsiveness import Transport
    session, events = intent_turn
    old_sink = session['transport']; new_sink = Transport()
    value = session['agent']; value.context_rebase_enabled = True
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    calls = []
    queued = {}
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    def offline(*a, persist_user_event_id=None, **kw):
        assert persist_user_event_id is not None
        accepted = value._session_db.read_context_input(session['session_key'],
            source='tui', event_id=persist_user_event_id)
        assert accepted.content in ('first input', 'second input')
        calls.append(value.model)
        if len(calls) == 1:
            queued['ack'] = server.dispatch({'id': 'busy-send', 'method': 'prompt.submit',
                'params': {'session_id': 'selected', 'text': 'second input'}}, new_sink)
        return {'final_response': calls[-1], 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(value, 'run_conversation', offline)
    first = server.dispatch({'id': 'first-send', 'method': 'prompt.submit',
        'params': {'session_id': 'selected', 'text': 'first input'}}, old_sink)
    assert not first.get('error') and not queued['ack'].get('error')
    assert queued['ack']['result']['status'] == 'queued'
    assert calls == ['model-b', 'model-a']
    assert value.model == 'model-a' and not session.get('_one_turn_model_runtime')
    assert not session['running'] and not session.get('inflight_turn') and not session.get('queued_prompt')
    def finals(sink):
        return [f['params']['payload'] for f in sink.frames
            if f.get('params', {}).get('type') == 'message.complete']
    assert [p['text'] for p in finals(old_sink)] == ['model-b']
    assert [p['text'] for p in finals(new_sink)] == ['model-a']


@pytest.mark.parametrize('successor', [False, True])
def test_Stop_before_queued_refusal_finishes_only_captured_nonce(failed_once, monkeypatch, successor):
    session, events, lease, _ = failed_once
    session['agent'].context_rebase_enabled = True
    receipt = server._accept_tui_context_input(session, 'accepted queued input')
    with session['history_lock']:
        server._enqueue_prompt(session, 'accepted queued input', session['transport'],
            context_input_event_id=receipt.event_id)
    real_terminal = server._emit_terminal_turn_error
    captured = {}; sentinel = object()
    def claim(*a, **kw):
        captured['successor_turn'] = session['inflight_turn']
        session['agent'].interim_assistant_callback = sentinel
        return True
    def before_helper(*a, **kw):
        window = session['_turn_outcomes']
        captured['nonce'] = window.turns[-1]['accepted_turn']['request_id']
        captured['stop'] = server._methods['session.interrupt']('stop', {'session_id': 'selected'})
        if successor:
            captured['pick'] = server.handle_request(select('model-c', 'endpoint-c'))
            monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
            monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
            monkeypatch.setattr(server, '_run_prompt_submit', claim)
            captured['send'] = submit('successor')
        captured['events_before'] = len(events)
        captured['settled'] = real_terminal(*a, **kw)
        return captured['settled']
    monkeypatch.setattr(server, '_emit_terminal_turn_error', before_helper)
    assert server._drain_queued_prompt('drain', 'selected', session)
    assert not captured['stop'].get('error') and captured['settled'] is False
    assert session['_turn_outcomes'].find(captured['nonce'])['state'] == 'interrupted'
    assert len(events) == captured['events_before']
    if successor:
        assert not captured['pick'].get('error') and not captured['send'].get('error'), captured
        assert session['running'] and session['inflight_turn'] is captured['successor_turn']
        assert session['agent'].interim_assistant_callback is sentinel
    else:
        assert not session['running'] and session['_one_turn_model_runtime'] is lease


def test_settled_metadata_transport_callback_can_acquire_admission_lock(intent_turn, monkeypatch):
    session, _ = intent_turn
    observed = []
    value = session['agent']
    monkeypatch.setattr(value, 'run_conversation', lambda *a, **kw: {
        'final_response': 'offline', 'completed': False, 'api_calls': 0})
    sink = session['transport']; real_write = sink.write
    def write(frame):
        if frame.get('params', {}).get('type') == 'session.info' and not session['running']:
            acquired = session['history_lock'].acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                session['history_lock'].release()
        return real_write(frame)
    monkeypatch.setattr(sink, 'write', write)
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    session['running'] = True
    server._run_prompt_submit('old', 'selected', session, 'first')
    assert observed == [True]


@pytest.mark.parametrize('changed', ['agent', 'transport'])
def test_pending_pop_owner_is_carried_into_switch_commit(failed_once, monkeypatch, changed):
    session, events, lease, _ = failed_once
    session['pending_model_switch'] = {'raw': 'model-c --provider endpoint-c --session',
        'confirm_expensive_model': True}
    original_agent = session['agent']; replacement = copy.copy(original_agent)
    real_apply = server._apply_model_switch
    def before_entry(*a, **kw):
        if changed == 'agent': session['agent'] = replacement
        else: session['transport'] = object()
        events.clear()
        return real_apply(*a, **kw)
    monkeypatch.setattr(server, '_apply_model_switch', before_entry)
    accept = Mock(side_effect=AssertionError('stale acceptance'))
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    response = submit('old input')
    accept.assert_not_called()
    assert response.get('error') and response['error']['data']['durable_input_accepted'] is False
    assert replacement.model == original_agent.model == 'model-b'
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']
    assert not events


def test_pending_resolution_error_cannot_deliver_after_new_Send(failed_once, monkeypatch):
    session, events, _, _ = failed_once
    session['agent'].context_rebase_enabled = True
    session['pending_model_switch'] = {'raw': 'model-c --provider endpoint-c --session',
        'confirm_expensive_model': True}
    real_resolve = ms.switch_model; accepted = []; successor = {}; sentinel = object()
    real_accept = server._accept_tui_context_input
    def accept(*a, **kw):
        accepted.append(a[1]);return real_accept(*a, **kw)
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    def claim(*a, **kw):
        successor['turn'] = session['inflight_turn']
        successor['nonce'] = session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id']
        session['agent'].interim_assistant_callback = sentinel
        return True
    def resolve(*a, **kw):
        monkeypatch.setattr(ms, 'switch_model', real_resolve)
        successor['pick'] = server.handle_request(select('model-c', 'endpoint-c'))
        monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
        monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
        monkeypatch.setattr(server, '_run_prompt_submit', claim)
        successor['send'] = submit('new owner')
        events.clear()
        raise RuntimeError('old pending resolution failed')
    monkeypatch.setattr(ms, 'switch_model', resolve)
    response = submit('old pending input')
    assert not successor['pick'].get('error') and not successor['send'].get('error'), successor
    assert response.get('error') and response['error']['data']['durable_input_accepted'] is False
    assert accepted == ['new owner'] and not events
    assert session['running'] and session['inflight_turn'] is successor['turn']
    assert session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == successor['nonce']
    assert session['agent'].interim_assistant_callback is sentinel


def test_Stop_new_Send_at_queue_policy_boundary_keeps_successor(failed_once, monkeypatch):
    session, events, _, _ = failed_once
    session['agent'].context_rebase_enabled = True
    receipt = server._accept_tui_context_input(session, 'old queued input')
    with session['history_lock']:
        server._enqueue_prompt(session, 'old queued input', session['transport'],
            context_input_event_id=receipt.event_id)
    entered = []; successor = {}; sentinel = object(); markers = []
    monkeypatch.setattr(server, '_retire_turn_marker', lambda *a: markers.append('retire'))
    def claim(*a, **kw):
        successor['turn'] = session['inflight_turn']
        successor['nonce'] = session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id']
        session['agent'].interim_assistant_callback = sentinel
        return True
    def policy(*a, **kw):
        if not entered:
            entered.append(True)
            successor['stop'] = server._methods['session.interrupt']('stop', {'session_id': 'selected'})
            successor['pick'] = server.handle_request(select('model-c', 'endpoint-c'))
            monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
            monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
            monkeypatch.setattr(server, '_run_prompt_submit', claim)
            successor['send'] = submit('new owner')
            successor['markers'] = len(markers)
            events.clear()
        return False
    monkeypatch.setattr(server, '_session_uses_compute_host', policy)
    assert server._drain_queued_prompt('drain', 'selected', session)
    assert all(not successor[k].get('error') for k in ('stop', 'pick', 'send')), successor
    assert session['running'] and session['inflight_turn'] is successor['turn']
    assert session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == successor['nonce']
    assert session['agent'].interim_assistant_callback is sentinel
    assert len(markers) == successor['markers'] and not events
