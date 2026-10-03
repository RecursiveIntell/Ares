"""Real selectors and session commit/metadata boundaries with inert clients."""
import copy
import threading

import pytest

from hermes_cli.config import load_config
from tests.hermes_cli.test_model_switch_route_contract import routes  # noqa: F401
from tests.hermes_cli.test_model_switch_runtime_boundary import agent, assert_route  # noqa: F401
from tui_gateway import server


@pytest.fixture
def gateway(routes, agent, monkeypatch):
    value, _ = agent
    session = {'agent': value, 'history': [], 'history_lock': threading.Lock(),
               'model_override': {'model': 'model-a', 'provider': 'endpoint-a'},
               'model_verified_for': ('endpoint-a', 'model-a')}
    events = []
    monkeypatch.setattr(server, '_sessions', {'selected': session})
    monkeypatch.setattr(server, '_load_cfg', load_config)
    monkeypatch.setattr(server, '_restart_slash_worker', lambda *a: None)
    monkeypatch.setattr(server, '_persist_live_session_runtime', lambda *a: None)
    monkeypatch.setattr(server, '_persist_live_session_system_prompt', lambda *a: None)
    monkeypatch.setattr(server, '_probe_credentials', lambda *a: None)
    monkeypatch.setattr(server, '_git_branch_for_cwd', lambda *a: None)
    monkeypatch.setattr(server, '_project_info_for_cwd', lambda *a: None)
    monkeypatch.setattr(server, '_emit', lambda name, sid, payload: events.append((name, sid, payload)))
    monkeypatch.setattr('hermes_cli.banner.get_available_skills', lambda: {})
    monkeypatch.setattr('hermes_cli.banner.get_update_result', lambda **kw: None)
    monkeypatch.setattr('tools.mcp_tool.get_mcp_status', lambda: [])
    return session, events


def test_explicit_session_switch_uses_target_and_does_not_change_profile(routes, gateway):
    session, events = gateway
    other = {'model_override': {'model': 'other', 'provider': 'endpoint-a'}}
    server._sessions['other'] = other
    prior = (routes.home / 'config.yaml').read_bytes()
    result = server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --session',
                                      confirm_expensive_model=True, persist_override=False)
    assert result['scope'] == 'session'
    assert_route(session['agent'], 'http://b.invalid/v1', 'synthetic-key-b')
    assert session['model_override']['provider'] == 'endpoint-b'
    assert session['model_override']['api_key'] == 'synthetic-key-b'
    assert other == {'model_override': {'model': 'other', 'provider': 'endpoint-a'}}
    assert (routes.home / 'config.yaml').read_bytes() == prior
    assert events[-1][2]['model'] == 'chosen-model'


def test_gateway_once_commit_and_restore_preserve_pin(routes, gateway):
    session, events = gateway
    pin = copy.deepcopy(session['model_override'])
    prior = (routes.home / 'config.yaml').read_bytes()
    result = server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --once',
                                      confirm_expensive_model=True, persist_override=False)
    assert result['scope'] == 'once'
    assert_route(session['agent'], 'http://b.invalid/v1', 'synthetic-key-b')
    assert session['model_override'] == pin
    assert (events[-1][2]['model'], events[-1][2]['provider']) == ('chosen-model', 'endpoint-b')
    server._restore_agent_model_runtime(session['agent'], session.pop('one_turn_model_restore'))
    assert_route(session['agent'], 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['agent'].model == 'model-a'
    info = server._session_info(session['agent'], session)
    assert (info['model'], info['provider']) == ('model-a', 'endpoint-a')
    assert session['model_override'] == pin
    assert (routes.home / 'config.yaml').read_bytes() == prior


def test_disabled_session_target_does_not_publish_or_commit(routes, gateway):
    session, events = gateway
    routes.config['providers']['endpoint-b']['enabled'] = False
    routes.write()
    pin = copy.deepcopy(session['model_override'])
    with pytest.raises(ValueError, match='disabled'):
        server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --session',
                                  confirm_expensive_model=True, persist_override=False)
    assert_route(session['agent'], 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['model_override'] == pin
    assert session['model_verified_for'] == ('endpoint-a', 'model-a')
    assert not events


@pytest.mark.parametrize('provider', ['endpoint-b', ''])
def test_queued_selection_precedes_old_session_pin(gateway, provider):
    session, _ = gateway
    session['pending_model_switch'] = {'display_model': 'queued-model', 'display_provider': provider}
    info = server._session_info(session['agent'], session)
    assert (info['model'], info['provider']) == ('queued-model', provider or 'endpoint-a')
    assert info['model_ready'] is False
    assert session['model_override'] == {'model': 'model-a', 'provider': 'endpoint-a'}


def test_idle_selection_still_uses_session_pin(gateway):
    session, _ = gateway
    session['model_override'] = {'model': 'pinned-model', 'provider': 'pinned-provider'}
    info = server._session_info(session['agent'], session)
    assert (info['model'], info['provider']) == ('pinned-model', 'pinned-provider')


def test_compute_host_metadata_keeps_its_owner(gateway):
    session, _ = gateway
    session['_compute_host_active'] = True
    session['_metadata_mirror'] = {'model': 'host-model', 'provider': 'host-provider'}
    info = server._session_info(session['agent'], session)
    assert (info['model'], info['provider']) == ('host-model', 'host-provider')
