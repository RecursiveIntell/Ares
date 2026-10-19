"""Desktop notification-poller coalescing + host-admission routing.

Ported from the retained `dfe1e17dbe` design (Ares-main-current) onto the
compute-host admission structure from `b26a80d2af6a`:

- same-session async-delegation completions drain the queue and ride out as
  ONE consolidated turn (overnight-disconnect backlog regression);
- foreign-owned and non-delegation events stay separate;
- claims are taken before the grouped submission and released on failure;
- a single deliverable keeps the legacy per-event shape;
- in the compute-host child the grouped dispatch rides the same host
  admission as a wire turn.start (complete owner), and a busy refusal
  releases every claim and requeues every event.
"""

import threading
import time
import types

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _reset_ad_records():
    """Ensure the in-memory delegation registry starts and ends empty."""
    from tools import async_delegation as ad

    with ad._records_lock:
        ad._records.clear()
    yield
    with ad._records_lock:
        ad._records.clear()
    # Durable rows die with conftest's per-session tmp HERMES_HOME.


def _make_delegation_events(session_id, prefix, count):
    from tools import async_delegation as ad

    events = []
    for i in range(count):
        delegation_id = f"{prefix}_{i}"
        record = {
            "delegation_id": delegation_id,
            "goal": f"review {i}",
            "status": "running",
            "dispatched_at": time.time(),
        }
        with ad._records_lock:
            ad._records[delegation_id] = record
        ad._persist_dispatch(record)
        events.append({
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "goal": f"review {i}",
            "origin_ui_session_id": session_id,
            "session_key": "session-key",
            "status": "completed",
            "summary": f"summary {i}",
        })
    return events


def test_notification_poller_coalesces_same_session_delegations(monkeypatch):
    """A same-session backlog of delegation completions rides out as ONE turn.

    Regression for the overnight-desktop-disconnect backlog: every queued
    async-delegation completion used to be injected as its own prompt turn,
    producing N "already incorporated, no action needed" bubbles in a row.
    """
    import queue as _queue_mod

    from tools.process_registry import process_registry
    from tools import async_delegation as ad

    turns = []
    emitted = []

    class _Agent:
        def run_conversation(self, prompt, conversation_history=None, stream_callback=None, **_kwargs):
            turns.append(prompt)
            return {
                "final_response": "ok",
                "messages": [{"role": "assistant", "content": "ok"}],
            }

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
        def start(self):
            self._target()

    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)

    sess = _session(agent=_Agent())
    server._sessions["sid_coalesce"] = sess
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: emitted.append(a))
    monkeypatch.setattr(server, "make_stream_renderer", lambda cols: None)
    monkeypatch.setattr(server, "render_message", lambda raw, cols: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)

    events = _make_delegation_events("sid_coalesce", "deleg_coalesce", 3)

    isolated_queue: _queue_mod.Queue = _queue_mod.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", isolated_queue)
    for evt in events:
        isolated_queue.put(evt)

    stop = threading.Event()
    stop.set()
    try:
        server._notification_poller_loop(stop, "sid_coalesce", sess)

        assert len(turns) == 1, f"expected 1 consolidated turn, got {len(turns)}"
        consolidated = turns[0]
        assert "send at most one consolidated user-facing response" in consolidated
        for i in range(3):
            assert f"summary {i}" in consolidated
            assert f"--- result {i + 1}/3 ---" in consolidated

        # Durable ledger: every member acked exactly once.
        con = ad._connect()
        try:
            rows = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT delegation_id, delivery_state FROM async_delegations "
                    "WHERE delegation_id LIKE 'deleg_coalesce_%'"
                )
            }
        finally:
            con.close()
        assert rows == {
            f"deleg_coalesce_{i}": "delivered" for i in range(3)
        }, rows
    finally:
        server._sessions.pop("sid_coalesce", None)
        with ad._records_lock:
            for i in range(3):
                ad._records.pop(f"deleg_coalesce_{i}", None)
        while not isolated_queue.empty():
            isolated_queue.get_nowait()


