"""Cold resume waits for host readiness before its short ownership query."""

import sys
import json
import threading

import pytest

from tui_gateway import server
from tui_gateway.host_supervisor import HostSendNotSent, HostSendUncertain, HostSupervisor
from tests.tui_gateway.test_session_resume_db_ownership import _RecordingDB


@pytest.fixture
def delayed_host(tmp_path):
    # A real disposable pipe peer. No model, credentials, or user session data.
    script = tmp_path / "peer.py"
    script.write_text('''
import json, os, sys, time, uuid
from pathlib import Path
Path(sys.argv[1]).write_text("starting", encoding="utf-8")
time.sleep(float(sys.argv[2]))
print(json.dumps({"type":"hello", "boot_id":uuid.uuid4().hex,
    "build_sha":"fixture", "hermes_home":os.environ["HERMES_HOME"]}), flush=True)
for raw in sys.stdin:
    frame=json.loads(raw)
    with open(sys.argv[3], "a", encoding="utf-8") as log:
        log.write(raw)
    if frame["type"] == "shutdown":
        print(json.dumps({"type":"shutdown.ack", "request_id":frame["request_id"]}), flush=True)
        break
    if frame["type"] == "session.lookup":
        print(json.dumps({"type":"session.lookup.ack", "request_id":frame["request_id"],
            "sessions":[]}), flush=True)
''', encoding="utf-8")
    host = HostSupervisor(
        registry_path=tmp_path / "host.json",
        argv=[sys.executable, str(script), str(tmp_path / "started"), "2.2", str(tmp_path / "frames")],
        expected_build_sha="fixture", autostart=False, respawn_max=0,
    )
    yield host
    host.shutdown()


def test_cold_resume_outlasts_short_owner_lookup(delayed_host, monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: delayed_host)
    record = {"history_lock": threading.Lock(), "session_key": "stored", "running": False}

    assert server._claim_or_reuse_live("resumed", "stored", record, None) is None
    assert server._sessions == {"resumed": record}
    assert delayed_host.boot_id
    assert not delayed_host._pending_turns


def test_rejected_hello_is_quarantined(delayed_host):
    delayed_host.argv[-2] = "0"
    delayed_host.expected_build_sha = "different-build"
    with pytest.raises(HostSendNotSent, match="build mismatch"):
        delayed_host.wait_ready(timeout=3)
    assert delayed_host._proc is None
    assert delayed_host.hello == {}
    assert not delayed_host.registry_path.exists()


def test_live_child_is_not_ready_until_validation_finishes(delayed_host, monkeypatch):
    delayed_host.argv[-2] = "0"
    entered, release = threading.Event(), threading.Event()
    validate = delayed_host._validate_hello

    def blocked_validation():
        entered.set()
        assert release.wait(5)
        validate()

    monkeypatch.setattr(delayed_host, "_validate_hello", blocked_validation)
    errors = []

    def first_waiter():
        try:
            delayed_host.wait_ready(timeout=4)
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=first_waiter)
    first.start()
    try:
        assert entered.wait(3)
        assert delayed_host.is_running()
        with pytest.raises(HostSendNotSent, match="startup deadline"):
            delayed_host.wait_ready(timeout=0.05)
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    assert not errors


def test_lookup_pins_replacement_boot_after_readiness(delayed_host):
    delayed_host.argv[-2] = "0"
    delayed_host._hello = {"boot_id": "previous-boot"}
    assert delayed_host.lookup_session_key("stored", timeout=3) is None
    assert delayed_host.boot_id != "previous-boot"


def test_readiness_deadline_has_no_delayed_request(delayed_host):
    with pytest.raises(HostSendNotSent, match="startup deadline"):
        delayed_host.lookup_session_key("stored", timeout=0.05)
    assert delayed_host._startup_result[0].wait(4)
    assert not delayed_host._pending_controls
    assert not (delayed_host.registry_path.parent / "frames").exists()


def test_cold_resumes_share_startup_without_holding_close_lock(delayed_host, monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: delayed_host)
    entered, release = threading.Event(), threading.Event()
    validate = delayed_host._validate_hello

    def validation():
        entered.set()
        assert release.wait(5)
        validate()

    monkeypatch.setattr(delayed_host, "_validate_hello", validation)
    outcomes, errors = [], []

    def resume(sid):
        record = {"history_lock": threading.Lock(), "session_key": "stored", "running": False}
        try:
            outcomes.append((sid, server._claim_or_reuse_live(sid, "stored", record, None)))
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=resume, args=(sid,)) for sid in ("manual", "automatic")]
    for worker in workers:
        worker.start()
    try:
        assert entered.wait(4)
        assert server._session_resume_lock.acquire(timeout=1), "cold startup held session.close's lock"
        server._session_resume_lock.release()
    finally:
        release.set()
        for worker in workers:
            worker.join(6)
    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    assert len(server._sessions) == 1
    assert sum(winner is None for _sid, winner in outcomes) == 1
    assert len([t for t in delayed_host._restart_times]) == 0


