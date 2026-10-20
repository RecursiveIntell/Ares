"""Browser launch is an ephemeral single-use grant, never the API bearer."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import web_server as ws


def test_reusable_session_bearer_is_not_browser_launch_grant(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    (tmp_path / "index.html").write_text("<html><head></head><body>inert</body></html>")
    monkeypatch.setattr(ws, "WEB_DIST", tmp_path)
    app = FastAPI()
    ws.mount_spa(app)
    with TestClient(app, base_url="http://127.0.0.1:9119") as client:
        response = client.get("/", params={"bootstrap": ws._SESSION_TOKEN}, follow_redirects=False)
        assert response.status_code == 400
        assert ws._SESSION_TOKEN not in response.text


def test_ticket_consumption_is_atomic_and_expiry_is_exact():
    from hermes_cli.dashboard_auth.browser_launch import BrowserLaunchTickets
    now = [10.0]
    store = BrowserLaunchTickets(clock=lambda: now[0], ttl_seconds=60, capacity=2)
    ticket = store.issue()
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(store.consume, [ticket] * 8)) == 1
    assert not store.consume(ticket)
    expired = store.issue()
    now[0] = 70.0
    assert not store.consume(expired)
    assert not store.consume(ws._SESSION_TOKEN)
    assert not store.consume("x" * 10000)


def test_ticket_capacity_denies_without_evicting_live_grants():
    from hermes_cli.dashboard_auth.browser_launch import BrowserLaunchTickets
    store = BrowserLaunchTickets(capacity=1)
    first = store.issue()
    with pytest.raises(RuntimeError, match="capacity"):
        store.issue()
    assert store.consume(first)


def test_ticket_replay_cannot_bootstrap_another_browser(monkeypatch, tmp_path):
    from hermes_cli.dashboard_auth.browser_launch import BrowserLaunchTickets
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    (tmp_path / "index.html").write_text("<html><head></head><body>inert</body></html>")
    monkeypatch.setattr(ws, "WEB_DIST", tmp_path)
    monkeypatch.setattr(ws, "_BROWSER_LAUNCH_TICKETS", BrowserLaunchTickets())
    app = FastAPI()
    app.add_api_route("/auth/browser-bootstrap", ws.browser_bootstrap, methods=["POST"])
    ws.mount_spa(app)
    ticket = ws._BROWSER_LAUNCH_TICKETS.issue()
    assert ticket != ws._SESSION_TOKEN
    headers = {"Origin": "http://127.0.0.1:9119"}
    with TestClient(app, base_url="http://127.0.0.1:9119") as first:
        reply = first.post("/auth/browser-bootstrap", json={"ticket": ticket}, headers=headers)
        assert reply.status_code == 200
        assert reply.headers["referrer-policy"] == "no-referrer"
        assert reply.headers["cache-control"] == "no-store"
        assert "set-cookie" not in reply.headers
        assert reply.json()["token"] == ws._SESSION_TOKEN
        assert ws._SESSION_TOKEN not in first.get("/").text
    with TestClient(app, base_url="http://127.0.0.1:9119") as second:
        assert second.post("/auth/browser-bootstrap", json={"ticket": ticket}, headers=headers).status_code == 403
