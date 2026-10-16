"""Session-owned effort/Fast writes defer without changing the live request."""
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture
def state(monkeypatch):
    agent = SimpleNamespace(model='fixture', provider='fixture', reasoning_config={'enabled': True, 'effort': 'low'},
                            service_tier=None, request_overrides={})
    # Production sessions use a plain, non-reentrant Lock (server.py session
    # constructors); an RLock here would hide a self-deadlock in the pending apply.
    session = {'session_key': 'stored', 'history_lock': threading.Lock(), 'running': True, 'agent': agent,
               'history': []}
    monkeypatch.setattr(server, '_sessions', {'live': session})
    monkeypatch.setenv('HERMES_COMPUTE_HOST_CHILD', '1')
    monkeypatch.setattr(server, '_load_cfg', lambda: {})
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock())
    monkeypatch.setattr(server, '_write_config_key', Mock(side_effect=AssertionError('global write')))
    monkeypatch.setattr('hermes_cli.models.resolve_fast_mode_overrides', lambda model: {'service_tier': 'priority'})
    return session


def test_busy_combined_options_defer_as_one_validated_intent(state):
    result = server._methods['config.set']('options', {'session_id': 'live', 'key': 'runtime_options',
                                                     'value': {'effort': 'high', 'fast': True}})
    assert result['result']['deferred'] is True
    assert state['agent'].reasoning_config['effort'] == 'low'
    assert state['agent'].service_tier is None
    assert state['pending_model_switch']['runtime_options'] == {'effort': 'high', 'fast': True}
    state['running'] = False
    applied = threading.Thread(target=server._apply_pending_model_switch, args=('live', state),
                               kwargs={'before_admission': True}, daemon=True)
    applied.start()
    applied.join(5)
    if applied.is_alive():
        # Free the stuck thread so a regression fails fast instead of hanging teardown.
        state['history_lock'].release()
        applied.join(5)
        pytest.fail('pending apply deadlocked on the non-reentrant history_lock')
    assert state['agent'].reasoning_config['effort'] == 'high'
    assert state['agent'].service_tier == 'priority'
    assert not state.get('pending_model_switch')


@pytest.mark.parametrize('key,value,expected', [('reasoning','high',{'effort':'high'}), ('fast','normal',{'fast':False})])
def test_real_host_busy_options_are_deferred_not_refused(state, key, value, expected):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._handle_control({'sid': 'live', 'request_id': 'option', 'route_name': 'config.set.'+key,
                              'params': {'value': value}})
        ack = json.loads(output.getvalue().splitlines()[0])
        assert ack['type'] == 'control.ack'
        assert ack['result']['deferred'] is True
        assert state['pending_model_switch']['runtime_options'] == expected
        assert state['agent'].reasoning_config['effort'] == 'low'
        assert state['agent'].service_tier is None
    finally:
        host.close()


def test_invalid_tuple_does_not_change_or_queue_either_dimension(state):
    result = server._methods['config.set']('options', {'session_id': 'live', 'key': 'runtime_options',
                                                     'value': {'effort': 'invalid', 'fast': True}})
    assert result['error']['code'] == 4002
    assert not state.get('pending_model_switch')
    assert state['agent'].service_tier is None


def test_busy_option_updates_merge_dimensions_in_existing_model_intent(state):
    state['pending_model_switch'] = {'raw': 'chosen --session', 'display_model':'chosen', 'display_provider':'fixture'}
    for values in ({'effort':'high'}, {'fast':True}, {'effort':'low'}):
        result = server._methods['config.set']('options', {'session_id':'live', 'key':'runtime_options', 'value':values})
        assert result['result']['deferred']
    assert state['pending_model_switch']['raw'] == 'chosen --session'
    assert state['pending_model_switch']['runtime_options'] == {'effort':'low', 'fast':True}


@pytest.mark.parametrize("replacement", [False, True])
def test_failed_deferred_model_retains_only_owned_options(state, monkeypatch, replacement):
    state['running'] = False
    state['pending_model_switch'] = {'raw': 'chosen --session', 'runtime_options': {'effort': 'high', 'fast': True}}
    original_agent = state['agent']
    def fail(*args, **kwargs):
        if replacement:
            state['agent'] = SimpleNamespace(model='replacement')
        raise RuntimeError('client construction failed')
    monkeypatch.setattr(server, '_apply_model_switch', fail)
    server._apply_pending_model_switch('live', state, before_admission=True)
    if replacement:
        assert not state.get('pending_model_switch')
    else:
        assert state['pending_model_switch'] == {'runtime_options': {'effort': 'high', 'fast': True}}
        server._apply_pending_model_switch('live', state, before_admission=True)
        assert not state.get('pending_model_switch')
        assert original_agent.reasoning_config['effort'] == 'high'
        assert original_agent.service_tier == 'priority'
    server._write_config_key.assert_not_called()