def test_one_request_is_submitted_at_most_once(delayed_host):
    delayed_host.argv[-2] = "0"
    frame = {"sid": "resumed", "request_id": "one-turn", "prompt": "fixture"}
    assert delayed_host.submit_turn(frame) == "one-turn"
    with pytest.raises(ValueError, match="duplicate"):
        delayed_host.submit_turn(frame)
    assert delayed_host.lookup_session_key("stored") is None  # orders pipe consumption
    frames = [json.loads(raw) for raw in (delayed_host.registry_path.parent / "frames").read_text(encoding="utf-8").splitlines()]
    assert sum(frame["type"] == "turn.start" for frame in frames) == 1


def test_lookup_after_real_crash_uses_new_boot(delayed_host):
    delayed_host.argv[-2] = "0"
    delayed_host.respawn_max = 1
    crashed = threading.Event()
    delayed_host.on_crash = crashed.set
    delayed_host.wait_ready()
    previous_boot = delayed_host.boot_id
    delayed_host._proc.kill()  # This fixture's disposable child only.
    assert crashed.wait(3)
    assert delayed_host.lookup_session_key("stored", timeout=3) is None
    assert delayed_host.boot_id and delayed_host.boot_id != previous_boot


def test_shutdown_cancels_startup_without_delayed_submission(delayed_host, monkeypatch):
    delayed_host.argv[-2] = "0"
    entered, release = threading.Event(), threading.Event()
    validate = delayed_host._validate_hello

    def validation():
        entered.set()
        assert release.wait(5)
        validate()

    monkeypatch.setattr(delayed_host, "_validate_hello", validation)
    outcomes = []

    def submit():
        try:
            delayed_host.submit_turn({"sid": "s", "request_id": "cancelled"})
            outcomes.append("sent")
        except HostSendNotSent:
            outcomes.append("not_sent")

    waiter = threading.Thread(target=submit)
    waiter.start()
    assert entered.wait(3)
    closer = threading.Thread(target=delayed_host.shutdown)
    closer.start()
    try:
        waiter.join(2)
        assert not waiter.is_alive(), "shutdown did not cancel pending readiness"
        assert outcomes == ["not_sent"]
    finally:
        release.set()
        waiter.join(5)
        closer.join(5)
    assert not delayed_host._pending_turns
    frames_path = delayed_host.registry_path.parent / "frames"
    if frames_path.exists():
        frames = [json.loads(raw) for raw in frames_path.read_text(encoding="utf-8").splitlines()]
        assert all(frame["type"] != "turn.start" for frame in frames)


def test_readiness_deadline_includes_startup_guard(delayed_host):
    outcomes = []
    finished = threading.Event()
    delayed_host._startup_guard.acquire()

    def wait():
        try:
            delayed_host.wait_ready(timeout=0.05)
        except HostSendNotSent:
            outcomes.append("not_sent")
        finally:
            finished.set()

    waiter = threading.Thread(target=wait)
    waiter.start()
    try:
        assert finished.wait(2), "startup guard ignored the readiness deadline"
        assert outcomes == ["not_sent"]
        assert delayed_host._startup_result is None
    finally:
        delayed_host._startup_guard.release()
        waiter.join(4)


def test_desktop_manual_and_route_resume_share_one_runtime(delayed_host, monkeypatch):
    """Both Desktop entry points call this same session.resume RPC shape."""
    db = _RecordingDB()
    db.rows["stored"] = {"id": "stored", "message_count": 1}
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _: {})
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: delayed_host)
    params = {"session_id": "stored", "source": "desktop", "cols": 96,
              "defer_history": True, "omit_messages": True}
    replies = [server.handle_request({"id": origin, "method": "session.resume", "params": params})
               for origin in ("automatic-route-restore", "manual-retry")]
    assert all("error" not in reply for reply in replies), replies
    assert replies[0]["result"]["session_id"] == replies[1]["result"]["session_id"]
    record = server._sessions[replies[0]["result"]["session_id"]]
    assert record["resume_history_ready"].wait(3)
    assert not record["running"]
    assert len(server._sessions) == 1
    assert not delayed_host._pending_turns


def test_post_lookup_boot_change_is_unconfirmed(delayed_host, monkeypatch):
    delayed_host.argv[-2] = "0"
    delayed_host.wait_ready()
    sent = []
    boot = delayed_host.boot_id

    def answer(frame, **kwargs):
        sent.append(frame)
        delayed_host._hello = {"boot_id": "replacement"}
        delayed_host._handle_host_frame({"type": "session.lookup.ack",
            "request_id": frame["request_id"], "_host_boot_id": boot, "sessions": []})

    monkeypatch.setattr(delayed_host, "_send_owner_control_bounded", answer)
    with pytest.raises(HostSendUncertain, match="unconfirmed"):
        delayed_host.lookup_session_key("stored")
    assert len(sent) == 1
    assert not delayed_host._pending_controls
