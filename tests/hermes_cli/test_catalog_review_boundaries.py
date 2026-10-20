"""PR120 identity and credential catalog boundaries, using inert inventory I/O."""
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("requested", ["openrouter", "openai-api", "anthropic"])
def test_disabled_openai_declaration_does_not_disable_distinct_builtin(requested):
    from hermes_cli.web_server import _validate_model_assignment_provider
    _validate_model_assignment_provider({"providers": {"openai": {"enabled": False}}}, requested, "")


@pytest.mark.parametrize("requested", ["openai", "custom:openai"])
def test_disabled_exact_declaration_still_rejects(requested):
    from fastapi import HTTPException
    from hermes_cli.web_server import _validate_model_assignment_provider
    with pytest.raises(HTTPException) as caught:
        _validate_model_assignment_provider({"providers": {"openai": {"enabled": False}}}, requested, "")
    assert caught.value.status_code == 400


@pytest.mark.parametrize("alias,canonical", [("claude", "anthropic"), ("google", "gemini"), ("fw", "fireworks")])
def test_disabled_independent_alias_endpoint_does_not_disable_canonical_builtin(alias, canonical):
    from hermes_cli.web_server import _validate_model_assignment_provider
    cfg = {"providers": {alias: {"name": "Independent fixture", "base_url": "http://fixture.invalid/v1", "enabled": False}}}
    _validate_model_assignment_provider(cfg, canonical)


@pytest.mark.parametrize("alias,canonical", [("claude", "anthropic"), ("google", "gemini"), ("fw", "fireworks")])
def test_disabled_canonical_declaration_still_owns_native_aliases(alias, canonical):
    from fastapi import HTTPException
    from hermes_cli.web_server import _validate_model_assignment_provider
    with pytest.raises(HTTPException) as caught:
        _validate_model_assignment_provider({"providers": {canonical: {"enabled": False}}}, alias)
    assert caught.value.status_code == 400


@pytest.mark.parametrize("multiplex", [False, True])
@pytest.mark.parametrize("target_key", [None, "inert-target-anthropic"])
def test_actual_rpc_inventory_uses_only_target_credentials_and_restores_scope(tmp_path, monkeypatch, multiplex, target_key):
    import agent.secret_scope as secrets
    import agent.models_dev as mdev
    import hermes_cli.auth as auth
    import hermes_cli.inventory as inventory
    import hermes_cli.models as models
    import hermes_cli.model_switch as switch
    import hermes_cli.profiles as profiles
    import hermes_cli.providers as providers
    import tui_gateway.server as srv
    from hermes_constants import get_hermes_home

    launch, target = tmp_path / "launch", tmp_path / "target"
    for home in [launch, target]:
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "auto", "default": "fixture"}}))
    (launch / ".env").write_text("ANTHROPIC_API_KEY=inert-launch-anthropic\n")
    if target_key:
        (target / ".env").write_text(f"ANTHROPIC_API_KEY={target_key}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inert-launch-anthropic")
    monkeypatch.setattr(secrets, "_MULTIPLEX_ACTIVE", multiplex)
    monkeypatch.setattr(srv, "_hermes_home", launch)
    monkeypatch.setattr(srv, "_sessions", {})
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: target if name == "target" else launch)
    monkeypatch.setattr(mdev, "PROVIDER_TO_MODELS_DEV", {"anthropic": "anthropic"})
    monkeypatch.setattr(mdev, "fetch_models_dev", lambda: {"anthropic": {"env": ["ANTHROPIC_API_KEY"]}})
    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", {"anthropic": auth.PROVIDER_REGISTRY["anthropic"]})
    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(auth, "read_credential_pool", lambda *a, **k: [])
    monkeypatch.setattr(auth, "is_runtime_provider_routable", lambda slug: True)
    monkeypatch.setattr(providers, "HERMES_OVERLAYS", {})
    monkeypatch.setattr(models, "CANONICAL_PROVIDERS", [])
    monkeypatch.setattr(models, "_PROVIDER_MODELS", {"anthropic": ["fixture-model"]})
    monkeypatch.setattr(models, "get_curated_nous_model_ids", lambda: [])
    monkeypatch.setattr(models, "fetch_ollama_cloud_models", lambda: [])
    monkeypatch.setattr(switch, "_credential_pool_is_usable", lambda *a, **k: False)
    fetched = []
    def inert_models(slug):
        fetched.append((slug, Path(get_hermes_home()), secrets.get_secret("ANTHROPIC_API_KEY")))
        return ["fixture-model"]
    monkeypatch.setattr(models, "cached_provider_model_ids", inert_models)
    seen = []
    def actual_inventory(ctx, **kwargs):
        rows = switch.list_authenticated_providers(probe_custom_providers=False)
        seen.append((secrets.current_secret_scope(), auth.is_provider_explicitly_configured("anthropic")))
        return {"providers": rows}
    monkeypatch.setattr(inventory, "build_model_options_payload", actual_inventory)
    outer = secrets.set_secret_scope({"ANTHROPIC_API_KEY": "inert-outer"})
    old_scope = secrets.current_secret_scope()
    try:
        reply = srv.handle_request({"id": "catalog", "method": "model.options", "params": {"profile": "target", "refresh": True}})
        assert "error" not in reply, reply
        slugs = {r["slug"] for r in reply["result"]["providers"]}
        assert ("anthropic" in slugs) == bool(target_key)
        assert seen[0][0] is not old_scope
        assert seen[0][1] == bool(target_key)
        assert fetched == ([('anthropic', target, target_key)] if target_key else [])
        assert secrets.current_secret_scope() is old_scope
        assert Path(get_hermes_home()) == launch
    finally:
        secrets.reset_secret_scope(outer)


