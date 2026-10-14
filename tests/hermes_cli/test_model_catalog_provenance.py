"""Discovery provenance through real parsing, disk cache and picker inventory."""
import io
import json
import time
from unittest.mock import patch

import pytest
import httpx

from hermes_cli import inventory, models


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    monkeypatch.setattr(models, '_openrouter_catalog_cache', None)
    monkeypatch.setattr(models, '_custom_provider_ssl_context', lambda *a: None)
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline fixture')))
    monkeypatch.setattr('hermes_cli.model_catalog._fetch_manifest', lambda *a, **k: None)
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda *a, **k: {})
    monkeypatch.setattr('hermes_cli.auth._load_auth_store', lambda: {})
    monkeypatch.setattr('hermes_cli.model_switch._credential_pool_is_usable', lambda *a, **k: False)
    monkeypatch.setattr('hermes_cli.auth.resolve_nous_runtime_credentials',
                        lambda: {'api_key': 'fixture-key', 'base_url': 'https://nous.invalid/Route/v1'})
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    monkeypatch.setattr(models, '_get_model_config_dict', lambda: {
        'provider': 'openai-api', 'base_url': 'https://direct.invalid/Route/v1'})
    monkeypatch.setattr(httpx.Client, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline fixture')))


def respond(monkeypatch, payload):
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(httpx.Client, 'get', lambda *a, **k: httpx.Response(200, json=payload))


def catalog_payload(provider, ids=None):
    ids = ids if ids is not None else ([m for m, _ in models.OPENROUTER_MODELS[:2]]
                                     if provider == 'openrouter' else ['Case/ID', 'case/id'])
    return {'data': [{'id': mid, 'supported_parameters': ['tools']} for mid in ids]}


@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'openai-api'])
def test_cold_offline_is_degraded_and_never_cached_as_fresh(provider):
    out = models.cached_provider_model_ids(provider, force_refresh=True)
    assert out.catalog_status['degraded'] is True
    assert out.catalog_status['freshness'] != 'fresh'
    assert out.catalog_status['authoritative'] is False
    assert provider not in models._load_provider_models_cache()


@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'openai-api'])
def test_nonempty_fallback_cannot_replace_last_good(provider, monkeypatch):
    respond(monkeypatch, catalog_payload(provider))
    first = models.cached_provider_model_ids(provider, force_refresh=True)
    before = models._provider_models_cache_path().read_bytes()
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    monkeypatch.setattr(httpx.Client, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    # Simulate restart of the in-process OpenRouter cache, retaining disk only.
    monkeypatch.setattr(models, '_openrouter_catalog_cache', None)
    out = models.cached_provider_model_ids(provider, force_refresh=True)
    assert out == first
    assert out.catalog_status['source'] == 'cache'
    assert out.catalog_status['freshness'] == 'stale'
    assert out.catalog_status['degraded'] is True
    assert models._provider_models_cache_path().read_bytes() == before


@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'openai-api'])
def test_successful_empty_delists_and_remains_empty_after_restart(provider, monkeypatch):
    respond(monkeypatch, catalog_payload(provider))
    assert models.cached_provider_model_ids(provider, force_refresh=True)
    respond(monkeypatch, {'data': []})
    out = models.cached_provider_model_ids(provider, force_refresh=True)
    assert out == []
    assert out.catalog_status['source'] == 'live'
    assert out.catalog_status['authoritative'] is True
    monkeypatch.setattr(models, '_openrouter_catalog_cache', None)
    with patch.object(models, 'provider_model_ids', side_effect=AssertionError('warm disk only')):
        assert models.cached_provider_model_ids(provider) == []


@pytest.mark.parametrize('payload', [{}, {'data': None}, {'data': [{}]},
                                     {'data': [{'id': 42}]}, {'data': [{'id': '  '}]},
                                     {'data': [{'id': 'valid'}, {'bad': 'entry'}]}])
