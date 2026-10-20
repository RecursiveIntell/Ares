"""Real profile config, runtime selector and switch pipeline; no provider calls."""
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import model_switch as ms, runtime_provider as rp
from hermes_cli.config import load_config, get_compatible_custom_providers


@pytest.fixture
def routes(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-openai-key')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-groq-key')
    monkeypatch.setenv('GLM_API_KEY', 'synthetic-zai-key')
    monkeypatch.setenv('OPENCODE_ZEN_API_KEY', 'synthetic-zen-key')
    monkeypatch.setattr(ms, 'resolve_alias', lambda *a, **k: None)
    monkeypatch.setattr(ms, 'list_provider_models', lambda *a, **k: [])
    monkeypatch.setattr(ms, 'get_model_info', lambda *a, **k: None)
    monkeypatch.setattr(ms, 'get_model_capabilities', lambda *a, **k: None)
    monkeypatch.setattr('hermes_cli.models.detect_provider_for_model', lambda *a, **k: None)
    monkeypatch.setattr('hermes_cli.models.validate_requested_model', lambda *a, **k: {'accepted': True, 'persist': True, 'recognized': True})
    monkeypatch.setattr(ms, 'normalize_model_for_provider', lambda model, provider: model)
    config = {'model': {'provider': 'endpoint-a', 'default': 'model-a'},
              'providers': {
                  'endpoint-a': {'name': 'Endpoint A', 'base_url': 'http://a.invalid/v1', 'api_key': 'synthetic-key-a', 'default_model': 'model-a'},
                  'endpoint-b': {'name': 'Endpoint B', 'base_url': 'http://b.invalid/v1', 'api_key': 'synthetic-key-b', 'default_model': 'model-b'},
                  'dual-wire': {'name': 'Dual Wire', 'base_url': 'https://opencode.ai/zen/v1', 'api_key': 'synthetic-wire-key', 'default_model': 'glm-5'}},
              'fallback_providers': []}

    def write():
        (home / 'config.yaml').write_text(yaml.safe_dump(config))
        return load_config()

    def switch(provider='endpoint-b', current='endpoint-a', **extra):
        cfg = load_config()
        return ms.switch_model(raw_input='chosen-model', current_provider=current, current_model='prior-model',
                               current_base_url='http://a.invalid/v1', current_api_key='synthetic-key-a',
                               explicit_provider=provider, user_providers=cfg.get('providers'),
                               custom_providers=get_compatible_custom_providers(cfg), **extra)

    write()
    return SimpleNamespace(config=config, write=write, switch=switch, home=home)


@pytest.mark.parametrize('alias', ['endpoint-b', 'custom:endpoint-b', 'Endpoint B'])
def test_named_alias_resolves_its_own_endpoint_and_key(routes, alias):
    result = routes.switch(provider=alias)
    assert result.success, result.error_message
    assert (result.base_url, result.api_key) == ('http://b.invalid/v1', 'synthetic-key-b')


def test_explicit_disabled_custom_provider_is_not_success(routes):
    routes.config['providers']['endpoint-b']['enabled'] = False
    routes.write()
    result = routes.switch()
    assert not result.success
    assert 'disabled' in result.error_message.lower()


@pytest.mark.parametrize('state', ['missing', 'disabled'])
def test_same_provider_resolution_error_does_not_keep_stale_runtime(routes, state):
    if state == 'missing':
        routes.config['providers'].pop('endpoint-a')
    else:
        routes.config['providers']['endpoint-a']['enabled'] = False
    routes.write()
    result = routes.switch(provider='', current='endpoint-a')
    assert not result.success, (result.base_url, result.api_key)


def test_missing_explicit_provider_cannot_use_generic_default(routes):
    result = routes.switch(provider='missing-target')
    assert not result.success
    assert 'missing-target' in result.error_message


def test_warm_named_a_b_a_uses_target_owned_runtime(routes):
    current_provider, current_url, current_key = 'endpoint-a', 'http://a.invalid/v1', 'synthetic-key-a'
    for provider, url, key in [('endpoint-b', 'http://b.invalid/v1', 'synthetic-key-b'), ('endpoint-a', 'http://a.invalid/v1', 'synthetic-key-a')]:
        cfg = load_config()
        result = ms.switch_model('chosen-model', current_provider, 'prior-model', current_url, current_key,
                                 explicit_provider=provider, user_providers=cfg['providers'])
        assert result.success, result.error_message
        assert (result.base_url, result.api_key) == (url, key)
        current_provider, current_url, current_key = result.target_provider, result.base_url, result.api_key


def test_cli_refresh_routes_selected_model_instead_of_profile_default(routes):
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    shell = SimpleNamespace(requested_provider='opencode-zen', _explicit_api_key=None, _explicit_base_url=None,
                            model='claude-sonnet-4-6', api_key='', base_url='', provider='opencode-zen',
                            api_mode='chat_completions', acp_command=None, acp_args=[], agent=None,
                            _normalize_model_for_provider=lambda _: False, _fallback_model=[])
    assert CLIAgentSetupMixin._ensure_runtime_credentials(shell)
    assert shell.api_mode == 'anthropic_messages'
    assert shell.base_url == 'https://opencode.ai/zen'


def test_explicit_cli_named_override_ignores_profile_default_runtime(routes):
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    shell = SimpleNamespace(requested_provider='endpoint-b', _explicit_api_key=None, _explicit_base_url=None,
                            model='chosen-model', api_key='synthetic-key-a', base_url='http://a.invalid/v1',
                            provider='endpoint-a', api_mode='chat_completions', acp_command=None, acp_args=[],
                            agent=None, _normalize_model_for_provider=lambda _: False, _fallback_model=[])
    assert CLIAgentSetupMixin._ensure_runtime_credentials(shell)
    assert (shell.base_url, shell.api_key, shell.model) == ('http://b.invalid/v1', 'synthetic-key-b', 'chosen-model')


@pytest.mark.parametrize('alias', ['endpoint-b', 'custom:endpoint-b', 'Endpoint B'])
def test_disabled_named_alias_rejected_by_real_runtime_selector(routes, alias):
    routes.config['providers']['endpoint-b']['enabled'] = False
    routes.write()
    with pytest.raises((rp.AuthError, ValueError)):
        rp.resolve_runtime_provider(requested=alias, target_model='chosen-model')
