"""Explicit alternate routes with saved native defaults; all transports inert."""
from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp
from tests.hermes_cli.test_codex_app_server_selection_init import offline_route_boundaries  # noqa: F401


@pytest.mark.parametrize('native_key', ['api_mode', 'openai_runtime'])
@pytest.mark.parametrize('configured_provider', ['openai', 'openai-codex'])
def test_explicit_anthropic_resolves_before_inherited_native_mode(
    offline_route_boundaries, native_key, configured_provider
):
    state = offline_route_boundaries
    state.config = {'model': {'provider': configured_provider, native_key: 'codex_app_server'},
                    'compression': {}}
    state.provider = 'anthropic'
    result = rp.resolve_runtime_provider(requested='anthropic')
    assert result['provider'] == result['requested_provider'] == 'anthropic'
    assert result['api_mode'] == 'anthropic_messages'
    assert result['api_key'] == 'offline-dummy-key'
    assert result['credential_pool'] is state.pool
    assert 'native' not in result['source']


@pytest.mark.parametrize('native_key', ['api_mode', 'openai_runtime'])
def test_explicit_anthropic_cannot_bypass_required_native_trial(
    offline_route_boundaries, native_key
):
    state = offline_route_boundaries
    state.config = {'model': {'provider': 'openai', native_key: 'codex_app_server'},
                    'compression': {'context_rebase_enabled': True}}
    state.provider = 'anthropic'
    with pytest.raises(ValueError, match='codex_app_server'):
        rp.resolve_runtime_provider(requested='anthropic')
    assert state.calls == []


def test_explicit_anthropic_still_obeys_disabled_target(offline_route_boundaries):
    state = offline_route_boundaries
    state.config = {'model': {'provider': 'openai', 'api_mode': 'codex_app_server'},
                    'providers': {'anthropic': {'enabled': False}}}
    with pytest.raises(ValueError, match='disabled'):
        rp.resolve_runtime_provider(requested='anthropic')
    assert state.calls == []


@pytest.mark.parametrize('native_key', ['api_mode', 'openai_runtime'])
@pytest.mark.parametrize('continuity_required', [False, True])
def test_actual_direct_init_explicit_anthropic_route(
    monkeypatch, tmp_path, native_key, continuity_required
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ''
    config = {'model': {'provider': 'openai', 'default': 'dummy-model', native_key: 'codex_app_server'},
              'compression': {'context_rebase_enabled': continuity_required}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: config)
    monkeypatch.setattr('hermes_cli.model_normalize.normalize_model_for_provider', lambda model, provider: model)
    monkeypatch.setattr('agent.model_metadata.query_ollama_num_ctx', lambda *a, **kw: None)
    monkeypatch.setattr('run_agent.get_tool_definitions', lambda *a, **kw: [])
    monkeypatch.setattr('agent.iteration_budget.IterationBudget', lambda *a: SimpleNamespace())
    monkeypatch.setattr('hermes_cli.config.cfg_get', lambda *a, **kw: None)
    monkeypatch.setattr('tools.lazy_deps.ensure', lambda *a, **kw: None)
    monkeypatch.setattr(agent, '_get_transport', lambda: None, raising=False)
    clients = []
    def client(*args, **kwargs):
        clients.append((args, kwargs))
        return object()
    monkeypatch.setattr('agent.anthropic_adapter.build_anthropic_client', client)
    monkeypatch.setattr(agent, '_create_openai_client', lambda *a, **kw: pytest.fail('wrong OpenAI SDK'), raising=False)
    kwargs = dict(provider='anthropic', api_mode='anthropic_messages', model='dummy-model',
                  api_key='synthetic-anthropic-key', base_url='https://api.anthropic.com',
                  skip_memory=True, skip_context_files=True, quiet_mode=True)
    if continuity_required:
        with pytest.raises(ValueError, match='codex_app_server'):
            init_agent(agent, **kwargs)
        assert not clients
    else:
        init_agent(agent, **kwargs)
        assert (agent.provider, agent.api_mode) == ('anthropic', 'anthropic_messages')
        assert agent._anthropic_client is not None
        assert agent.client is None
        assert len(clients) == 1
        assert clients[0][0][:2] == ('synthetic-anthropic-key', 'https://api.anthropic.com')
