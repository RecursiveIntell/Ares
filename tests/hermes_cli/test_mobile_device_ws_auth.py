from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import web_server
from hermes_cli.dashboard_auth.base import TokenPrincipal


class _WebSocket:
    def __init__(self, authorization: str = "", *, query_params: dict | None = None) -> None:
        self.headers = {"authorization": authorization}
        self.query_params = query_params or {}
        self.client = SimpleNamespace(host="127.0.0.1")


class _Provider:
    def __init__(self, name: str, result: TokenPrincipal | None) -> None:
        self.name = name
        self._result = result
        self.seen: list[str] = []

    def verify_token(self, *, token: str) -> TokenPrincipal | None:
        self.seen.append(token)
        return self._result


def test_mobile_device_bearer_stamps_server_owned_ws_identity(monkeypatch) -> None:
    mobile = _Provider(
        "mobile-device",
        TokenPrincipal(principal="device-1", provider="mobile-device", scopes=("mobile:device",)),
    )
    monkeypatch.setattr("hermes_cli.dashboard_auth.list_token_providers", lambda: [mobile])
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)

    ws = _WebSocket("Bearer device-token")
    reason, credential = web_server._ws_auth_reason(ws)

    assert reason is None
    assert credential == "mobile-device"
    assert ws._hermes_auth_identity == {
        "user_id": "device-1",
        "provider": "mobile-device",
        "device_id": "device-1",
        "scopes": ["mobile:device"],
    }
    assert mobile.seen == ["device-token"]


def test_mobile_bearer_does_not_accept_a_foreign_token_provider(monkeypatch) -> None:
    foreign = _Provider(
        "other-token-provider",
        TokenPrincipal(principal="foreign", provider="other-token-provider"),
    )
    monkeypatch.setattr("hermes_cli.dashboard_auth.list_token_providers", lambda: [foreign])
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)

    reason, credential = web_server._ws_auth_reason(_WebSocket("Bearer foreign-token"))

    assert reason == "no_credential"
    assert credential == "none"
    assert foreign.seen == []


def test_mobile_device_token_is_not_accepted_from_query_string(monkeypatch) -> None:
    mobile = _Provider(
        "mobile-device",
        TokenPrincipal(principal="device-1", provider="mobile-device", scopes=("mobile:device",)),
    )
    monkeypatch.setattr("hermes_cli.dashboard_auth.list_token_providers", lambda: [mobile])
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)

    reason, credential = web_server._ws_auth_reason(
        _WebSocket(query_params={"token": "device-token"})
    )

    assert reason == "token_mismatch"
    assert credential == "token"
    assert mobile.seen == []