def test_notification_poller_keeps_foreign_and_non_delegation_events_separate(
    monkeypatch,
):
    """Only THIS session's delegation completions enter the consolidated turn.

    A foreign-owned delegation and an ordinary completion event must not ride
    the consolidated text; they write their own turns.
    """
    import queue as _queue_mod

    from tools.process_registry import process_registry
    from tools import async_delegation as ad

    turns = []
    emitted = []

    class _Agent:
        def run_conversation(self, prompt, conversation_history=None, stream_callback=None, **_kwargs):
            turns.append(prompt)
            return {
                "final_response": "ok",
                "messages": [{"role": "assistant", "content": "ok"}],
            }

    mine = _session(agent=_Agent())
    other = _session(agent=_Agent())
    server._sessions["sid_mine"] = mine
    server._sessions["sid_other"] = other
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: emitted.append(a))
    monkeypatch.setattr(server, "make_stream_renderer", lambda cols: None)
    monkeypatch.setattr(server, "render_message", lambda raw, cols: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)

    delegation_id = "deleg_foreign_mine"
    record = {
        "delegation_id": delegation_id,
        "goal": "mine",
        "status": "running",
        "dispatched_at": time.time(),
    }
    with ad._records_lock:
        ad._records[delegation_id] = record
    ad._persist_dispatch(record)

    isolated_queue: _queue_mod.Queue = _queue_mod.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", isolated_queue)
    isolated_queue.put({
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "goal": "mine",
        "origin_ui_session_id": "sid_mine",
        "session_key": "session-key",
        "status": "completed",
        "summary": "my summary",
    })
    isolated_queue.put({
        "type": "completion",
        "session_id": "proc_separate",
        "command": "echo hi",
        "exit_code": 0,
        "output": "hi",
    })
    isolated_queue.put({
        "type": "async_delegation",
        "delegation_id": "deleg_foreign_other",
        "goal": "theirs",
        "origin_ui_session_id": "sid_other",
        "session_key": "unrelated-key",
        "status": "completed",
        "summary": "their summary",
    })
    process_registry._completion_consumed.discard("proc_separate")

    # The poller iterates: my delegation first (one turn), then the ordinary
    # completion (its own turn); the foreign delegation is requeued forever
    # until stop. Run it live and stop once both of MY turns landed.
    stop = threading.Event()
    loop = threading.Thread(
        target=server._notification_poller_loop,
        args=(stop, "sid_mine", mine),
        daemon=True,
    )
    loop.start()
    try:
        deadline = time.time() + 10
        while len(turns) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        loop.join(timeout=5)

    # My delegation becomes one consolidated turn carrying ONLY my summary;
    # the ordinary background-process completion writes its own separate
    # turn; the foreign delegation never leaks into any of this session's
    # turns.
    assert len(turns) == 2, [
        (t[:80], "[IMPORTANT" in t) for t in turns
    ]
    delegation_turn = turns[0] if "my summary" in turns[0] else turns[1]
    completion_turn = turns[1] if delegation_turn is turns[0] else turns[0]
    assert "my summary" in delegation_turn
    assert "their summary" not in delegation_turn
    assert "[IMPORTANT: Background process proc_separate" in completion_turn
    # Nothing of mine left queued (the foreign event may remain — it
    # belongs to sid_other).
    remaining = []
    while not isolated_queue.empty():
        remaining.append(isolated_queue.get_nowait())
    assert all(
        str(e.get("origin_ui_session_id") or "") == "sid_other" for e in remaining
    ), remaining


def test_coalesced_dispatch_releases_claims_on_failure():
    """When the consolidated submission fails, every claim must be released
    and every event returned for requeue — the ledger never acks undelivered
    work."""
    from tools import async_delegation as ad

    events = _make_delegation_events("sid_groupfail", "deleg_groupfail", 3)

    deliverables = [
        (evt, f"formatted {i}") for i, evt in enumerate(events)
    ]
    claim_ids = []
    try:
        for evt in events:
            claim = ad.claim_event_delivery(evt, "tui-poller")
            assert claim is not None
            claim_ids.append((evt, claim))

        def _failing_submit(*_args, **_kwargs):
            raise RuntimeError("boom")

        ok, missed = server._dispatch_grouped_delegation_notifications(
            "sid_groupfail", _session(), deliverables, claim_ids,
            _dispatch_fn=_failing_submit,
        )
        assert ok is False
        assert [e["delegation_id"] for e in missed] == [
            f"deleg_groupfail_{i}" for i in range(3)
        ]

        con = ad._connect()
        try:
            rows = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT delegation_id, delivery_state FROM async_delegations "
                    "WHERE delegation_id LIKE 'deleg_groupfail_%'"
                )
            }
        finally:
            con.close()
        assert all(state == "pending" for state in rows.values()), rows
    finally:
        with ad._records_lock:
            for i in range(3):
                ad._records.pop(f"deleg_groupfail_{i}", None)


