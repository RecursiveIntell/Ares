"""Session setting latency must not block the transport reader or reorder writes."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from tui_gateway import server
from tui_gateway.transport import current_transport


class Transport:
    def __init__(self):
        self.frames = []
        self.done = threading.Event()

    def write(self, frame):
        self.frames.append(frame)
        self.done.set()
        return True


@pytest.fixture
def scoped(monkeypatch):
    transport = Transport()
    record = {"session_key": "stored", "transport": transport}
    monkeypatch.setattr(server, "_sessions", {"live": record})
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(server, "_pool", pool)
        yield transport, record


def request(rid):
    return {"id": rid, "method": "config.set", "params": {
        "session_id": "live", "key": "reasoning", "value": "xhigh"}}


def test_slow_setting_leaves_reader_free_and_does_not_overlap_writes(scoped, monkeypatch):
    transport, record = scoped
    entered, release, read_next = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def slow(rid, params):
        calls.append(rid)
        assert current_transport() is transport
        entered.set()
        assert release.wait(3)
        return server._ok(rid, {"value": params["value"]})

    monkeypatch.setitem(server._methods, "config.set", slow)
    monkeypatch.setitem(server._methods, "fixture.read", lambda rid, params: server._ok(rid, {"ok": True}))
    responses = []

    def reader():
        responses.append(server.dispatch(request("first"), transport))
        responses.append(server.dispatch({"id": "read", "method": "fixture.read"}, transport))
        read_next.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(1)
        assert read_next.wait(1), "connection reader blocked behind config.set"
        assert responses[0] is None
        assert responses[1]["result"] == {"ok": True}
        competing = server.dispatch(request("second"), transport)
        assert competing["error"]["code"] == 4009
        assert calls == ["first"]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert transport.done.wait(2)
    assert transport.frames == [server._ok("first", {"value": "xhigh"})]


@pytest.mark.parametrize("replacement", ["record", "transport"])
def test_queued_setting_cannot_mutate_replaced_owner(scoped, monkeypatch, replacement):
    transport, record = scoped
    queued = []

    class HeldPool:
        def submit(self, fn):
            queued.append(fn)

    monkeypatch.setattr(server, "_pool", HeldPool())
    calls = []
    monkeypatch.setitem(server._methods, "config.set", lambda rid, params: calls.append(rid))
    assert server.dispatch(request("held"), transport) is None
    if replacement == "record":
        server._sessions["live"] = {"session_key": "replacement", "transport": transport}
    else:
        record["transport"] = Transport()
    assert len(queued) == 1
    queued[0]()
    assert calls == []
    assert transport.frames[0]["error"]["code"] == 4001


def test_pool_refusal_does_not_leave_setting_reserved(scoped, monkeypatch):
    transport, record = scoped
    pool = server._pool

    class RefusingPool:
        def submit(self, fn):
            raise RuntimeError("pool stopped")

    calls = []

    def handler(rid, params):
        calls.append(rid)
        return server._ok(rid, {"ok": True})

    monkeypatch.setattr(server, "_pool", RefusingPool())
    monkeypatch.setitem(server._methods, "config.set", handler)
    response = server.dispatch(request("refused"), transport)
    assert response["error"]["code"] == 4009
    assert calls == []
    monkeypatch.setattr(server, "_pool", pool)
    assert server.dispatch(request("accepted"), transport) is None
    assert transport.done.wait(2)
    assert calls == ["accepted"]


def test_handler_failure_releases_setting_reservation(scoped, monkeypatch):
    transport, record = scoped

    def fail(rid, params):
        raise RuntimeError("fixture handler failure")

    monkeypatch.setitem(server._methods, "config.set", fail)
    assert server.dispatch(request("failed"), transport) is None
    assert transport.done.wait(2)
    assert transport.frames[0]["error"]["code"] == -32000
    transport.done.clear()
    monkeypatch.setitem(server._methods, "config.set", lambda rid, params: server._ok(rid, {"ok": True}))
    assert server.dispatch(request("retry"), transport) is None
    assert transport.done.wait(2)
    assert transport.frames[-1]["result"] == {"ok": True}


def test_real_reasoning_handler_can_persist_without_holding_reader(scoped, monkeypatch):
    transport, record = scoped
    record["agent"] = SimpleNamespace(model="fixture", provider="fixture", reasoning_config=None)
    entered, release, reader_free = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda session: False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_emit", lambda *args: None)

    def persist(session):
        assert session is record
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(server, "_persist_live_session_runtime", persist)

    def reader():
        assert server.dispatch(request("persist"), transport) is None
        reader_free.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(1)
        assert reader_free.wait(1)
    finally:
        release.set()
        thread.join(3)
    assert transport.done.wait(2)
    assert transport.frames[0]["result"]["value"] == "xhigh"
    assert record["agent"].reasoning_config == {"enabled": True, "effort": "xhigh"}


@pytest.mark.parametrize("queue_age,applied", [(24.0, True), (25.0, False), (60.0, False)])
def test_setting_admission_expires_before_late_worker_mutation(scoped, monkeypatch, queue_age, applied):
    transport, record = scoped
    clock = [100.0]
    queued, calls = [], []

    class HeldPool:
        def submit(self, fn):
            queued.append(fn)

    def handler(rid, params):
        calls.append(rid)
        return server._ok(rid, {"ok": True})

    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(server, "_pool", HeldPool())
    monkeypatch.setitem(server._methods, "config.set", handler)
    assert server.dispatch(request("aged"), transport) is None
    clock[0] += queue_age
    queued.pop(0)()
    if applied:
        assert calls == ["aged"]
        assert transport.frames[-1]["result"] == {"ok": True}
    else:
        assert calls == []
        assert transport.frames[-1]["error"]["code"] == 4009
        assert "not applied" in transport.frames[-1]["error"]["message"]
    # Expiry releases the old reservation; it never poisons a new intent.
    assert server.dispatch(request("fresh"), transport) is None
    queued.pop(0)()
    assert calls[-1] == "fresh"
