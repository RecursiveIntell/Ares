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


def test_persist_failure_restores_all_live_option_fields(state, monkeypatch):
    state['running'] = False
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock(side_effect=RuntimeError('fixture failure')))
    result = server._methods['config.set']('options', {'session_id':'live', 'key':'runtime_options',
                                                     'value':{'effort':'high','fast':True}})
    assert result.get('error')
    assert state['agent'].reasoning_config['effort'] == 'low'
    assert state['agent'].service_tier is None
    assert state['agent'].request_overrides == {}
