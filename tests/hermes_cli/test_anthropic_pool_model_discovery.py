from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import models


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_anthropic_picker_discovers_models_with_pool_api_key(monkeypatch):
    """A direct API key stored only in auth.json must reach /v1/models."""
    monkeypatch.setattr(models, "_get_model_config_dict", lambda: {"provider": "nous"})
    monkeypatch.setattr(
        "agent.anthropic_adapter.resolve_anthropic_token",
        lambda: None,
    )
    monkeypatch.setattr(
        "hermes_cli.auth.read_credential_pool",
        lambda provider: (
            [
                {
                    "auth_type": "api_key",
                    "access_token": "sk-ant-api03-pool-key",
                    "base_url": "https://api.anthropic.com",
                }
            ]
            if provider == "anthropic"
            else []
        ),
    )

    captured = {}

    def _open(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        return _Response({"data": [{"id": "claude-opus-5"}]})

    monkeypatch.setattr(models, "_urlopen_model_catalog_request", _open)

    result = models.provider_model_ids("anthropic")

    assert "claude-opus-5" in result
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-ant-api03-pool-key"
    assert "authorization" not in captured["headers"]


def test_anthropic_pool_api_key_overrides_conflicting_active_endpoint(monkeypatch):
    """A pool-scoped key must never reach the active model's other endpoint."""
    active_endpoint = "https://active.example/anthropic/v1"
    pool_endpoint = "https://pool.example/anthropic/v1"
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {"provider": "anthropic", "base_url": active_endpoint},
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.resolve_anthropic_token",
        lambda: None,
    )
    monkeypatch.setattr(
        "hermes_cli.auth.read_credential_pool",
        lambda provider: (
            [
                {
                    "auth_type": "api_key",
                    "access_token": "proxy-key",
                    "base_url": pool_endpoint,
                }
            ]
            if provider == "anthropic"
            else []
        ),
    )

    requests = []

    def _open(request, *, timeout):
        requests.append((
            request.full_url,
            {key.lower(): value for key, value in request.header_items()},
        ))
        return _Response({"data": [{"id": "claude-proxy-model"}]})

    monkeypatch.setattr(models, "_urlopen_model_catalog_request", _open)

    assert models.provider_model_ids("anthropic") == ["claude-proxy-model"]
    assert requests == [
        (
            f"{pool_endpoint}/models",
            {
                "anthropic-version": "2023-06-01",
                "x-api-key": "proxy-key",
            },
        )
    ]
    assert all(not url.startswith(active_endpoint) for url, _headers in requests)


def test_anthropic_picker_uses_curated_fallback_when_catalog_is_unavailable(monkeypatch):
    """Discovery failure retains the curated catalog and its ordering."""
    expected = list(models._PROVIDER_MODELS["anthropic"])
    monkeypatch.setattr(models, "_fetch_anthropic_models", lambda **_kwargs: None)

    assert models.provider_model_ids("anthropic") == expected


def test_anthropic_picker_catalog_uses_pool_account_before_claude_code_file(monkeypatch):
    """The model list must use the pool-selected account, not another login."""
    monkeypatch.setattr(models, "_get_model_config_dict", lambda: {"provider": "nous"})
    for key in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: {
            "accessToken": "claude-code-file-token",
            "refreshToken": "fixture-refresh-token",
            "expiresAt": 9_999_999_999_000,
        },
    )
    pool_entry = SimpleNamespace(auth_type="oauth", access_token="sk-ant-oat01-selected-pool-token")
    pool = SimpleNamespace(_available_entries=lambda **_kwargs: ([pool_entry], []))
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: pool)

    captured = {}

    def _open(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        return _Response({"data": [{"id": "claude-fable-5-2"}]})

    monkeypatch.setattr(models, "_urlopen_model_catalog_request", _open)

    result = models.provider_model_ids("anthropic")

    assert "claude-fable-5-2" in result
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["authorization"] == "Bearer sk-ant-oat01-selected-pool-token"
