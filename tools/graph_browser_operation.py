"""BROWSER card (P4): isolated read-only browser operation via CDP.

Each context is a disposable Chromium process with its own user-data-dir
(cookies/storage isolated). The operation is READ-ONLY: page text extraction
within an allowed-origin list, bounded timeout and artifact size. Mutating
browser classes are explicitly unqualified until a separate F3-style
reconciliation card exists. No live login, no real submission.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import unquote, urlsplit
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional


class BrowserOperationError(RuntimeError):
    """Typed refusal before any browser effect."""


_MAX_TEXT_BYTES = 65536


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_origin(value: str):
    """Return a normalized HTTP(S) origin tuple, or None for invalid input."""
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    return scheme, parsed.hostname.lower(), effective_port


def _url_is_allowed(url: str, allowed_origins: List[str]) -> bool:
    """Match web origins exactly and file URLs only beneath allowed paths."""
    try:
        target = urlsplit(url)
        scheme = target.scheme.lower()
    except (TypeError, ValueError):
        return False

    if scheme in {"http", "https"}:
        target_origin = _http_origin(url)
        return target_origin is not None and any(
            _http_origin(origin) == target_origin for origin in allowed_origins
        )

    if scheme != "file" or target.netloc.lower() not in {"", "localhost"}:
        return False
    try:
        target_path = Path(unquote(target.path)).resolve()
        for origin in allowed_origins:
            try:
                allowed = urlsplit(origin)
                if (allowed.scheme.lower() != "file"
                        or allowed.netloc.lower() not in {"", "localhost"}):
                    continue
                allowed_path = Path(unquote(allowed.path) or "/").resolve()
            except (TypeError, ValueError, OSError, RuntimeError):
                continue
            if target_path.is_relative_to(allowed_path):
                return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False


class GraphBrowserOperation:
    """Fixed read-only browser operations over disposable CDP contexts."""

    def __init__(self, chromium: Optional[str] = None):
        self.chromium = chromium or shutil.which("chromium") or shutil.which("chromium-browser")
        if not self.chromium:
            raise BrowserOperationError("browser_executable_unavailable")
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._context_state: Dict[str, Dict[str, Any]] = {}

    # ── context lifecycle ──────────────────────────────────────────────
    def open_context(self, name: str, *, allowed_origins: List[str]) -> Dict[str, Any]:
        if name in self._contexts:
            raise BrowserOperationError("browser_context_name_taken")
        user_data_dir = tempfile.mkdtemp(prefix=f"graph-browser-{name}-")
        port = _free_port()
        proc = subprocess.Popen(
            [self.chromium,
             f"--remote-debugging-port={port}",
             f"--user-data-dir={user_data_dir}",
             "--headless=new", "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws_url = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                shutil.rmtree(user_data_dir, ignore_errors=True)
                raise BrowserOperationError("browser_process_exited_early")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as resp:
                    ws_url = json.loads(resp.read())["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.15)
        if ws_url is None:
            proc.kill()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise BrowserOperationError("browser_cdp_unavailable")
        ctx = {
            "name": name, "port": port, "ws_url": ws_url,
            "user_data_dir": user_data_dir, "proc": proc,
            "allowed_origins": list(allowed_origins),
        }
        self._contexts[name] = ctx
        self._context_state[name] = {}
        return ctx

    @contextmanager
    def context(self, name: str, *, allowed_origins: List[str]):
        ctx = self.open_context(name, allowed_origins=allowed_origins)
        try:
            yield ctx
        finally:
            self.cancel_context(ctx)

    def cancel_context(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        proc = ctx.get("proc")
        exited = False
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
                exited = True
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                    exited = True
                except subprocess.TimeoutExpired:
                    exited = False
        else:
            exited = True
        shutil.rmtree(ctx.get("user_data_dir", ""), ignore_errors=True)
        self._contexts.pop(ctx.get("name"), None)
        self._context_state.pop(ctx.get("name"), None)
        return {"cleanup_state": "closed" if exited else "kill_pending",
                "process_exited": exited}

    def read_context_state(self, ctx: Dict[str, Any], owner_name: str) -> Any:
        """Context-scoped state is private: another context cannot read it."""
        if ctx.get("name") != owner_name:
            return None
        return self._context_state.get(owner_name)

    # ── CDP helpers ────────────────────────────────────────────────────
    def _cdp_call(self, ws, msg_id: int, method: str, params: Dict[str, Any],
                  timeout: float) -> Dict[str, Any]:
        import websockets.sync.client as wsc
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg
        raise BrowserOperationError("browser_cdp_timeout")

    def _page_ws(self, ctx: Dict[str, Any], url: str, timeout: float) -> str:
        # Modern Chromium requires PUT for /json/new.
        req = urllib.request.Request(
            f"http://127.0.0.1:{ctx['port']}/json/new?{url}", method="PUT"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            target = json.loads(resp.read())
        return target["webSocketDebuggerUrl"]

    # ── fixed operations ───────────────────────────────────────────────
    def read_page(self, ctx: Dict[str, Any], *, url: str, selector: str,
                  parent_agent: Any, timeout_seconds: float = 30) -> Dict[str, Any]:
        if parent_agent is None:
            return {"status": "blocked", "code": "policy_owner_required"}
        if not _url_is_allowed(url, ctx["allowed_origins"]):
            return {"status": "blocked", "code": "browser_origin_not_allowed"}
        if timeout_seconds <= 0:
            return {"status": "blocked", "code": "browser_deadline_expired"}
        import websockets.sync.client as wsc
        try:
            page_ws = self._page_ws(ctx, url, timeout_seconds)
            with wsc.connect(page_ws, max_size=4 * _MAX_TEXT_BYTES) as ws:
                self._cdp_call(ws, 1, "Runtime.enable", {}, timeout_seconds)
                # Wait for load, then extract the selector's text.
                expr = (
                    "(() => { const el = document.querySelector("
                    + json.dumps(selector)
                    + "); return el ? el.textContent : null; })()"
                )
                deadline = time.monotonic() + timeout_seconds
                text = None
                while time.monotonic() < deadline:
                    reply = self._cdp_call(
                        ws, 2, "Runtime.evaluate",
                        {"expression": expr, "returnByValue": True},
                        max(0.5, deadline - time.monotonic()),
                    )
                    value = reply.get("result", {}).get("result", {}).get("value")
                    if value is not None:
                        text = str(value)
                        break
                    time.sleep(0.2)
                if text is None:
                    return {"status": "unknown", "exit_state": "selector_not_observed"}
                text = text[:_MAX_TEXT_BYTES]
                self._context_state[ctx["name"]]["last_read_sha"] = __import__("hashlib").sha256(
                    text.encode()).hexdigest()
                return {"status": "completed", "text": text,
                        "bytes": len(text.encode()), "read_only": True}
        except BrowserOperationError as exc:
            return {"status": "unknown", "exit_state": str(exc)}
        except Exception as exc:
            return {"status": "blocked", "code": "browser_transport_unavailable",
                    "error": type(exc).__name__}

    def submit_form(self, ctx: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """Mutating browser class: unqualified until a separate reconciliation
        card with F3-style preimage/effect proof exists. Never executes."""
        return {"status": "blocked", "code": "browser_mutation_class_unqualified"}
