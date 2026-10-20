"""Actual headless-browser NET-01/04 gate. Requires provisioned Playwright
and a normal-sandbox Chromium executable mounted by the controller. Missing
prerequisites are SKIP/unqualified, never a security acceptance receipt.
"""
import asyncio
import contextlib
import hashlib
import shutil
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from hermes_cli import web_server as ws
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit
from tests.hermes_cli.test_dashboard_auth_password_login import PasswordProvider
from tests.hermes_cli.p2_native_fixture import native_headers


@contextlib.contextmanager
def serve(application):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(application, lifespan="off", proxy_headers=True, forwarded_allow_ips="",
        log_level="error", access_log=False))
    worker = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not server.started and worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "inert browser listener failed"
    try:
        yield origin
    finally:
        server.should_exit = True
        worker.join(5)
        listener.close()
        assert not worker.is_alive()


def test_real_headless_second_origin_and_cookie_form(monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api", reason="controller must provision browser driver")
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("controller must mount a normal-sandbox Chromium executable")
    effects = []
    clear_providers()
    _reset_password_rate_limit()
    register_provider(PasswordProvider())
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(ws.app.state, "trusted_public_hosts", frozenset(), raising=False)
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_KEY", "synthetic-browser-fixture-proof-with-32-bytes")
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_ID", "synthetic-browser-fixture-identity")
    monkeypatch.setenv("HERMES_SERVE_HEADLESS", "1")
    ws.mount_spa(ws.app)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("harmless browser sentinel")
    monkeypatch.setattr(ws, "_managed_file_response", lambda *a, **kw:
        effects.append("download") or FileResponse(sentinel))
    monkeypatch.setattr(ws, "_spawn_hermes_action", lambda args, name, **kw:
        effects.append(name) or SimpleNamespace(pid=12345))
    monkeypatch.setattr(ws, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr("hermes_cli.update_contract.evaluate_update_admission", lambda root: None)
    monkeypatch.setattr(ws, "_ACTION_PROCS", {})
    hostile = FastAPI()
    @hostile.get("/")
    def inert():
        return HTMLResponse("<html><body>inert second origin</body></html>")
    try:
        with serve(ws.app) as backend, serve(hostile) as sibling, playwright.sync_playwright() as runtime:
            # No --no-sandbox escape hatch; browser startup failures fail this gate.
            browser = runtime.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=True)
            try:
                page = browser.new_page()
                page.goto(sibling)
                result = page.evaluate("""async base => {
                  try { const r = await fetch(base+'/'); return await r.text(); }
                  catch { return 'blocked'; }
                }""", backend)
                assert ws._SESSION_TOKEN not in result
                native = httpx.get(backend + "/auth/desktop-bootstrap", headers=native_headers(
                    ws._DESKTOP_BOOTSTRAP_KEY, ws._DESKTOP_BOOTSTRAP_ID, backend + "/auth/desktop-bootstrap"), timeout=5)
                assert native.status_code == 200
                assert native.json()["identity"] == ws._DESKTOP_BOOTSTRAP_ID
                assert "token" not in native.json()
                assert native.json()["session_digest"] == hashlib.sha256(ws._SESSION_TOKEN.encode()).hexdigest()
                # This fixture deliberately supplies the parent-owned bearer
                # to the hostile browser; it is not discovered from bootstrap.
                stolen = ws._SESSION_TOKEN
                socket_url = backend.replace("http:", "ws:") + "/api/events?channel=p2-synthetic&token=" + stolen
                assert page.evaluate("""url => new Promise(r => {
                  const s = new WebSocket(url); const t=setTimeout(()=>{s.close();r('deadline')},3000);
                  s.onopen=()=>{clearTimeout(t);s.close();r('opened')};
                  s.onerror=()=>{clearTimeout(t);r('denied')}; s.onclose=()=>{clearTimeout(t);r('denied')};
                })""", socket_url) == "denied"
                download = backend + "/api/files/download?path=sentinel&token=" + stolen
                with page.expect_navigation():
                    page.evaluate("url => { location.href=url }", download)
                assert effects == []
                assert "Cross-origin download denied" in page.content()
                page.goto(backend)
                for _ in range(2):
                    assert page.evaluate("""url => new Promise(r => {
                      const s=new WebSocket(url); const t=setTimeout(()=>{s.close();r(false)},3000);
                      s.onopen=()=>{clearTimeout(t);s.close();r(true)};s.onerror=()=>{clearTimeout(t);r(false)};
                    })""", socket_url)
                assert page.evaluate("async url => (await fetch(url)).text()", download) == "harmless browser sentinel"
                assert effects == ["download"]
                effects.clear()
                monkeypatch.setattr(ws.app.state, "auth_required", True)
                assert page.evaluate("""async base => (await fetch(base+'/auth/password-login', {
                  method:'POST', headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({provider:'testpw',username:'admin',password:'hunter2'})})).status""", backend) == 200
                for path in ["/api/gateway/stop", "/api/hermes/update"]:
                    page.goto(sibling)
                    with page.expect_navigation():
                        page.evaluate("""url => { const f=document.createElement('form');
                          f.method='POST';f.action=url;document.body.append(f);f.submit(); }""", backend + path)
                    assert "Origin denied" in page.content()
                    assert effects == []
                page.goto(backend + "/login")
                assert page.evaluate("async base => (await fetch(base+'/api/gateway/stop',{method:'POST'})).status", backend) == 200
                assert effects == ["gateway-stop"]
                provider = PasswordProvider()
                access = provider.complete_password_login(username="admin", password="hunter2").access_token
                response = httpx.post(backend + "/api/hermes/update",
                    headers={"Authorization": "Bearer " + access}, timeout=5)
                assert response.status_code == 200
                assert effects == ["gateway-stop", "hermes-update"]
                _reset_password_rate_limit()
                for number in range(15):
                    guess = httpx.post(backend + "/auth/password-login",
                        headers={"X-Forwarded-For": f"192.0.2.{number}"},
                        json={"provider": "testpw", "username": "admin", "password": "wrong"}, timeout=5)
                assert guess.status_code == 429
            finally:
                browser.close()
    finally:
        clear_providers()
        _reset_password_rate_limit()
