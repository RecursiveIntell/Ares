"""Tests for tui_gateway inline-RPC pool routing under GIL pressure (#50005).

The WS read loop in ``handle_ws()`` processes requests sequentially via
``await asyncio.to_thread(server.dispatch, req, transport)``. Inline handlers
(NOT in ``_LONG_HANDLERS``) run ``handle_request()`` synchronously inside
``dispatch()``, blocking the loop from reading the next request. Under GIL
pressure from multiple concurrent agent turns, even lightweight RPCs like
``session.list`` and ``pet.info`` can take seconds, causing frontend requests
to time out (120s) and the WebSocket to disconnect — the false "needs setup"
failure mode (#50005).

The fix routes all frontend-polled RPCs through ``_LONG_HANDLERS`` so
``dispatch()`` returns immediately (``_pool.submit`` + ``return None``) and
the WS read loop is never blocked.
"""

import io
import json
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only — keeping them active for
    # the whole test would poison modules first imported inside test bodies
    # (see tests/tui_gateway/test_protocol.py for the full rationale).
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")

    # Tests below stub handlers ("session.list", "prompt.submit", ...) in
    # the module-level _methods dict shared with every other test file in
    # the process — snapshot and restore it around each test.
    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()


@pytest.fixture(autouse=True)
def _restore_method_registry(server):
    """Tests may substitute RPC handlers, but must not leak them cross-module."""
    original = dict(server._methods)
    yield
    server._methods.clear()
    server._methods.update(original)


@pytest.fixture()
def capture(server):
    """Redirect server's real stdout to a StringIO and return (server, buf)."""
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf


# ─── RPCs that must be in _LONG_HANDLERS ────────────────────────────────

# These are polled by the Desktop frontend. Before the fix they ran inline,
# blocking the WS read loop under GIL pressure and causing false "needs setup"
# (#50005). Each one does I/O (DB query, file read, network) that can take
# seconds when the GIL is contended by concurrent agent turns.

FRONTEND_POLLED_RPCS = [
    "session.active_list",   # live-session rehydrate — in-memory registry
    "session.list",          # loads session list — SQLite query
    "pet.info",              # petdex poll — file/network read
    "process.list",          # background process status — process registry scan
    "setup.runtime_check",   # runtime readiness — resolve_runtime_provider() I/O
    "setup.status",          # provider configured check — config/credential scan
]

# Transcript reads materialize durable history and can be much larger than the
# sidebar page. They must not block a later Stop request on the same WebSocket.
CONTROL_LATENCY_RPCS = ["session.history"]


@pytest.mark.parametrize("method", FRONTEND_POLLED_RPCS)
def test_frontend_polled_rpc_is_pool_routed(server, method):
    """Every frontend-polled RPC must be in _LONG_HANDLERS so dispatch()
    returns immediately and the WS read loop is not blocked (#50005)."""
    assert method in server._LONG_HANDLERS, (
        f"{method!r} is not in _LONG_HANDLERS — it will block the WS read "
        f"loop under GIL pressure, causing false 'needs setup' (#50005)."
    )


@pytest.mark.parametrize("method", CONTROL_LATENCY_RPCS)
def test_history_read_is_pool_routed_so_controls_remain_admissible(server, method):
    assert method in server._LONG_HANDLERS, (
        f"{method!r} is not in _LONG_HANDLERS — durable history reads can block "
        "the WS reader before session.interrupt is admitted."
    )


def test_slow_history_does_not_block_same_socket_interrupt_admission(server):
    """A slow inline-turned-long handler must not prevent a concurrent fast
    handler from completing. This is the core invariant: dispatch() must
    return immediately for _LONG_HANDLERS so the WS read loop stays free.

    Simulates the GIL-pressure scenario from #50005: a slow handler (mimicking
    a session.list query under GIL contention) must not block a fast handler
    (mimicking setup.runtime_check).
    """
    released = threading.Event()

    def slow_session_history(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"messages": [], "count": 0})

    server._methods["session.history"] = slow_session_history
    server._methods["session.interrupt"] = lambda rid, params: server._ok(rid, {"status": "interrupted"})

    try:
        t0 = time.monotonic()
        # The actual WS loop awaits dispatch; the pooled history call must
        # return immediately so it can read the following interrupt frame.
        assert server.dispatch({"id": "history", "method": "session.history", "params": {}}) is None

        interrupt_resp = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {}})
        fast_elapsed = time.monotonic() - t0

        assert interrupt_resp["result"] == {"status": "interrupted"}
        assert fast_elapsed < 2.0, (
            f"session.interrupt admission blocked for {fast_elapsed:.2f}s behind "
            "a slow session.history read on the same WS reader."
        )
    finally:
        released.set()


def test_websocket_reader_reaches_stop_after_a_slow_history_request(monkeypatch, server):
    """Exercise the real WS receive loop, not only direct dispatch()."""
    import asyncio

    from tui_gateway import ws as ws_mod

    ws_server = ws_mod.server
    released = threading.Event()

    def slow_history(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"messages": [], "count": 0})

    monkeypatch.setitem(ws_server._methods, "session.history", slow_history)
    monkeypatch.setitem(
        ws_server._methods,
        "session.interrupt",
        lambda rid, params: ws_server._ok(rid, {"status": "interrupted"}),
    )
    monkeypatch.setattr(ws_server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(ws_server, "resolve_skin", lambda: "default")
    monkeypatch.setattr(ws_server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(ws_server, "register_live_transport", lambda *_a, **_k: None)
    monkeypatch.setattr(ws_server, "unregister_live_transport", lambda *_a, **_k: None)
    monkeypatch.setattr(ws_server, "_release_wake_for_transport", lambda *_a, **_k: None)
    monkeypatch.setattr(ws_server, "_close_sessions_for_transport", lambda *_a, **_k: (0, 0))

    class FakeWS:
        def __init__(self):
            self.reads = 0
            self.sent = []
            self.interrupt_read = asyncio.Event()

        async def accept(self, **kwargs):
            return None

        async def send_text(self, line):
            self.sent.append(json.loads(line))

        async def receive_text(self):
            self.reads += 1
            if self.reads == 1:
                return json.dumps({"id": "history", "method": "session.history", "params": {}})
            if self.reads == 2:
                self.interrupt_read.set()
                return json.dumps({"id": "stop", "method": "session.interrupt", "params": {}})
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            return None

    async def run():
        fake = FakeWS()
        task = asyncio.create_task(ws_mod.handle_ws(fake))
        try:
            await asyncio.wait_for(fake.interrupt_read.wait(), timeout=2.5)
        finally:
            released.set()
        await task
        return fake.sent

    sent = asyncio.run(run())
    assert any(
        frame.get("id") == "stop" and frame.get("result", {}).get("status") == "interrupted"
        for frame in sent
    )


def test_rpc_pool_workers_supports_concurrent_long_handlers(server):
    """The RPC thread pool must have enough workers to handle concurrent
    long handlers without queueing. With 6+ frontend-polled RPCs added to
    _LONG_HANDLERS, the default 4 workers can be exhausted when multiple
    agent turns are running. The pool must be at least 8."""
    assert server._rpc_pool_workers >= 8, (
        f"_rpc_pool_workers is {server._rpc_pool_workers}, expected >= 8. "
        f"Frontend-polled RPCs added to _LONG_HANDLERS need more workers to "
        f"avoid queueing under multi-agent load (#50005)."
    )
