"""Real selector-to-live-agent switching and one-turn restoration, inert SDK only."""
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_model_switch_route_contract import routes  # noqa: F401
from tests.run_agent.test_switch_model_rollback import _make_agent_openrouter
from hermes_cli import model_switch as ms
from hermes_cli.config import load_config, get_compatible_custom_providers


@pytest.fixture
def agent(routes, monkeypatch):
    value = _make_agent_openrouter()
    value.quiet_mode = True
    clients = []

    def create(kwargs, **_):
        client = SimpleNamespace(kwargs=kwargs.copy())
        clients.append(client)
        return client

    value._create_openai_client = create
    monkeypatch.setattr('hermes_cli.timeouts.get_provider_request_timeout', lambda *a, **k: None)
    value.switch_model('model-a', 'endpoint-a', 'synthetic-key-a', 'http://a.invalid/v1', 'chat_completions')
    return value, clients


def select(agent, provider):
    cfg = load_config()
    result = ms.switch_model('chosen-model', agent.provider, agent.model, agent.base_url, agent.api_key,
                             explicit_provider=provider, user_providers=cfg['providers'],
                             custom_providers=get_compatible_custom_providers(cfg))
    assert result.success, result.error_message
    agent.switch_model(result.new_model, result.target_provider, result.api_key, result.base_url, result.api_mode)
    return result


def assert_route(agent, endpoint, key):
    assert (agent.base_url, agent.api_key) == (endpoint, key)
    assert (agent.client.kwargs['base_url'], agent.client.kwargs['api_key']) == (endpoint, key)
    assert (agent._primary_runtime['base_url'], agent._primary_runtime['api_key']) == (endpoint, key)


def test_live_agent_warm_a_b_a_preserves_endpoint_key_client_pair(agent):
    value, clients = agent
    select(value, 'endpoint-b')
    assert_route(value, 'http://b.invalid/v1', 'synthetic-key-b')
    select(value, 'endpoint-a')
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert len(clients) == 3


def test_gateway_once_restores_original_target_runtime(agent):
    from tui_gateway import server

    value, _ = agent
    snapshot = server._snapshot_agent_model_runtime(value)
    select(value, 'endpoint-b')
    server._restore_agent_model_runtime(value, snapshot)
    assert (value.model, value.provider) == ('model-a', 'endpoint-a')
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')


def test_cli_once_restores_shell_and_real_agent_runtime(agent):
    from cli import HermesCLI

    value, _ = agent
    shell = SimpleNamespace(model=value.model, provider=value.provider, requested_provider=value.provider,
                            api_key=value.api_key, base_url=value.base_url, api_mode=value.api_mode,
                            _explicit_api_key=None, _explicit_base_url=None, agent=value)
    snapshot = HermesCLI._snapshot_model_runtime(shell)
    select(value, 'endpoint-b')
    for name in ('model', 'provider', 'api_key', 'base_url', 'api_mode'):
        setattr(shell, name, getattr(value, name))
    shell.requested_provider = 'endpoint-b'
    shell._explicit_api_key = value.api_key
    shell._explicit_base_url = value.base_url
    HermesCLI._restore_model_runtime_snapshot(shell, snapshot)
    assert (shell.provider, shell.requested_provider, shell._explicit_api_key, shell._explicit_base_url) == ('endpoint-a', 'endpoint-a', None, None)
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')


@pytest.mark.parametrize('provider', ['opencode-zen', 'openai-api', 'glm'])
def test_builtin_and_alias_selection_resolves_target_owned_credential(routes, provider):
    result = routes.switch(provider=provider)
    assert result.success, result.error_message
    expected = {'opencode-zen': 'synthetic-zen-key', 'openai-api': 'synthetic-openai-key', 'glm': 'synthetic-zai-key'}
    assert result.api_key == expected[provider]
    assert result.base_url != 'http://a.invalid/v1'


def test_named_keyless_target_does_not_borrow_current_or_ambient_key(routes):
    routes.config['providers']['endpoint-b'].pop('api_key')
    routes.write()
    result = routes.switch()
    assert result.success, result.error_message
    assert result.api_key == 'no-key-required'
    assert result.base_url == 'http://b.invalid/v1'


def test_failed_selector_does_not_mutate_live_agent(routes, agent):
    value, clients = agent
    old_client = value.client
    routes.config['providers']['endpoint-b']['enabled'] = False
    routes.write()
    result = routes.switch()
    assert not result.success
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert value.client is old_client and len(clients) == 1


def test_named_provider_credentials_remain_profile_scoped(routes, monkeypatch):
    from agent import secret_scope

    routes.config['providers']['endpoint-b'].pop('api_key')
    routes.config['providers']['endpoint-b']['key_env'] = 'TARGET_ROUTE_KEY'
    routes.write()
    monkeypatch.setenv('TARGET_ROUTE_KEY', 'synthetic-ambient-key')
    prior = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({'TARGET_ROUTE_KEY': 'synthetic-profile-key'})
    try:
        result = routes.switch()
        assert result.success, result.error_message
        assert result.api_key == 'synthetic-profile-key'
        assert result.base_url == 'http://b.invalid/v1'
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(prior)


def test_explicit_cli_missing_target_has_no_generic_fallback(routes):
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    shell = SimpleNamespace(requested_provider='missing-target', _explicit_api_key=None, _explicit_base_url=None,
                            model='chosen-model', api_key='synthetic-key-a', base_url='http://a.invalid/v1',
                            provider='endpoint-a', api_mode='chat_completions', acp_command=None, acp_args=[],
                            agent=None, _normalize_model_for_provider=lambda _: False, _fallback_model=[])
    assert not CLIAgentSetupMixin._ensure_runtime_credentials(shell)
    assert (shell.provider, shell.base_url, shell.api_key) == ('endpoint-a', 'http://a.invalid/v1', 'synthetic-key-a')