def test_definitive_deferred_option_failure_is_not_requeued(state, monkeypatch):
    state['running'] = False
    state['pending_model_switch'] = {'raw': 'chosen --session', 'runtime_options': {'fast': True}}
    monkeypatch.setattr(server, '_apply_model_switch', Mock(side_effect=RuntimeError('model failed')))
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert state['pending_model_switch'] == {'runtime_options': {'fast': True}}
    # The retained options now belong to their own application attempt on
    # the old model, which does not support Fast. That refusal is terminal.
    monkeypatch.setattr('hermes_cli.models.resolve_fast_mode_overrides', lambda model: None)
    apply = Mock(wraps=server._apply_session_runtime_options)
    monkeypatch.setattr(server, '_apply_session_runtime_options', apply)
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert not state.get('pending_model_switch')
    assert state['agent'].service_tier is None
    assert state['agent'].request_overrides == {}
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert apply.call_count == 1


def test_uncertain_deferred_option_persistence_is_not_automatically_replayed(state, monkeypatch):
    state['running'] = False
    state['pending_model_switch'] = {'runtime_options': {'effort': 'high'}}
    persist = Mock(side_effect=RuntimeError('persistence unconfirmed'))
    monkeypatch.setattr(server, '_persist_live_session_runtime', persist)
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert not state.get('pending_model_switch')
    assert state['agent'].reasoning_config['effort'] == 'low'
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert persist.call_count == 1


def test_failed_deferred_model_preserves_newer_pending_dimensions(state, monkeypatch):
    state['running'] = False
    state['pending_model_switch'] = {'raw': 'old --session', 'runtime_options': {'effort': 'high', 'fast': True}}

    def fail(*args, **kwargs):
        state['pending_model_switch'] = {'raw': 'new --session', 'runtime_options': {'effort': 'low'}}
        raise RuntimeError('client construction failed')

    monkeypatch.setattr(server, '_apply_model_switch', fail)
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert state['pending_model_switch'] == {'raw': 'new --session', 'runtime_options': {'effort': 'low', 'fast': True}}


@pytest.mark.parametrize('change', ['transport', 'generation', 'session'])
def test_failed_model_cannot_requeue_options_across_an_owner_transition(state, monkeypatch, change):
    state['running'] = False
    state['pending_model_switch'] = {'raw': 'old --session', 'runtime_options': {'effort': 'high'}}

    def fail(*args, **kwargs):
        if change == 'transport':
            state['transport'] = object()
        elif change == 'generation':
            state['_queued_prompt_generation'] = 1
        else:
            server._sessions['live'] = {}
        raise RuntimeError('client construction failed')

    monkeypatch.setattr(server, '_apply_model_switch', fail)
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert not state.get('pending_model_switch')


def test_expensive_model_confirmation_retains_options_without_replaying_model(state, monkeypatch):
    state['running'] = False
    state['pending_model_switch'] = {'raw': 'expensive --session', 'runtime_options': {'effort': 'high'}}
    monkeypatch.setattr(server, '_apply_model_switch', lambda *args, **kwargs: {'confirm_required': True})
    server._apply_pending_model_switch('live', state, before_admission=True)
    assert state['pending_model_switch'] == {'runtime_options': {'effort': 'high'}}
    assert state['agent'].reasoning_config['effort'] == 'low'


def test_persist_failure_restores_all_live_option_fields(state, monkeypatch):
    state['running'] = False
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock(side_effect=RuntimeError('fixture failure')))
    result = server._methods['config.set']('options', {'session_id':'live', 'key':'runtime_options',
                                                     'value':{'effort':'high','fast':True}})
    assert result.get('error')
    assert state['agent'].reasoning_config['effort'] == 'low'
    assert state['agent'].service_tier is None
    assert state['agent'].request_overrides == {}


def test_option_persistence_failure_restores_primary_intent_projection(state, monkeypatch):
    state['running'] = False
    old_reasoning = {'enabled': True, 'effort': 'low'}
    state['agent']._primary_runtime = {'service_tier_intent': '', 'reasoning_config': old_reasoning}
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock(side_effect=RuntimeError('unconfirmed')))
    response = server._methods['config.set']('failed', {'session_id': 'live', 'key': 'runtime_options',
                                                       'value': {'fast': True, 'effort': 'high'}})
    assert 'error' in response
    assert state['agent']._primary_runtime['service_tier_intent'] == ''
    assert state['agent']._primary_runtime['reasoning_config'] is old_reasoning
    assert 'create_service_tier_override' not in state
    assert state['agent'].service_tier is None
    assert state['agent'].request_overrides == {}


def test_compute_host_preserves_definitive_fast_rejection_delivery(state, monkeypatch):
    state['agent'].model = 'claude-sonnet-5-5'
    monkeypatch.setattr('hermes_cli.models.resolve_fast_mode_overrides', lambda model: None)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._handle_control({'sid': 'live', 'request_id': 'unsupported-fast',
                              'route_name': 'config.set.runtime_options',
                              'params': {'value': {'fast': True}}})
        ack = json.loads(output.getvalue().splitlines()[0])
        assert ack['type'] == 'control.error'
        assert ack['code'] == 4002
        assert ack['delivery'] == 'not_applied'
        assert state['agent'].service_tier is None
        assert state['agent'].request_overrides == {}
        assert not state.get('pending_model_switch')
    finally:
        host.close()
