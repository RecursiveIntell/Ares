"""Target-owned persistence roundtrips; no auth resolution or provider calls."""
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from hermes_cli import web_server as ws
from hermes_cli.models import normalize_provider
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail("Provider/network calls are forbidden in this persistence gate")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ws, "build_cron_model_impact", lambda **kwargs: {})
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_cfg = {"model": {"provider": "ollama-launch", "default": "source-model"},
                  "providers": {"ollama-launch": {"base_url": "http://source.invalid/v1"}}}
    (source / "config.yaml").write_text(yaml.safe_dump(source_cfg))
    monkeypatch.setenv("HERMES_HOME", str(source))
    return source, target


def write_cfg(home, cfg):
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def read_cfg(home):
    return yaml.safe_load((home / "config.yaml").read_text())


@pytest.mark.parametrize("provider,definition", [
    ("openrouter", {}), ("anthropic", {}), ("openai-codex", {}),
    ("claude", {"providers": {"anthropic": {"enabled": True}}}),
    ("openai-api", {}), ("ollama-cloud", {}), ("auto", {}),
    ("ollama-launch", {"providers": {"ollama-launch": {"base_url": "http://target.invalid/v1"}}}),
    ("custom:target", {"custom_providers": [{"name": "Target", "base_url": "http://target.invalid/v1"}]}),
    ("custom:target-key", {"providers": {"target-key": {"name": "Target Display", "api": "http://target.invalid/v1"}}}),
])
def test_profile_a_b_a_roundtrip(homes, provider, definition):
    source, target = homes
    write_cfg(target, {"model": {"provider": "openrouter", "default": "original"}, "display": {"fixture_marker": "keep"}, **definition})
    original_source = (source / "config.yaml").read_bytes()
    for selected_provider, model in [(provider, "model-a"), ("openrouter", "model-b"), (provider, "model-a")]:
        ws._write_profile_model(target, selected_provider, model)
        assert read_cfg(target)["model"]["default"] == model
    assert normalize_provider(read_cfg(target)["model"]["provider"]) == normalize_provider(provider)
    assert read_cfg(target)["display"]["fixture_marker"] == "keep"
    assert (source / "config.yaml").read_bytes() == original_source


@pytest.mark.parametrize("provider,definition", [
    ("ollama-launch", {}), ("custom:missing", {}),
    ("claude", {"providers": {"anthropic": {"enabled": False}}}),
    ("custom:openai", {"providers": {"openai": {"api_key_env": "FIXTURE_KEY"}}}),
    ("ollama-launch", {"providers": {"ollama-launch": {"base_url": "http://target.invalid/v1", "enabled": False}}}),
    ("custom:target-key", {"providers": {"target-key": {"base_url": "http://target.invalid/v1", "enabled": "off"}}}),
    ("custom:target", {"custom_providers": [{"name": "Target", "base_url": "http://target.invalid/v1", "enabled": False}]}),
    ("ollama-launch", {"providers": {"ollama-launch": {"name": "Incomplete"}}}),
])
def test_invalid_target_definition_never_borrows_source_or_writes(homes, provider, definition):
    source, target = homes
    path = write_cfg(target, {"model": {"provider": "ollama-launch", "default": "glm-5.3-flash:cloud", "base_url": ""}, **definition})
    before = path.read_bytes()
    with pytest.raises(HTTPException) as err:
        ws._write_profile_model(target, provider, "ollama/model-a")
    assert err.value.status_code == 400
    assert "this profile" in err.value.detail
    assert path.read_bytes() == before
    assert ws.load_config()["model"]["default"] == "source-model"


def test_main_api_rejects_missing_named_definition_without_write(homes):
    _, target = homes
    path = write_cfg(target, {"model": {"provider": "openrouter", "default": "original"}})
    before = path.read_bytes()
    token = set_hermes_home_override(str(target))
    try:
        with pytest.raises(HTTPException):
            ws._apply_model_assignment_sync("main", "ollama-launch", "glm-5.3-flash:cloud", "", "")
    finally:
        reset_hermes_home_override(token)
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["custom", "local", "ollama", "vllm", "llamacpp", "llama.cpp", "llama-cpp"])
def test_generic_endpoint_is_not_repointed_to_first_saved_custom(homes, monkeypatch, provider):
    _, target = homes
    write_cfg(target, {"model": {"provider": "openrouter", "default": "original"}, "custom_providers": [{"name": "Ollama", "base_url": "http://other.invalid/v1"}]})
    # Registration is separate bookkeeping; skip its CLI import in this gate.
    import hermes_cli.main as main
    monkeypatch.setattr(main, "_save_custom_provider", lambda *args, **kwargs: None)
    token = set_hermes_home_override(str(target))
    try:
        result = ws._apply_model_assignment_sync("main", provider, "model-a", "", "http://target.invalid/v1")
    finally:
        reset_hermes_home_override(token)
    assert result["provider"] == provider
    cfg = read_cfg(target)
    assert cfg["model"]["provider"] == provider
    assert cfg["model"]["base_url"] == "http://target.invalid/v1"
    assert cfg["custom_providers"][0]["base_url"] == "http://other.invalid/v1"