@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'openai-api'])
def test_malformed_refresh_preserves_disk_and_last_good(payload, provider, monkeypatch):
    respond(monkeypatch, catalog_payload(provider))
    first = models.cached_provider_model_ids(provider, force_refresh=True)
    before = models._provider_models_cache_path().read_bytes()
    respond(monkeypatch, payload)
    out = models.cached_provider_model_ids(provider, force_refresh=True)
    assert out == first
    assert out.catalog_status['degraded'] is True
    assert models._provider_models_cache_path().read_bytes() == before


def test_fresh_result_equal_to_snapshot_still_has_witnessed_provenance(monkeypatch):
    respond(monkeypatch, catalog_payload('openrouter', [m for m, _ in models.OPENROUTER_MODELS]))
    out = models.model_ids(force_refresh=True)
    assert out == [m for m, _ in models.OPENROUTER_MODELS]
    assert isinstance(out, list)
    assert out.catalog_status['source'] == 'live'
    assert out.catalog_status['freshness'] == 'fresh'
    assert out.catalog_status['authoritative'] is True


@pytest.mark.parametrize('change', ['credential', 'endpoint', 'api_mode'])
def test_direct_scope_change_never_reuses_old_good(change, monkeypatch):
    cfg = {'provider': 'openai-api', 'base_url': 'https://direct.invalid/Route/v1',
           'api_mode': 'chat_completions'}
    monkeypatch.setattr(models, '_get_model_config_dict', lambda: cfg)
    respond(monkeypatch, catalog_payload('openai-api'))
    assert models.cached_provider_model_ids('openai-api', force_refresh=True) == ['Case/ID', 'case/id']
    if change == 'credential':
        monkeypatch.setenv('OPENAI_API_KEY', 'rotated-fixture')
    elif change == 'endpoint':
        cfg['base_url'] = 'https://direct.invalid/route/v1'
    else:
        cfg['api_mode'] = 'responses'
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    out = models.cached_provider_model_ids('openai-api', force_refresh=True)
    assert 'Case/ID' not in out
    assert out.catalog_status['degraded'] is True


def test_scope_rotated_during_discovery_cannot_write_or_return_old_ids(monkeypatch):
    respond(monkeypatch, catalog_payload('openai-api'))
    models.cached_provider_model_ids('openai-api', force_refresh=True)
    before = models._provider_models_cache_path().read_bytes()

    def rotating_response(*a, **k):
        monkeypatch.setenv('OPENAI_API_KEY', 'rotated-during-request')
        return io.BytesIO(json.dumps(catalog_payload('openai-api', ['old-scope-only'])).encode())

    monkeypatch.setattr(models, '_urlopen_model_catalog_request', rotating_response)
    out = models.cached_provider_model_ids('openai-api', force_refresh=True)
    assert out == []
    assert out.catalog_status['degraded'] is True
    assert models._provider_models_cache_path().read_bytes() == before


def test_unknown_adapter_semantics_cannot_replace_good_or_claim_fresh(monkeypatch):
    provider = 'fixture-unknown'
    fp = models._credential_fingerprint(provider)
    models._save_provider_models_cache({provider: {'fp': fp, 'at': time.time(), 'models': ['good']}})
    before = models._provider_models_cache_path().read_bytes()
    monkeypatch.setattr(models, 'provider_model_ids', lambda *a, **k: ['unwitnessed'])
    out = models.cached_provider_model_ids(provider, force_refresh=True)
    assert out == ['good']
    assert out.catalog_status['degraded'] is True
    assert models._provider_models_cache_path().read_bytes() == before