def test_prefetch_real_worker_carries_both_profile_home_and_secret_scope(tmp_path, monkeypatch):
    import agent.secret_scope as secrets
    import hermes_cli.models as models
    import hermes_cli.model_switch as switch
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setattr(secrets, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setattr(switch, "_PARALLEL_PREFETCH_WORKERS", 1)
    monkeypatch.setattr(models, "_load_provider_models_cache", lambda: {})
    monkeypatch.setattr(models, "_credential_fingerprint", lambda slug: "inert-fingerprint")
    monkeypatch.setattr(models, "update_provider_cache_entry", lambda *a: None)
    fetched = []
    def inert_fetch(slug, **kwargs):
        fetched.append((slug, Path(get_hermes_home()), secrets.get_secret("ANTHROPIC_API_KEY")))
        return ["fixture-model"]
    monkeypatch.setattr(models, "cached_provider_model_ids", inert_fetch)
    home_token = set_hermes_home_override(str(tmp_path))
    secret_token = secrets.set_secret_scope({"ANTHROPIC_API_KEY": "inert-prefetch-target"})
    try:
        switch._prefetch_provider_models_parallel(["anthropic", "gemini", "fireworks", "deepseek"])
        assert fetched == [(slug, tmp_path, "inert-prefetch-target") for slug in ["anthropic", "gemini", "fireworks", "deepseek"]]
    finally:
        secrets.reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def test_catalog_authority_is_opt_in_and_scope_miss_preserves_legacy_env_injection(tmp_path, monkeypatch):
    import agent.secret_scope as secrets
    monkeypatch.setattr(secrets, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inert-environment")
    for allow_fallback, expected in [(True, "inert-environment"), (False, None)]:
        scope = secrets.build_profile_secret_scope(tmp_path, allow_environment_fallback=allow_fallback)
        token = secrets.set_secret_scope(scope)
        try:
            assert secrets.get_secret("ANTHROPIC_API_KEY") == expected
            assert secrets.get_secret("PATH") is not None
        finally:
            secrets.reset_secret_scope(token)


def test_rpc_exception_restores_previous_secret_and_home_context(tmp_path, monkeypatch):
    import agent.secret_scope as secrets
    import hermes_cli.inventory as inventory
    import hermes_cli.profiles as profiles
    import tui_gateway.server as srv
    from hermes_constants import get_hermes_home
    launch, target = tmp_path / "launch", tmp_path / "target"
    launch.mkdir(); target.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(srv, "_hermes_home", launch)
    monkeypatch.setattr(srv, "_sessions", {})
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: target)
    def fail(ctx, **kwargs):
        assert Path(get_hermes_home()) == target
        assert secrets.current_secret_scope().profile_home == target
        assert not secrets.current_secret_scope().allow_environment_fallback
        raise RuntimeError("inert inventory failure")
    monkeypatch.setattr(inventory, "build_model_options_payload", fail)
    token = secrets.set_secret_scope({"ANTHROPIC_API_KEY": "inert-outer"})
    previous = secrets.current_secret_scope()
    try:
        reply = srv.handle_request({"id": "catalog", "method": "model.options", "params": {"profile": "target"}})
        assert reply["error"]["code"] == 5033
        assert "inert inventory failure" in reply["error"]["message"]
        assert secrets.current_secret_scope() is previous
        assert Path(get_hermes_home()) == launch
    finally:
        secrets.reset_secret_scope(token)


@pytest.mark.parametrize("auth_source", ["saved_record", "credential_pool"])
def test_canonical_sdk_prefetch_skips_saved_auth_and_pools(tmp_path, monkeypatch, auth_source):
    from types import SimpleNamespace
    import agent.secret_scope as secrets
    import agent.models_dev as mdev
    import hermes_cli.auth as auth
    import hermes_cli.models as models
    import hermes_cli.model_switch as switch
    import hermes_cli.providers as providers
    slugs = ["anthropic", "gemini", "fireworks", "deepseek"]
    monkeypatch.setattr(mdev, "PROVIDER_TO_MODELS_DEV", {})
    monkeypatch.setattr(providers, "HERMES_OVERLAYS", {})
    monkeypatch.setattr(models, "CANONICAL_PROVIDERS", [SimpleNamespace(slug=slug) for slug in [*slugs, "bedrock"]])
    store = {"providers": {slug: {} for slug in slugs}}
    if auth_source == "saved_record":
        store["providers"]["bedrock"] = {"fixture": True}
    monkeypatch.setattr(auth, "_load_auth_store", lambda: store)
    monkeypatch.setattr(switch, "_credential_pool_is_usable", lambda slug, **kwargs: slug == "bedrock" and auth_source == "credential_pool")
    monkeypatch.setattr(switch, "_PARALLEL_PREFETCH_WORKERS", 1)
    monkeypatch.setattr(models, "_load_provider_models_cache", lambda: {})
    monkeypatch.setattr(models, "_credential_fingerprint", lambda slug: "inert-fingerprint")
    monkeypatch.setattr(models, "update_provider_cache_entry", lambda *args: None)
    fetched = []
    def inert_fetch(slug, **kwargs):
        assert slug != "bedrock", "SDK discovery must never enter catalog prefetch"
        fetched.append(slug)
        return ["fixture-model"]
    monkeypatch.setattr(models, "cached_provider_model_ids", inert_fetch)
    token = secrets.set_secret_scope(secrets.build_profile_secret_scope(tmp_path, allow_environment_fallback=False))
    try:
        selected = switch._collect_authed_provider_slugs({}, {}, [])
        assert selected == slugs
        switch._prefetch_provider_models_parallel(selected)
        assert fetched == slugs
    finally:
        secrets.reset_secret_scope(token)


@pytest.mark.parametrize("multiplex", [False, True])
@pytest.mark.parametrize("params", [{}, {"profile": "default"}])
def test_bare_and_explicit_launch_catalog_bind_launch_scope(tmp_path, monkeypatch, multiplex, params):
    import agent.secret_scope as secrets
    import hermes_cli.inventory as inventory
    import hermes_cli.profiles as profiles
    import tui_gateway.server as srv
    tmp_path.joinpath('.env').write_text('ANTHROPIC_API_KEY=inert-launch\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secrets, '_MULTIPLEX_ACTIVE', multiplex)
    monkeypatch.setattr(srv, '_hermes_home', tmp_path)
    monkeypatch.setattr(srv, '_sessions', {})
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: tmp_path)
    seen = []
    def inventory_launch(ctx, **kwargs):
        seen.append(secrets.get_secret('ANTHROPIC_API_KEY'))
        return {'providers': []}
    monkeypatch.setattr(inventory, 'build_model_options_payload', inventory_launch)
    previous = secrets.current_secret_scope()
    reply = srv.handle_request({'id': 'catalog', 'method': 'model.options', 'params': params})
    assert 'error' not in reply, reply
    assert seen == ['inert-launch']
    assert secrets.current_secret_scope() is previous


def test_model_catalog_key_and_fingerprint_use_the_bound_secret_scope(monkeypatch):
    import agent.secret_scope as secrets
    import hermes_cli.models as models
    monkeypatch.setattr(secrets, '_MULTIPLEX_ACTIVE', True)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'inert-launch-a')
    token = secrets.set_secret_scope({'OPENROUTER_API_KEY': 'inert-target'})
    try:
        assert models._resolve_openrouter_api_key() == 'inert-target'
        before = models._credential_fingerprint('openrouter')
        monkeypatch.setenv('OPENROUTER_API_KEY', 'inert-launch-b')
        assert models._credential_fingerprint('openrouter') == before
    finally:
        secrets.reset_secret_scope(token)