@pytest.mark.parametrize("provider", ["custom", "local"])
def test_profile_generic_endpoint_repick_keeps_explicit_host(homes, provider):
    _, target = homes
    write_cfg(target, {"model": {"provider": provider, "default": "original", "base_url": "http://target.invalid/v1"},
                       "custom_providers": [{"name": "Unrelated", "base_url": "http://other.invalid/v1"}]})
    ws._write_profile_model(target, provider, "model-a")
    assert read_cfg(target)["model"] == {"provider": provider, "default": "model-a", "base_url": "http://target.invalid/v1"}


@pytest.mark.parametrize("previous,selected", [("custom", "ollama"), ("ollama", "custom"), ("local", "vllm"), ("vllm", "local")])
def test_profile_generic_alias_repick_keeps_same_explicit_endpoint(homes, previous, selected):
    _, target = homes
    write_cfg(target, {"model": {"provider": previous, "default": "original", "base_url": "http://target.invalid/v1", "api_mode": "chat_completions"}})
    ws._write_profile_model(target, selected, "model-a")
    assert read_cfg(target)["model"] == {"provider": selected, "default": "model-a", "base_url": "http://target.invalid/v1", "api_mode": "chat_completions"}


@pytest.mark.parametrize("surface", ["main", "profile"])
def test_named_generic_alias_uses_target_definition_without_old_endpoint_key(homes, surface):
    _, target = homes
    write_cfg(target, {
        "model": {"provider": "custom", "default": "old", "base_url": "http://old.invalid/v1", "api_key": "fixture-old-endpoint-key"},
        "providers": {"ollama": {"base_url": "http://target.invalid/v1", "models": ["model-a"]}},
    })
    if surface == "profile":
        ws._write_profile_model(target, "ollama", "model-a")
    else:
        token = set_hermes_home_override(str(target))
        try:
            ws._apply_model_assignment_sync("main", "ollama", "model-a", "", "")
        finally:
            reset_hermes_home_override(token)
    cfg = read_cfg(target)["model"]
    assert cfg["base_url"] == "http://target.invalid/v1"
    assert not cfg.get("api_key")


def test_legacy_named_generic_alias_resolves_its_declared_target(homes):
    _, target = homes
    write_cfg(target, {"model": {"provider": "openrouter", "default": "old"},
                       "custom_providers": [{"name": "Ollama", "base_url": "http://target.invalid/v1"}]})
    ws._write_profile_model(target, "ollama", "model-a")
    assert read_cfg(target)["model"]["provider"] == "custom:ollama"


@pytest.mark.parametrize("surface", ["main", "profile"])
def test_same_provider_repick_preserves_deliberate_model_endpoint_override(homes, surface):
    _, target = homes
    write_cfg(target, {"model": {"provider": "openai-api", "default": "old", "base_url": "http://override.invalid/v1", "api_key": "fixture-override-key"},
                       "providers": {"openai-api": {"base_url": "http://definition.invalid/v1"}}})
    if surface == "profile":
        ws._write_profile_model(target, "openai-api", "model-a")
    else:
        token = set_hermes_home_override(str(target))
        try:
            ws._apply_model_assignment_sync("main", "openai-api", "model-a", "", "")
        finally:
            reset_hermes_home_override(token)
    assert read_cfg(target)["model"]["base_url"] == "http://override.invalid/v1"
    assert read_cfg(target)["model"]["api_key"] == "fixture-override-key"


@pytest.mark.parametrize("provider,success", [("ollama-launch", False), ("openrouter", True)])
def test_bots_configure_reports_safe_model_failure_and_keeps_partial_save(homes, monkeypatch, provider, success):
    import hermes_cli.profiles as profiles
    import tui_gateway.server as srv
    import hermes_cli.model_selection_guards as guards
    _, target = homes
    path = write_cfg(target, {"model": {"provider": "openrouter", "default": "original"}})
    before = path.read_bytes()
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: target)
    monkeypatch.setattr(guards, "combined_selection_warning", lambda *args, **kwargs: None)
    result = srv._methods["profiles.configure"]("fixture", {
        "name": "target", "provider": provider, "model": "model-a", "soul": "# Fixture soul"
    })["result"]
    assert result["applied"] == {"model": success, "soul": True}
    assert result["ok"] is success
    assert (target / "SOUL.md").read_text() == "# Fixture soul"
    if success:
        assert "model_error" not in result
        assert read_cfg(target)["model"]["default"] == "model-a"
    else:
        assert result["model_error"]["code"] == "provider_configuration_unavailable"
        assert "previous model is preserved" in result["model_error"]["message"]
        assert path.read_bytes() == before


def test_bots_error_does_not_expose_raw_exception(homes, monkeypatch):
    import hermes_cli.profiles as profiles
    import hermes_cli.web_routers.profiles as router
    import hermes_cli.model_selection_guards as guards
    import tui_gateway.server as srv
    _, target = homes
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: target)
    monkeypatch.setattr(guards, "combined_selection_warning", lambda *args, **kwargs: None)
    def fail(*args):
        raise HTTPException(status_code=400, detail="https://fixture-user:fixture-password@host.invalid/?token=fixture-secret")
    monkeypatch.setattr(router, "_write_profile_model", fail)
    result = srv._methods["profiles.configure"]("fixture", {"name": "target", "provider": "missing", "model": "model-a"})["result"]
    assert result["applied"]["model"] is False
    assert "fixture-" not in str(result)
