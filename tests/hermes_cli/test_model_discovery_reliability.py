"""Route identity and bounded catalog recovery, without provider calls."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from hermes_cli import inventory, models

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Provider auto-detection can consult the OS/CLI credential owner even in
    # a temp HOME. These route fixtures admit only their explicit synthetic
    # env/config inputs, never an unrelated real credential pool or store.
    monkeypatch.setattr('hermes_cli.model_switch._credential_pool_is_usable', lambda *a, **k: False)
    monkeypatch.setattr('hermes_cli.auth._load_auth_store', lambda: {})
    monkeypatch.setattr('hermes_cli.model_catalog._fetch_manifest', lambda *a, **k: None)
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda *a, **k: {})
    monkeypatch.setattr(models, '_urlopen_model_catalog_request', lambda *a, **k: (_ for _ in ()).throw(OSError('offline fixture')))


IDS = [
    'anthropic/claude-opus-5.5', 'anthropic/claude-sonnet-5.5',
    'anthropic/claude-haiku-5.5', 'anthropic/claude-fable-5.1',
]


@pytest.mark.parametrize('aux', [False, True])
def test_collisions_preserve_independent_routes_and_opaque_ids(aux):
    ctx = inventory.ConfigContext('openrouter', IDS[0], '', {}, [])
    rows = [dict(slug=slug, name=slug, models=IDS + ['Case/ID', 'case/id'],
                 total_models=len(IDS) + 2, is_user_defined=custom,
                 api_url=url, api_mode=mode, source='user-config' if custom else 'built-in')
            for slug, custom, url, mode in [
                ('tokenrouter', True, 'https://proxy.invalid/v1', 'responses'),
                ('openrouter', False, 'https://router.invalid/v1', 'chat_completions')]]
    with patch('hermes_cli.model_switch.list_authenticated_providers', return_value=deepcopy(rows)), \
         patch.object(inventory, 'load_picker_context', return_value=ctx), \
         patch.object(inventory, '_moa_provider_row', return_value=None):
        out = inventory.build_aux_picker_rows() if aux else inventory.build_models_payload(ctx)['providers']
    assert [(r['slug'], r['models'], r['api_url'], r['api_mode']) for r in out] == [
        (r['slug'], r['models'], r['api_url'], r['api_mode']) for r in rows]


def test_mixed_ids_do_not_erase_vendor_shortlists():
    prefixed = [f'anthropic/model-{n}' for n in range(8)] + [f'openai/model-{n}' for n in range(8)]
    rows = [dict(slug='openrouter', models=prefixed + ['bare-opaque'])]
    with patch('agent.models_dev.get_model_info', return_value=None):
        inventory._apply_featured(rows)
    featured = rows[0]['featured_models']
    assert any(m.startswith('anthropic/') for m in featured)
    assert any(m.startswith('openai/') for m in featured)
    assert 'bare-opaque' in featured
    assert set(featured) <= set(rows[0]['models'])
    reversed_rows = [dict(slug='openrouter', models=list(reversed(rows[0]['models'])))]
    with patch('agent.models_dev.get_model_info', return_value=None):
        inventory._apply_featured(reversed_rows)
    assert set(reversed_rows[0]['featured_models']) == set(featured)


def test_explicit_refresh_does_not_delete_last_known_good(monkeypatch):
    from hermes_cli.model_switch import list_authenticated_providers
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-test-key')
    with patch.object(models, 'clear_provider_models_cache') as clear, \
         patch.object(models, 'cached_provider_model_ids', return_value=['model-live-only']) as cached, \
         patch('agent.models_dev.fetch_models_dev', return_value={}):
        rows = list_authenticated_providers(current_provider='deepseek', refresh=True)
    clear.assert_not_called()
    assert any(r['slug'] == 'deepseek' for r in rows)
    assert any(c.args == ('deepseek',) and c.kwargs.get('force_refresh') is True for c in cached.call_args_list)


def test_custom_cache_disk_restart_offline_identity_and_delisting(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    url = 'https://models.invalid/v1'
    with patch.object(models, 'fetch_api_models', return_value=IDS):
        assert models.cached_fetch_api_models('identity-a', url, force_refresh=True) == IDS
    with patch.object(models, 'fetch_api_models', return_value=None) as live:
        recovered = models.cached_fetch_api_models('identity-a', url, force_refresh=True)
        assert recovered == IDS
        assert recovered.catalog_status['degraded'] is True
        assert recovered.catalog_status['source'] == 'cache'
        assert recovered.catalog_status['freshness'] == 'stale'
        assert models.cached_fetch_api_models('identity-b', url, force_refresh=True) is None
        assert models.cached_fetch_api_models('identity-a', url, api_mode='responses', cache_only=True) is None
    with patch.object(models, 'fetch_api_models', return_value=[]):
        assert models.cached_fetch_api_models('identity-a', url, force_refresh=True) == []
    with patch.object(models, 'fetch_api_models', side_effect=AssertionError('no network')):
        assert models.cached_fetch_api_models('identity-a', url, cache_only=True) == []


def test_cached_endpoint_paths_are_case_sensitive(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with patch.object(models, 'fetch_api_models', return_value=['private-route']):
        models.cached_fetch_api_models('key', 'https://models.invalid/Route', force_refresh=True)
    with patch.object(models, 'fetch_api_models', side_effect=AssertionError('no network')):
        assert models.cached_fetch_api_models('key', 'https://models.invalid/route', cache_only=True) is None


def test_normal_options_open_never_enables_custom_probes():
    ctx = inventory.ConfigContext('custom', IDS[0], 'https://models.invalid/v1', {}, [])
    with patch.object(inventory, 'build_models_payload', return_value={}) as build:
        inventory.build_model_options_payload(ctx)
    assert build.call_args.kwargs['probe_custom_providers'] is False
    assert build.call_args.kwargs['probe_current_custom_provider'] is False


def test_real_inventory_custom_cached_status_and_delisting(tmp_path, monkeypatch):
    from hermes_cli import providers
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda: {})
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {})
    monkeypatch.setattr(models, 'get_curated_nous_model_ids', lambda: [])
    url = 'https://models.invalid/v1'
    ctx = inventory.ConfigContext('custom', IDS[3], url, {}, [])
    with patch.object(models, 'fetch_api_models', return_value=IDS):
        models.cached_fetch_api_models('', url, force_refresh=True)
    with patch.object(models, 'fetch_api_models', side_effect=AssertionError('picker must stay offline')), \
         patch.object(inventory, '_moa_provider_row', return_value=None):
        payload = inventory.build_models_payload(ctx, probe_custom_providers=False)
    row = next(r for r in payload['providers'] if r['slug'] == 'custom')
    assert row['models'] == IDS
    assert row['catalog_status']['source'] == 'cache'
    # A successful empty response supersedes the saved list; the current
    # selection remains in the payload, but is explicitly unavailable.
    with patch.object(models, 'fetch_api_models', return_value=[]):
        models.cached_fetch_api_models('', url, force_refresh=True)
    with patch.object(models, 'fetch_api_models', side_effect=AssertionError('picker must stay offline')), \
         patch.object(inventory, '_moa_provider_row', return_value=None):
        payload = inventory.build_models_payload(ctx, probe_custom_providers=False)
    row = next(r for r in payload['providers'] if r['slug'] == 'custom')
    assert row['models'] == []
    assert payload['model'] == IDS[3]
    assert payload['provider'] == 'custom'
    assert row['unavailable_models'] == [IDS[3]]


@pytest.mark.parametrize('payload', [{}, {'data': None}, {'data': [{}]}, {'data': [{'id': 42}]}])
def test_malformed_catalog_does_not_delist_last_known_good(payload, tmp_path, monkeypatch):
    import io
    import json
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    url = 'https://models.invalid/v1'
    with patch.object(models, 'fetch_api_models', return_value=IDS):
        models.cached_fetch_api_models('key', url, force_refresh=True)
    with patch.object(models, '_urlopen_model_catalog_request', side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())), \
         patch.object(models, '_custom_provider_ssl_context', return_value=None):
        out = models.cached_fetch_api_models('key', url, force_refresh=True)
    assert out == IDS
    assert out.catalog_status['degraded'] is True


def test_offline_fallback_uses_single_owner_without_aliasing():
    with patch('hermes_cli.model_catalog.get_curated_openrouter_models', return_value=None), \
         patch.object(models, '_openrouter_catalog_cache', None), \
         patch.object(models, '_urlopen_model_catalog_request', side_effect=OSError('offline')):
        out = models.fetch_openrouter_models(force_refresh=True)
    assert out == models.OPENROUTER_MODELS
    ids = [mid for mid, _ in out]
    assert len(ids) == len(set(ids))
    assert all('/' in mid and mid for mid in ids)


def test_background_refresh_cannot_label_old_ids_with_rotated_credentials(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    identity = ['a']
    monkeypatch.setattr(models, '_credential_fingerprint', lambda p: identity[0])
    models._save_provider_models_cache({'deepseek': {'fp': 'a', 'at': time.time() - 7200, 'models': ['cached-a']}})

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    def rotating_discovery(*args, **kwargs):
        identity[0] = 'b'
        return ['old-credential-model']

    with patch.object(models.threading, 'Thread', ImmediateThread), \
         patch.object(models, 'provider_model_ids', side_effect=rotating_discovery):
        assert models.cached_provider_model_ids('deepseek') == ['cached-a']
    with patch.object(models, 'provider_model_ids', return_value=[]):
        assert models.cached_provider_model_ids('deepseek') == []


def test_real_inventory_keeps_declared_case_sensitive_ids_and_endpoint_routes(monkeypatch):
    from hermes_cli import providers
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda: {})
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {})
    monkeypatch.setattr(models, 'get_curated_nous_model_ids', lambda: [])
    definitions = {name: {'name': name, 'base_url': f'https://models.invalid/{path}',
                        'models': ['Case/ID', 'case/id'], 'discover_models': False}
                   for name, path in [('route-upper', 'Route'), ('route-lower', 'route')]}
    ctx = inventory.ConfigContext('', '', '', definitions, [])
    with patch.object(models, 'fetch_api_models', side_effect=AssertionError('no network')), \
         patch.object(inventory, '_moa_provider_row', return_value=None):
        out = inventory.build_models_payload(ctx, probe_custom_providers=False)['providers']
    assert [(row['slug'], row['api_url'], row['models']) for row in out] == [
        ('route-upper', 'https://models.invalid/Route', ['Case/ID', 'case/id']),
        ('route-lower', 'https://models.invalid/route', ['Case/ID', 'case/id'])]


def test_current_legacy_endpoint_is_not_duplicated_after_case_safe_matching(monkeypatch):
    from hermes_cli import providers
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {})
    monkeypatch.setattr(models, 'get_curated_nous_model_ids', lambda: [])
    url = 'https://models.invalid/Route'
    ctx = inventory.ConfigContext('custom', 'Case/ID', url, {}, [
        {'name': 'Saved Route', 'base_url': url, 'model': 'Case/ID', 'discover_models': False}])
    with patch.object(inventory, '_moa_provider_row', return_value=None):
        rows = inventory.build_models_payload(ctx, probe_custom_providers=False)['providers']
    assert len(rows) == 1
    assert rows[0]['api_url'] == url
    assert rows[0]['models'] == ['Case/ID']