def test_swr_real_thread_preserves_target_home_and_scope(tmp_path, monkeypatch):
    import threading
    import agent.secret_scope as secrets
    import hermes_cli.models as models
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setattr(secrets, '_MULTIPLEX_ACTIVE', True)
    done = threading.Event()
    seen = []
    def inert_refresh():
        try:
            seen.append((Path(get_hermes_home()), secrets.get_secret('OPENROUTER_API_KEY')))
        finally:
            done.set()
        return None
    home_token = set_hermes_home_override(str(tmp_path))
    secret_token = secrets.set_secret_scope({'OPENROUTER_API_KEY': 'inert-target'})
    try:
        models._spawn_swr_refresh('pr120-inert-unique', inert_refresh)
        assert done.wait(3), 'bounded inert refresh must settle'
        assert seen == [(tmp_path, 'inert-target')]
    finally:
        secrets.reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def test_actual_openai_catalog_transport_receives_target_key_and_endpoint(monkeypatch):
    import agent.secret_scope as secrets
    import hermes_cli.models as models
    monkeypatch.setattr(secrets, '_MULTIPLEX_ACTIVE', True)
    monkeypatch.setenv('OPENAI_API_KEY', 'inert-launch')
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://launch.invalid/v1')
    monkeypatch.setattr(models, '_get_model_config_dict', lambda: {})
    seen = []
    def inert_transport(api_key, base_url, **kwargs):
        seen.append((api_key, base_url))
        return ['inert-target-model']
    monkeypatch.setattr(models, 'fetch_api_models', inert_transport)
    token = secrets.set_secret_scope({'OPENAI_API_KEY': 'inert-target', 'OPENAI_BASE_URL': 'http://target.invalid/v1'})
    try:
        assert models.provider_model_ids('openai-api') == ['inert-target-model']
        assert seen == [('inert-target', 'http://target.invalid/v1')]
    finally:
        secrets.reset_secret_scope(token)


