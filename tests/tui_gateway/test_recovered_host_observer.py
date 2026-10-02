"""A re-adopted owner's terminal observer is request-scoped."""
import io
import json
import os
import threading

from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
import types
from pathlib import Path

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost

from tui_gateway.host_supervisor import HostSupervisor


def test_rejected_second_request_cannot_consume_owner_observer(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_ready", lambda: True)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **_kwargs: None)
    observed = []
    rejected = []
    host.observe_session("s", observed.append, request_id="A")
    host.submit_turn({"sid": "s", "request_id": "B"}, on_complete=rejected.append)
    refusal = {"type": "turn.error", "sid": "s", "request_id": "B",
               "reason": "admission_refused_busy"}
    host._complete_turn(refusal)
    wait_for_terminal_projection(host)
    assert rejected == [refusal]
    assert observed == []
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    assert observed == [terminal]


def test_reclaimed_observer_supersedes_old_parent_callback(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_ready", lambda: True)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **_kwargs: None)
    old_parent = []
    new_parent = []
    host.submit_turn({"sid": "s", "request_id": "A"}, on_complete=old_parent.append)
    host.observe_session("s", new_parent.append, request_id="A")
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    assert old_parent == [], "superseded parent must not emit or drain its old queue"
    assert new_parent == [terminal]
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    assert new_parent == [terminal]


@pytest.mark.parametrize("with_pending", [False, True])
def test_crash_settles_only_current_projection_owner(tmp_path, monkeypatch, with_pending):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_ready", lambda: True)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **_kwargs: None)
    old_parent = []
    new_parent = []
    if with_pending:
        host.submit_turn({"sid": "s", "request_id": "A"}, on_complete=old_parent.append)
    host.observe_session("s", new_parent.append, request_id="A")
    host._fail_pending_turns(reason="crash", message="host gone")
    wait_for_terminal_projection(host)
    assert old_parent == []
    assert len(new_parent) == 1
    assert new_parent[0]["reason"] == "crash"
    assert new_parent[0]["request_id"] == "A"
    assert not host._session_observers
    assert not host._pending_turns
    host._fail_pending_turns(reason="crash", message="host gone")
    wait_for_terminal_projection(host)
    assert len(new_parent) == 1


def test_crash_notification_failure_does_not_strand_observers(tmp_path):
    def failing_sink(message):
        raise OSError("renderer disconnected")
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False,
                          rpc_sink=failing_sink)
    observed = []
    host.observe_session("s1", observed.append, request_id="A")
    host.observe_session("s2", observed.append, request_id="B")
    host._fail_pending_turns(reason="crash", message="host gone")
    wait_for_terminal_projection(host)
    assert {f["request_id"] for f in observed} == {"A", "B"}
    assert not host._session_observers


def test_wrong_session_or_request_cannot_consume_observer(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    observed = []
    host.observe_session("s", observed.append, request_id="A")
    host._complete_turn({"type": "turn.end", "sid": "other", "request_id": "A"})
    wait_for_terminal_projection(host)
    host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "other"})
    wait_for_terminal_projection(host)
    assert observed == []
    host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "A"})
    wait_for_terminal_projection(host)
    assert len(observed) == 1


@pytest.mark.parametrize("request_id", ["", None])
def test_unidentified_observer_is_refused(tmp_path, request_id):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    with pytest.raises(ValueError, match="request"):
        host.observe_session("s", lambda frame, **_kwargs: None, request_id=request_id)


def test_lookup_reports_request_owner_during_running_flag_handoff(monkeypatch):
    state = {"session_key": "stored", "history_lock": threading.Lock(), "running": False}
    monkeypatch.setattr(server, "_sessions", {"s": state})
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._active_request_ids["s"] = "A"
        host._handle_session_lookup({"session_key": "stored", "request_id": "lookup"})
        reply = json.loads(output.getvalue().strip())
        assert reply["sessions"] == [{"session_id": "s", "request_id": "A",
                                      "running": True, "session_info": {}}]
    finally:
        host.close()


def test_unidentified_running_owner_is_not_published(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: None)
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: True)
    supervisor = types.SimpleNamespace(wait_ready=lambda: None, lookup_session_key=lambda key: {"session_id": "s", "running": True})
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    releases = []
    lease = types.SimpleNamespace(release=lambda: releases.append(True))
    with pytest.raises(RuntimeError, match="request id"):
        server._claim_or_reuse_live("new", "stored", {}, lease)
    assert server._sessions == {}
    assert releases == [True]


