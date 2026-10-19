"""Real built-browser cross-port isolation, reload and replay gate.

Normal Chromium sandbox, inert namespace-local servers; no live services.
Requires a controller-built production web_dist, never silently skips it.
"""
import hashlib
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from hermes_cli import web_server as ws
from tests.hermes_cli.test_p2_browser_network import serve


def test_redeemed_bootstrap_does_not_send_api_bearer_to_sibling_port(monkeypatch):
    from playwright.sync_api import sync_playwright
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    assert (ws.WEB_DIST / "index.html").is_file(), "Build the current production web frontend before this gate"
    dashboard = FastAPI()
    grants = []
    protected = []
    urls = []
    @dashboard.middleware("http")
    async def record(request: Request, call_next):
        urls.append(str(request.url))
        return await call_next(request)
    @dashboard.post("/auth/browser-bootstrap")
    async def bootstrap(request: Request):
        response = await ws.browser_bootstrap(request)
        grants.append(response.status_code)
        return response
    @dashboard.get("/api/{path:path}")
    async def inert_api(request: Request, path: str):
        protected.append((bool(grants), request.headers.get("x-hermes-session-token") == ws._SESSION_TOKEN))
        return JSONResponse({"detail": "inert unavailable feature"}, status_code=404)
    # Match production route ordering: API owners precede the SPA GET fallback.
    ws.mount_spa(dashboard)
    sibling = FastAPI()
    seen = []
    @sibling.get("/")
    def collect(request: Request):
        seen.append(request.headers.get("cookie", ""))
        response = HTMLResponse("inert sibling")
        response.set_cookie("hermes_dashboard_bootstrap", "synthetic-overwrite", httponly=True, samesite="strict")
        return response
    with serve(dashboard) as root, serve(sibling) as other, sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path="/sandbox/bin/google-chrome", headless=True, chromium_sandbox=True)
        try:
            page = browser.new_page()
            ticket = ws._BROWSER_LAUNCH_TICKETS.issue()
            with page.expect_response(lambda response: "/api/" in response.url):
                page.goto(root + "/sessions#bootstrap=" + ticket)
            page.wait_for_function("typeof window.__HERMES_SESSION_TOKEN__ === 'string'")
            assert page.evaluate("window.__HERMES_SESSION_TOKEN__") == ws._SESSION_TOKEN
            assert page.url == root + "/sessions"
            assert grants == [200]
            page.reload()
            page.wait_for_function("typeof window.__HERMES_SESSION_TOKEN__ === 'string'")
            assert grants == [200, 204]
            page.goto(other)
            assert seen and all(ws._SESSION_TOKEN not in cookie for cookie in seen)
            assert page.evaluate("sessionStorage.getItem('hermes.dashboard.session')") is None
            for cookie in seen + ["hermes_dashboard_bootstrap=" + ws._SESSION_TOKEN]:
                replay = httpx.post(root + "/auth/browser-bootstrap", json={}, headers={"Cookie": cookie, "Origin": root}, timeout=5)
                assert replay.status_code == 403
                shell = httpx.get(root + "/", headers={"Cookie": cookie}, timeout=5)
                assert ws._SESSION_TOKEN not in shell.text
            page.goto(root + "/sessions")
            page.wait_for_function("typeof window.__HERMES_SESSION_TOKEN__ === 'string'")
            assert page.evaluate("window.__HERMES_SESSION_TOKEN__") == ws._SESSION_TOKEN
            assert page.context.cookies() == []
            assert all(ticket not in url and ws._SESSION_TOKEN not in url for url in urls)
            assert all(booted and valid for booted, valid in protected)
            assert protected, "Real application imports must make protected requests after admission"
            bad = httpx.post(root + "/auth/browser-bootstrap", json={"ticket": ticket}, headers={"Origin": root}, timeout=5)
            assert bad.status_code == 403
            monkeypatch.setattr(ws, "_SESSION_TOKEN", "synthetic-next-generation")
            page.reload()
            page.wait_for_function("document.getElementById('root').textContent.includes('authenticated launch')")
            assert page.evaluate("window.__HERMES_SESSION_TOKEN__") is None
            assert page.evaluate("sessionStorage.getItem('hermes.dashboard.session')") is None
            files = sorted(p for p in ws.WEB_DIST.rglob("*") if p.is_file())
            hashes = {str(p.relative_to(ws.WEB_DIST)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
            Path("/evidence/browser-bootstrap-witness.json").write_text(json.dumps({
                "normal_sandbox": True, "production_bundle_hashes": hashes,
                "launch_and_reload": True, "cookie_bearer_leak": False,
                "cookie_replay_denied": True, "sibling_storage_isolated": True,
                "cookie_overwrite_ignored": True, "rotation_denied_without_loop": True,
                "protected_calls_after_admission": len(protected), "grant_statuses": grants,
            }, indent=2), encoding="utf-8")
        finally:
            browser.close()