@pytest.mark.parametrize('target_credential', [None, 'inert-target-aws'])
def test_strict_cross_profile_aws_catalog_never_uses_ambient_sdk_chain(tmp_path, monkeypatch, target_credential):
    import agent.secret_scope as secrets
    import agent.bedrock_adapter as bedrock
    import agent.models_dev as mdev
    import hermes_cli.auth as auth
    import hermes_cli.models as models
    import hermes_cli.model_switch as switch
    import hermes_cli.providers as providers
    monkeypatch.setattr(secrets, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'inert-launch-aws')
    monkeypatch.setattr(auth, 'PROVIDER_REGISTRY', {'bedrock': auth.PROVIDER_REGISTRY['bedrock']})
    monkeypatch.setattr(auth, '_load_auth_store', lambda: {})
    monkeypatch.setattr(switch, '_credential_pool_is_usable', lambda *a, **k: False)
    monkeypatch.setattr(mdev, 'PROVIDER_TO_MODELS_DEV', {})
    monkeypatch.setattr(mdev, 'fetch_models_dev', lambda: {})
    monkeypatch.setattr(providers, 'HERMES_OVERLAYS', {'bedrock': providers.HERMES_OVERLAYS['bedrock']})
    monkeypatch.setattr(models, 'CANONICAL_PROVIDERS', [])
    monkeypatch.setattr(models, 'get_curated_nous_model_ids', lambda: [])
    monkeypatch.setattr(models, 'fetch_ollama_cloud_models', lambda: [])
    def denied(*a, **k):
        pytest.fail('ambient SDK/provider discovery must not run for a strict catalog')
    monkeypatch.setattr(bedrock, 'has_aws_credentials', denied)
    monkeypatch.setattr(models, 'cached_provider_model_ids', denied)
    if target_credential:
        tmp_path.joinpath('.env').write_text(f'AWS_BEARER_TOKEN_BEDROCK={target_credential}\n')
    token = secrets.set_secret_scope(secrets.build_profile_secret_scope(tmp_path, allow_environment_fallback=False))
    try:
        rows = switch.list_authenticated_providers(current_provider='bedrock', probe_custom_providers=False)
        assert any(row['slug'] == 'bedrock' for row in rows) == bool(target_credential)
        if target_credential:
            row = next(row for row in rows if row['slug'] == 'bedrock')
            assert row['models'] == models._PROVIDER_MODELS['bedrock']
    finally:
        secrets.reset_secret_scope(token)
