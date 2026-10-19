"""Autonomous dispatch must register a complete host owner before running work.

Reproduced defect class (2026-10-17, `autonomous-owner-probe`): when an idle
background-notification turn starts, the serving path sets ``session["running"]``
and calls ``_run_prompt_submit`` directly. In the compute-host child that
bypasses the host's request-admission map, so the very next resume sees
``running=True`` with ``request_id=None`` and refuses with:

    RuntimeError: compute-host running owner omitted its request id

The guard is correct; the bypass is the bug. These tests pin the desired
invariant — a dispatched autonomous turn is visible to host owner lookup as a
*complete* owner (running WITH a request identity), resume adopts that owner
instead of refusing, and the event's durable delivery claim is settled only
for admitted work.

RED (pre-fix): test 1 fails on the missing request id; test 2 raises the
refusal above. GREEN (post-fix): the poller routes dispatch through host
admission and both invariants hold.
"""

import io
import json
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tools import async_delegation
from tools import process_registry as registry_module


def _frames(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


@pytest.fixture
def autonomous_env(monkeypatch):
    """A real ComputeHost + one stored session + one queued completion event.

    Model execution is replaced by a bounded real worker thread; the poller,
    host admission, lookup and resume guard are the real implementations.
    """
    stop = threading.Event()
    release = threading.Event()
    started = threading.Event()
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    workers: list[threading.Thread] = []
    deliveries = {"claims": [], "completed": [], "released": []}

    session = {
        "agent": SimpleNamespace(session_id="stored"),
        "session_key": "stored",
        "session_id": "runtime",
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
    }

    events: queue.Queue = queue.Queue()
    events.put({"type": "completion", "session_id": "process-1", "session_key": "stored"})
    registry = SimpleNamespace(
        completion_queue=events,
        is_completion_consumed=lambda _sid: False,
    )

    def run_prompt_submit(rid, sid, state, text, **kwargs):
        # Mirror a real submitted turn: a bounded worker is installed as the
        # session's run thread so the host keeps this admission alive until
        # the worker settles (the lifecycle contract from the thread repair).
        worker = threading.Thread(target=lambda: (started.set(), release.wait(10)))
        workers.append(worker)
        with server._sessions_lock:
            state["_run_thread"] = worker
            worker.start()
        assert started.wait(2)
        stop.set()  # one poller iteration is enough to observe the invariant
        return True

    def claim(evt, owner):
        deliveries["claims"].append(owner)
        return "claim-1"

    monkeypatch.setattr(server, "_sessions", {"runtime": session})
    monkeypatch.setattr(server, "_session_info", lambda *a, **kw: {})
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_collect_kanban_notifications", lambda *a, **kw: [])
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_notification_event_requires_owner", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt_submit)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda state: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda state: None)
    monkeypatch.setattr(server, "_begin_turn_outcome", lambda *a, **kw: None)

    def start_inflight(state, text):
        state["inflight_turn"] = {"text": text, "started_at": 1}

    monkeypatch.setattr(server, "_start_inflight_turn", start_inflight)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda state: state.pop("inflight_turn", None))
    monkeypatch.setattr(server, "_apply_compute_host_metadata_mirror", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **kw: None)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: session)
    monkeypatch.setattr(registry_module, "process_registry", registry)
    monkeypatch.setattr(registry_module, "format_process_notification", lambda evt: "background completion")
    monkeypatch.setattr(async_delegation, "claim_event_delivery", claim)
    monkeypatch.setattr(async_delegation, "complete_event_delivery",
                        lambda evt, c: deliveries["completed"].append(c))
    monkeypatch.setattr(async_delegation, "release_event_delivery",
                        lambda evt, c: deliveries["released"].append(c))
    monkeypatch.setitem(sys.modules, "hermes_undo",
                        SimpleNamespace(on_user_message_appended=lambda *a, **kw: None))

    # The child registers its local admission hook; mirror run_host() exactly.
    server.register_local_host_turn_admitter(host.admit_autonomous_turn)

    try:
        yield host, session, output, workers, deliveries, stop, release
    finally:
        stop.set()
        release.set()
        for worker in workers:
            worker.join(5)
        with host._turn_futures_lock:
            futures = list(host._turn_futures)
        for future in futures:
            future.result(timeout=10)
        host.close()
        server.register_local_host_turn_admitter(None)


def _host_owner_lookup(host, output, session_key):
    host._handle_session_lookup({"request_id": "lookup", "session_key": session_key})
    frame = _frames(output)[-1]
    assert frame["type"] == "session.lookup.ack"
    owner = frame["sessions"][0]
    owner.setdefault("host_boot_id", "boot")
    return owner


def test_autonomous_dispatch_registers_complete_host_owner(autonomous_env, monkeypatch):
    host, session, output, workers, deliveries, stop, release = autonomous_env
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")

    server._notification_poller_loop(stop, "runtime", session)

    assert deliveries["claims"], "the poller must claim the event's delivery"
    assert deliveries["completed"], "an admitted event's claim must be settled"
    assert not deliveries["released"], "an admitted event must not be released"
    assert workers and workers[0].is_alive(), "the queued work must actually run"

    owner = _host_owner_lookup(host, output, "stored")
    assert owner["running"] is True, "the dispatched turn must be owned"
    assert owner["request_id"], (
        "autonomous dispatch bypassed host admission: owner lookup reports "
        "running with no request id - the 'omitted its request id' resume refusal"
    )


def test_resume_adopts_autonomous_owner_instead_of_refusing(autonomous_env, monkeypatch):
    host, session, output, workers, deliveries, stop, release = autonomous_env
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")

    server._notification_poller_loop(stop, "runtime", session)
    owner = _host_owner_lookup(host, output, "stored")
    assert owner["request_id"], (
        "precondition: the autonomous owner must carry a request id "
        "(fails here pre-fix with the reproduced refusal)"
    )
    observed = []
    supervisor = SimpleNamespace(
        wait_ready=lambda: None,
        lookup_session_key=lambda key: dict(owner),
        observe_session=lambda sid, cb, **kw: observed.append({"sid": sid, **kw}),
        unobserve_session=lambda *a, **kw: False,
    )
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda *a, **kw: True)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key: None)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a, **kw: supervisor)

    record: dict = {}
    adopted = server._claim_or_reuse_live("runtime-new", "stored", record, None)

    assert adopted is not None, "resume must adopt the live owner, not mint a duplicate"
    adopted_sid, adopted_record = adopted
    assert adopted_sid == "runtime"
    assert adopted_record["running"] is True
    assert adopted_record["_compute_host_active_request_id"] == owner["request_id"]
    assert observed and observed[0]["request_id"] == owner["request_id"], (
        "the adopted owner's exact request identity must be registered for terminal observation"
    )