def test_host_route_grouped_dispatch_registers_single_owner(monkeypatch):
    """In the compute-host child the consolidated batch rides host admission.

    One admitted owner (request id + running) for the whole batch; every
    member is acked only after admission accepted the consolidated turn.
    """
    import queue as _queue_mod

    from tools.process_registry import process_registry
    from tools import async_delegation as ad

    sess = _session()
    server._sessions["sid_hostroute"] = sess
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")

    admissions = []

    def _admitter(frame):
        admissions.append(frame)
        return True

    server.register_local_host_turn_admitter(_admitter)
    try:
        events = _make_delegation_events("sid_hostroute", "deleg_hostroute", 4)
        isolated_queue: _queue_mod.Queue = _queue_mod.Queue()
        monkeypatch.setattr(process_registry, "completion_queue", isolated_queue)
        for evt in events:
            isolated_queue.put(evt)

        stop = threading.Event()
        stop.set()
        server._notification_poller_loop(stop, "sid_hostroute", sess)

        assert len(admissions) == 1, f"expected 1 admission, got {len(admissions)}"
        frame = admissions[0]
        assert frame["sid"] == "sid_hostroute"
        assert str(frame.get("request_id") or "").startswith("auto-turn-")
        assert "send at most one consolidated user-facing response" in frame["text"]
        assert "--- result 4/4 ---" in frame["text"]
        assert frame["display_kind"] == "async_delegation_complete"
        assert frame["display_metadata"].get("coalesced") is True
        # Host route never pre-claims the busy flag; admission owns running.
        assert sess["running"] is False

        con = ad._connect()
        try:
            rows = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT delegation_id, delivery_state FROM async_delegations "
                    "WHERE delegation_id LIKE 'deleg_hostroute_%'"
                )
            }
        finally:
            con.close()
        assert rows == {
            f"deleg_hostroute_{i}": "delivered" for i in range(4)
        }, rows
    finally:
        server.register_local_host_turn_admitter(None)
        server._sessions.pop("sid_hostroute", None)
        with ad._records_lock:
            for i in range(4):
                ad._records.pop(f"deleg_hostroute_{i}", None)


def test_host_route_refusal_releases_all_claims_and_requeues(monkeypatch):
    """A busy host refusal releases every claim and requeues every event."""
    import queue as _queue_mod

    from tools.process_registry import process_registry
    from tools import async_delegation as ad

    sess = _session()
    server._sessions["sid_hostbusy"] = sess
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere",
                        lambda *a, **kw: False)
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")

    def _refusing_admitter(frame):
        return False

    server.register_local_host_turn_admitter(_refusing_admitter)
    try:
        events = _make_delegation_events("sid_hostbusy", "deleg_hostbusy", 2)
        isolated_queue: _queue_mod.Queue = _queue_mod.Queue()
        monkeypatch.setattr(process_registry, "completion_queue", isolated_queue)
        for evt in events:
            isolated_queue.put(evt)

        stop = threading.Event()
        stop.set()
        server._notification_poller_loop(stop, "sid_hostbusy", sess)

        # The drain requeues the whole batch for a later retry: both events
        # are back on the queue and their durable rows stay pending.
        requeued = []
        while not isolated_queue.empty():
            requeued.append(isolated_queue.get_nowait())
        assert sorted(e["delegation_id"] for e in requeued) == [
            "deleg_hostbusy_0", "deleg_hostbusy_1",
        ], requeued

        con = ad._connect()
        try:
            rows = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT delegation_id, delivery_state FROM async_delegations "
                    "WHERE delegation_id LIKE 'deleg_hostbusy_%'"
                )
            }
        finally:
            con.close()
        assert all(state == "pending" for state in rows.values()), rows
    finally:
        server.register_local_host_turn_admitter(None)
        server._sessions.pop("sid_hostbusy", None)
        with ad._records_lock:
            for i in range(2):
                ad._records.pop(f"deleg_hostbusy_{i}", None)


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }
