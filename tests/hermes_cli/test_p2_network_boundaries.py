"""NET-01..04 real ASGI callers, synthetic auth, inert mutation/download sinks.

No lifespan, installed operations, IDP or external network. Browser/native
qualification is separate and must not be inferred from these assertions.
"""
import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from hermes_cli import web_server as ws
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import routes, native_flow
from tests.hermes_cli.test_dashboard_auth_password_login import PasswordProvider
from tests.hermes_cli.p2_native_fixture import native_headers


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    clear_providers()
    provider = PasswordProvider()
    register_provider(provider)
    routes._reset_password_rate_limit()
    native_flow._reset_for_tests()
    for name, value in {"bound_host": "127.0.0.1", "auth_required": True,
                        "trusted_public_hosts": frozenset()}.items():
        monkeypatch.setattr(ws.app.state, name, value, raising=False)
    effects = []
    monkeypatch.setattr(ws, "_spawn_hermes_action", lambda args, name, **kw:
                        effects.append(name) or SimpleNamespace(pid=12345))
    monkeypatch.setattr(ws, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr("hermes_cli.update_contract.evaluate_update_admission", lambda root: None)
    monkeypatch.setattr(ws, "_ACTION_PROCS", {})
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("harmless P2 sentinel")
    monkeypatch.setattr(ws, "_managed_file_response", lambda *a, **kw:
                        effects.append("download") or FileResponse(sentinel))
    client = TestClient(ws.app, base_url="http://127.0.0.1:9119")
    yield client, effects, provider
    client.close()
    clear_providers()
    routes._reset_password_rate_limit()
    native_flow._reset_for_tests()


def login(client):
    response = client.post("/auth/password-login", json={
        "provider": "testpw", "username": "admin", "password": "hunter2"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/gateway/stop", "/api/hermes/update"])
@pytest.mark.parametrize("origin", [None, "null", "http://localhost:9119",
    "http://127.0.0.1:9220", "https://127.0.0.1:9119", "http://evil.test",
    "http://127.0.0.1:9119/", "http://127.0.0.1:9119, http://evil.test"])
def test_cookie_mutation_denied_before_adapter(boundary, path, origin):
    client, effects, _ = boundary
    login(client)
    headers = {} if origin is None else {"Origin": origin}
    response = client.post(path, headers=headers, data={"inert": "form"})
    assert response.status_code == 403
    assert effects == []


@pytest.mark.parametrize("path,name", [("/api/gateway/stop", "gateway-stop"),
    ("/api/hermes/update", "hermes-update")])
@pytest.mark.parametrize("auth", ["cookie", "bearer"])
def test_real_mutation_positive(boundary, path, name, auth):
    client, effects, provider = boundary
    login(client)
    headers = {"Origin": "http://127.0.0.1:9119"} if auth == "cookie" else {
        "Authorization": "Bearer " + provider.complete_password_login(
            username="admin", password="hunter2").access_token}
    assert client.post(path, headers=headers).status_code == 200
    assert effects == [name]


def test_real_cors_options_before_auth(boundary):
    client, effects, _ = boundary
    for origin, status in [("http://127.0.0.1:9119", 200),
                           ("http://127.0.0.1:9220", 400), ("null", 400)]:
        response = client.options("/api/gateway/stop", headers={"Origin": origin,
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization"})
        assert response.status_code == status
        assert (response.headers.get("access-control-allow-origin") == origin) == (status == 200)
    assert effects == []


def test_native_child_identity_bootstrap_and_headless_root(boundary, monkeypatch):
    client, _, _ = boundary
    monkeypatch.setattr(ws.app.state, "auth_required", False)
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_KEY", "synthetic-child-only-proof-with-32-bytes")
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_ID", "synthetic-child-identity")
    native = native_headers(ws._DESKTOP_BOOTSTRAP_KEY, ws._DESKTOP_BOOTSTRAP_ID,
                            "http://127.0.0.1:9119/auth/desktop-bootstrap")
    assert client.get("/auth/desktop-bootstrap").status_code == 403
    assert client.get("/auth/desktop-bootstrap", headers={**native, "Origin": "null"}).status_code == 403
    assert client.get("/auth/desktop-bootstrap", headers={**native, "X-Hermes-Native-Identity": "foreign"}).status_code == 403
    response = client.get("/auth/desktop-bootstrap", headers=native)
    assert response.status_code == 200
    assert response.json()["identity"] == "synthetic-child-identity"
    assert response.json()["session_digest"] == hashlib.sha256(ws._SESSION_TOKEN.encode()).hexdigest()
    assert "token" not in response.json()
    monkeypatch.setenv("HERMES_SERVE_HEADLESS", "1")
    headless = FastAPI()
    ws.mount_spa(headless)
    root = TestClient(headless).get("/")
    assert root.status_code == 404
    assert ws._SESSION_TOKEN not in root.text


def test_browser_launch_and_reconnect_requires_authenticated_bootstrap(boundary, monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    (tmp_path / "index.html").write_text("<html><head></head><body>inert app</body></html>")
    monkeypatch.setattr(ws, "WEB_DIST", tmp_path)
    browser = FastAPI()
    browser.add_api_route("/auth/browser-bootstrap", ws.browser_bootstrap, methods=["POST"])
    ws.mount_spa(browser)
    client = TestClient(browser, base_url="http://127.0.0.1:9119")
    assert client.get("/").status_code == 200
    assert ws._SESSION_TOKEN not in client.get("/").text
    response = client.post("/auth/browser-bootstrap", json={"ticket": ws._BROWSER_LAUNCH_TICKETS.issue()},
        headers={"Origin": "http://127.0.0.1:9119"})
    assert response.status_code == 200
    assert response.json()["token"] == ws._SESSION_TOKEN
    assert "set-cookie" not in response.headers
    assert ws._SESSION_TOKEN not in client.get("/").text
    assert client.post("/auth/browser-bootstrap", json={}, headers={
        "Origin": "http://127.0.0.1:9119", "X-Hermes-Session-Token": ws._SESSION_TOKEN}).status_code == 204


def test_stolen_token_origin_cannot_download_or_upgrade(boundary, monkeypatch):
    client, effects, _ = boundary
    monkeypatch.setattr(ws.app.state, "auth_required", False)
    url = f"/api/files/download?path=sentinel&token={ws._SESSION_TOKEN}"
    assert client.get(url, headers={"Origin": "http://127.0.0.1:9220"}).status_code == 403
    assert effects == []
    assert client.get(url, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert effects == []
    assert client.get(url, headers={"Origin": "http://127.0.0.1:9119"}).status_code == 200
    assert effects == ["download"]
    for origin in ["null", "http://127.0.0.1:9220", "http://localhost:9119"]:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(f"ws://127.0.0.1:9119/api/events?channel=p2-synthetic&token={ws._SESSION_TOKEN}", headers={"Origin": origin}):
                pass
        assert rejected.value.code == 4403
    for _ in range(2):
        with client.websocket_connect(f"ws://127.0.0.1:9119/api/events?channel=p2-synthetic&token={ws._SESSION_TOKEN}",
                                      headers={"Origin": "http://127.0.0.1:9119"}):
            pass


def test_rotating_xff_same_peer_still_throttled(boundary):
    client, _, _ = boundary
    for number in range(15):
        response = client.post("/auth/password-login", headers={"X-Forwarded-For": f"192.0.2.{number}"},
            json={"provider": "testpw", "username": "admin", "password": "wrong"})
    assert response.status_code == 429
    assert len(routes._pw_attempts) == 1


@pytest.mark.parametrize("peer,xff,trusted,expected", [
    ("192.0.2.1", "203.0.113.1", "127.0.0.1", "192.0.2.1"),
    ("127.0.0.1", "203.0.113.1", "", "127.0.0.1"),
    ("127.0.0.1", "198.51.100.1, 203.0.113.1, 10.0.0.2", "127.0.0.1,10.0.0.0/8", "203.0.113.1"),
    ("::1", "2001:db8::42", "::1", "2001:db8::42"),
    ("127.0.0.1", "garbage", "127.0.0.1", "_unknown_"),
])
def test_uvicorn_real_proxy_resolver(boundary, peer, xff, trusted, expected):
    async def run():
        resolved = []
        async def observe(scope, receive, send):
            from starlette.requests import Request
            resolved.append(routes._client_ip(Request(scope)))
        resolver = ProxyHeadersMiddleware(observe, trusted_hosts=trusted)
        await resolver({"type": "http", "scheme": "http", "client": (peer, 123),
                        "headers": [(b"x-forwarded-for", xff.encode())]}, None, None)
        assert resolved == [expected]
    asyncio.run(run())


def test_native_authorize_uses_same_resolved_peer(boundary):
    client, _, _ = boundary
    statuses = [client.get("/auth/native/authorize", params={"provider": "testpw",
        "code_challenge": "x" * 43, "code_challenge_method": "S256",
        "redirect_uri": "http://127.0.0.1:12345/callback", "state": "inert"},
        headers={"X-Forwarded-For": f"192.0.2.{number}"}, follow_redirects=False).status_code
        for number in range(native_flow._MAX_PENDING_PER_IP + 1)]
    assert statuses[-1] == 503
    assert all(status == 302 for status in statuses[:-1])


def test_limiter_capacity_never_evicts_live_budget_and_expires(boundary, monkeypatch):
    monkeypatch.setattr(routes, "_PW_RATE_MAX_PEERS", 2)
    clock = [100.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock[0])
    for _ in range(routes._PW_RATE_MAX_ATTEMPTS):
        assert not routes._password_rate_limited("first")
    assert not routes._password_rate_limited("second")
    assert routes._password_rate_limited("third")
    assert routes._password_rate_limited("first")
    assert len(routes._pw_attempts) == 2
    clock[0] += routes._PW_RATE_WINDOW_SEC + 1
    assert not routes._password_rate_limited("third")
    assert set(routes._pw_attempts) == {"third"}
