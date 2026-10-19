"""Origin-bound browser-bootstrap endpoint contract; inert ASGI callers."""
import pytest
from fastapi.testclient import TestClient
from hermes_cli import web_server as ws
from hermes_cli.dashboard_auth.browser_launch import BrowserLaunchTickets


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "trusted_public_hosts", frozenset(), raising=False)
    monkeypatch.setattr(ws, "_BROWSER_LAUNCH_TICKETS", BrowserLaunchTickets())
    with TestClient(ws.app, base_url="http://127.0.0.1:9119") as value:
        yield value


def test_exact_origin_grant_redemption_sets_no_cookie_and_rejects_replay(client):
    ticket = ws._BROWSER_LAUNCH_TICKETS.issue()
    headers = {"Origin": "http://127.0.0.1:9119"}
    response = client.post("/auth/browser-bootstrap", json={"ticket": ticket}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"protocol": "browser-launch-v1", "token": ws._SESSION_TOKEN}
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/auth/browser-bootstrap", json={"ticket": ticket}, headers=headers).status_code == 403


@pytest.mark.parametrize("origin", [None, "null", "http://127.0.0.1:9220", "https://127.0.0.1:9119", "http://evil.test"])
def test_foreign_origin_does_not_consume_grant(client, origin):
    ticket = ws._BROWSER_LAUNCH_TICKETS.issue()
    headers = {} if origin is None else {"Origin": origin}
    response = client.post("/auth/browser-bootstrap", json={"ticket": ticket}, headers=headers)
    assert response.status_code == 403
    assert ws._SESSION_TOKEN not in response.text
    assert ws._BROWSER_LAUNCH_TICKETS.consume(ticket)


def test_cookie_only_cannot_recover_token(client):
    response = client.post("/auth/browser-bootstrap", json={}, headers={
        "Origin": "http://127.0.0.1:9119", "Cookie": "hermes_dashboard_bootstrap=" + ws._SESSION_TOKEN})
    assert response.status_code == 403
    assert ws._SESSION_TOKEN not in response.text


def test_reload_validation_checks_current_bearer_without_returning_it(client, monkeypatch):
    headers = {"Origin": "http://127.0.0.1:9119", "X-Hermes-Session-Token": ws._SESSION_TOKEN}
    response = client.post("/auth/browser-bootstrap", json={}, headers=headers)
    assert response.status_code == 204
    assert response.content == b""
    monkeypatch.setattr(ws, "_SESSION_TOKEN", "synthetic-rotated-token")
    assert client.post("/auth/browser-bootstrap", json={}, headers=headers).status_code == 403


@pytest.mark.parametrize("body", [{"ticket": "x" * 5000}, {"ticket": None}, {"ticket": "x", "extra": True}, []])
def test_malformed_grants_fail_closed(client, body):
    response = client.post("/auth/browser-bootstrap", json=body, headers={"Origin": "http://127.0.0.1:9119"})
    assert response.status_code in (400, 403, 413)
    assert ws._SESSION_TOKEN not in response.text