def test_terminal_before_observer_registration_reconciles_idle_owner(monkeypatch):
    """A completed host request must not leave an adopted mirror running."""
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: None)
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: True)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_apply_compute_host_metadata_mirror", lambda *a: None)
    monkeypatch.setattr(server, "_cancel_ws_orphan_reap", lambda *a: None)
    snapshots = iter([
        {"session_id": "host", "request_id": "A", "running": True,
         "host_boot_id": "boot-owner"},
        {"session_id": "host", "running": False, "host_boot_id": "boot-owner"},
    ])
    observers = {}

    def observe(sid, callback, *, request_id, expected_boot_id=None):
        assert expected_boot_id == "boot-owner"
        observers[sid] = (request_id, callback)

    def unobserve(sid, *, request_id, expected_boot_id=None):
        assert expected_boot_id == "boot-owner"
        if observers.get(sid, (None,))[0] != request_id:
            return False
        observers.pop(sid)
        return True

    supervisor = types.SimpleNamespace(
        wait_ready=lambda: None,
        lookup_session_key=lambda key: next(snapshots),
        observe_session=observe,
        unobserve_session=unobserve,
    )
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    released = []
    lease = types.SimpleNamespace(release=lambda: released.append(True))
    record = {"session_key": "stored", "history_lock": threading.Lock(), "running": False}
    assert server._claim_or_reuse_live("new", "stored", record, lease) == ("host", record)
    assert server._sessions["host"] is record
    assert record["running"] is False
    assert "_compute_host_active_request_id" not in record
    assert not observers
    assert released == [True]


def test_unconfirmed_post_registration_read_preserves_observer_and_releases_lease(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: None)
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: True)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_apply_compute_host_metadata_mirror", lambda *a: None)
    monkeypatch.setattr(server, "_cancel_ws_orphan_reap", lambda *a: None)
    reads = []
    observers = {}

    def lookup(_key):
        reads.append(True)
        if len(reads) == 1:
            return {"session_id": "host", "request_id": "A", "running": True,
                    "host_boot_id": "boot-owner"}
        raise TimeoutError("host readback unconfirmed")

    def observe(sid, callback, *, request_id, expected_boot_id):
        observers[sid] = (request_id, expected_boot_id, callback)

    supervisor = types.SimpleNamespace(wait_ready=lambda: None, lookup_session_key=lookup, observe_session=observe)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    released = []
    lease = types.SimpleNamespace(release=lambda: released.append(True))
    record = {"session_key": "stored", "history_lock": threading.Lock(), "running": False}
    assert server._claim_or_reuse_live("new", "stored", record, lease) == ("host", record)
    assert record["_host_delivery_uncertain"] is True
    assert record["_compute_host_active_request_id"] == "A"
    assert observers["host"][:2] == ("A", "boot-owner")
    assert released == [True]


def test_exact_unobserve_cannot_retire_newer_request_or_boot(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    old = []
    new = []
    host._hello = {"boot_id": "boot-new"}
    host.observe_session("s", old.append, request_id="A")
    host.observe_session("s", new.append, request_id="B")
    assert host.unobserve_session("s", request_id="A") is False
    assert host.unobserve_session("s", request_id="B", expected_boot_id="boot-old") is False
    assert host._session_observers["s"][0] == "B"
    assert host.unobserve_session("s", request_id="B", expected_boot_id="boot-new") is True
    assert not host._session_observers


@pytest.mark.parametrize("crash", [False, True])
def test_real_host_lookup_and_exact_observer_settle_once(tmp_path, monkeypatch, crash):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    host = HostSupervisor(registry_path=home / "host.json", autostart=False,
        heartbeat_secs=0, respawn_max=0,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "HERMES_HOME": str(home),
             "HERMES_ISO_CERTIFY_SYNTH_TURN": "1", "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0"})
    started = threading.Event()
    done = threading.Event()
    observed = []
    original = host._handle_host_frame

    def capture(frame):
        if frame.get("type") == "turn.started":
            started.set()
        original(frame)

    def observe(frame):
        observed.append(frame)
        done.set()

    monkeypatch.setattr(host, "_handle_host_frame", capture)
    try:
        host.submit_turn({"sid": "runtime", "session_key": "stored", "request_id": "A",
                          "source": "desktop", "text": json.dumps({"duration_s": 2, "chunk": 1000})})
        assert started.wait(8)
        owner = host.lookup_session_key("stored")
        assert owner is not None
        assert owner["running"] is True
        assert owner["request_id"] == "A"
        host.observe_session(owner["session_id"], observe, request_id=owner["request_id"])
        if crash:
            assert host._proc is not None
            host._proc.kill()  # only this test's disposable child
        assert done.wait(8)
        assert len(observed) == 1
        assert observed[0]["type"] == ("turn.error" if crash else "turn.end")
        if crash:
            assert observed[0]["reason"] == "crash"
        assert observed[0]["request_id"] == "A"
        host._complete_turn(observed[0])
        wait_for_terminal_projection(host)
        assert len(observed) == 1
    finally:
        host.shutdown()