def test_inventory_authoritative_empty_keeps_selection_unavailable_and_route(monkeypatch):
    from hermes_cli import providers
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {})
    monkeypatch.setattr('agent.models_dev.PROVIDER_TO_MODELS_DEV', {'openai-api': 'openai'})
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda: {'openai': {'env': ['OPENAI_API_KEY']}})
    monkeypatch.setattr(inventory, '_moa_provider_row', lambda *a, **k: None)
    cfg = {'provider': 'openai-api', 'base_url': 'https://direct.invalid/Route/v1'}
    monkeypatch.setattr(models, '_get_model_config_dict', lambda: cfg)
    respond(monkeypatch, {'data': []})
    ctx = inventory.ConfigContext('openai-api', 'Case/ID', cfg['base_url'], {}, [])
    out = inventory.build_models_payload(ctx, refresh=True)
    row = next(r for r in out['providers'] if r['slug'] == 'openai-api')
    assert row['models'] == []
    assert row['unavailable_models'] == ['Case/ID']
    assert row['catalog_status']['authoritative'] is True
    assert out['model'] == 'Case/ID'
    assert out['provider'] == 'openai-api'


def test_background_and_parallel_updates_never_replace_good_with_fallback(monkeypatch):
    fp = models._credential_fingerprint('openrouter')
    models._save_provider_models_cache({'openrouter': {'fp': fp, 'at': time.time() - 7200,
                                                       'models': ['good']}})
    before = models._provider_models_cache_path().read_bytes()

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(models.threading, 'Thread', InlineThread)
    out = models.cached_provider_model_ids('openrouter')
    assert out == ['good']
    models.update_provider_cache_entry('openrouter', models.provider_model_ids('openrouter', force_refresh=True))
    assert models._provider_models_cache_path().read_bytes() == before


def test_inventory_failed_refresh_cannot_resurrect_authoritatively_delisted_selection(monkeypatch):
    from hermes_cli import providers
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {})
    monkeypatch.setattr('agent.models_dev.PROVIDER_TO_MODELS_DEV', {'openai-api': 'openai'})
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda: {'openai': {'env': ['OPENAI_API_KEY']}})
    monkeypatch.setattr(inventory, '_moa_provider_row', lambda *a, **k: None)
    ctx = inventory.ConfigContext('openai-api', 'Case/ID', 'https://direct.invalid/Route/v1', {}, [])
    respond(monkeypatch, {'data': []})
    inventory.build_models_payload(ctx, refresh=True)
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    out = inventory.build_models_payload(ctx, refresh=True)
    row = next(r for r in out['providers'] if r['slug'] == 'openai-api')
    assert row['models'] == []
    assert row['unavailable_models'] == ['Case/ID']
    assert row['catalog_status']['degraded'] is True
    assert out['model'] == 'Case/ID'


def test_pricing_catalog_cache_still_works_beside_id_provenance(monkeypatch):
    monkeypatch.setattr(models, '_pricing_cache', {})
    monkeypatch.setattr(models, '_pricing_cache_retry_after', {})
    respond(monkeypatch, {'data': [{'id': 'Case/ID',
                                  'pricing': {'prompt': '0.000002', 'completion': '0.00001'}}]})
    first = models.fetch_models_with_pricing(base_url='https://pricing.invalid')
    assert first['Case/ID']['prompt'] == '0.000002'
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('warm pricing only')))
    assert models.fetch_models_with_pricing(base_url='https://pricing.invalid') == first


def test_openrouter_inprocess_last_good_cannot_cross_rotated_scope(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'first-fixture-key')
    respond(monkeypatch, catalog_payload('openrouter'))
    first = models.cached_provider_model_ids('openrouter', force_refresh=True)
    assert first.catalog_status['authoritative'] is True
    before = models._provider_models_cache_path().read_bytes()
    monkeypatch.setenv('OPENROUTER_API_KEY', 'second-fixture-key')
    monkeypatch.setattr(models, '_urlopen_model_catalog_request',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    out = models.cached_provider_model_ids('openrouter', force_refresh=True)
    assert out.catalog_status['source'] == 'curated'
    assert out.catalog_status['authoritative'] is False
    assert out.catalog_status['degraded'] is True
    assert models._provider_models_cache_path().read_bytes() == before
