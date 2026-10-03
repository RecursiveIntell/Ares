"""Real child blockers and receipts cross the serving gateway's control path."""
import json
import multiprocessing
import threading
import time

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSendUncertain, HostSupervisor
from tests.tui_gateway.test_prompt_recovery_contract import _session


def child(connection, batch):
    import os
    os.environ["HERMES_COMPUTE_HOST_CHILD"] = "1"
    server._pending, server._answers, server._pending_prompt_payloads = {}, {}, {}
    server._batch_clarify, server._clarify_request_owners, server._clarify_response_receipts = {}, {}, {}

    class Writer:
        def __init__(self):
            self.buffer = ""

        def write(self, data):
            self.buffer += data
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                connection.send(json.loads(line))

        def flush(self):
            pass

    host = ComputeHost(stdout=Writer(), heartbeat_secs=0)
    session = _session(running=True)
    session["transport"] = host._transport
    server._sessions = {"s": session}
    answers = []
    payload = {"questions": [{"qid": "q0", "question": "Color?"}, {"qid": "q1", "question": "Name?"}]} if batch else {"question": "Confirm?"}
    worker = threading.Thread(target=lambda: answers.append(server._block(
        "clarify.request", "s", payload, timeout=30, batch_qids=["q0", "q1"] if batch else None)), daemon=True)
    connection.send({"type": "ready", "boot_id": host._boot_id})
    worker.start()
    try:
        while True:
            command = connection.recv()
            if command == "inspect":
                worker.join(.1)
                connection.send({"answers": answers, "pending": bool(server._pending)})
            elif command == "replace":
                replacement = _session(running=False)
                replacement["transport"] = host._transport
                server._sessions["s"] = replacement
                connection.send({"replaced": True})
            elif command == "cancel":
                server._clear_pending("s")
                worker.join(2)
                connection.send({"cancelled": True})
            elif command == "stop":
                server._clear_pending("s")
                worker.join(2)
                return
            else:
                host._handle_control(command)
    finally:
        host.close()
        connection.close()


@pytest.fixture(params=[False, True], ids=["single", "batch"])
def bridge(tmp_path, monkeypatch, request):
    parent, child_pipe = multiprocessing.get_context("spawn").Pipe()
    process = multiprocessing.get_context("spawn").Process(target=child, args=(child_pipe, request.param))
    received = []

    class Transport:
        def write(self, frame):
            received.append(frame)
            return True

    transport = Transport()
    session = _session(running=True)
    session.update(transport=transport, _compute_host_active=True)
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})
    monkeypatch.setattr(server, "_pending_approval_request_payload", lambda *_: None)
    monkeypatch.setattr(server, "_fallback_session_info", lambda *_: {})
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False, rpc_sink=server.write_json)
    monkeypatch.setattr(server, "_compute_host_supervisor", host)
    writes = []
    drop = []

    def send(frame, **kwargs):
        assert kwargs["expected_boot_id"] == host.boot_id
        writes.append(frame)
        parent.send(frame)
        assert parent.poll(5)
        ack = parent.recv()
        if drop and frame["route_name"] == "clarify.respond":
            drop.pop()
            raise HostSendUncertain("controlled lost child ACK")
        host._handle_host_frame({**ack, "_host_boot_id": host.boot_id})

    monkeypatch.setattr(host, "is_ready", lambda: process.is_alive())
    monkeypatch.setattr(host, "_send_owner_control_bounded", send)
    process.start()
    child_pipe.close()
    assert parent.poll(5)
    host._hello = {"boot_id": parent.recv()["boot_id"]}
    assert parent.poll(5)
    host._handle_host_frame(parent.recv())
    rid = received[-1]["params"]["payload"]["request_id"]

    def inspect():
        parent.send("inspect")
        assert parent.poll(5)
        return parent.recv()

    def respond(answer="yes", qid=None, caller=transport):
        params = {"session_id": "s", "request_id": rid, "answer": answer}
        if qid is not None:
            params["question_id"] = qid
        replies = []
        ready = threading.Event()
        original_write = caller.write

        def receive(frame):
            if frame.get("id") == "answer-rpc":
                replies.append(frame)
                ready.set()
            return original_write(frame)

        caller.write = receive
        try:
            response = server.dispatch({"id": "answer-rpc", "method": "clarify.respond", "params": params}, caller)
            if response is not None:
                return response
            assert ready.wait(5), "registered answer worker did not reply"
            return replies[0]
        finally:
            caller.write = original_write

    yield session, host, parent, rid, respond, inspect, writes, drop, transport, request.param
    parent.send("stop")
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(2)
    parent.close()
    assert process.exitcode == 0


def test_resume_and_explicit_answer_reach_exact_child(bridge):
    session, _, _, rid, respond, inspect, writes, _, _, batch = bridge
    resume = server._live_session_payload("s", session, omit_messages=True)
    assert resume["pending_clarify"]["request_id"] == rid
    if batch:
        assert respond("blue", "q0")["result"] == {"status": "ok", "remaining": ["q1"]}
        assert server._live_session_payload("s", session, omit_messages=True)["pending_clarify"]["answers"] == {"q0": "blue"}
        assert respond("packet", "q1")["result"] == {"status": "ok", "remaining": []}
        expected = json.dumps({"answers": {"q0": "blue", "q1": "packet"}})
    else:
        assert respond()["result"] == {"status": "ok"}
        expected = "yes"
    assert inspect() == {"answers": [expected], "pending": False}
    assert "pending_clarify" not in server._live_session_payload("s", session, omit_messages=True)
    count = len([f for f in writes if f["route_name"] == "clarify.respond"])
    assert count == (2 if batch else 1)


def test_lost_child_ack_resume_and_explicit_retry_confirm_without_redelivery(bridge):
    session, _, _, _, respond, inspect, writes, drop, _, batch = bridge
    server._live_session_payload("s", session, omit_messages=True)
    drop.append(True)
    assert respond()["error"]["code"] == 5032  # cancel-all also applies to a batch
    assert inspect() == {"answers": ["yes"], "pending": False}
    assert "pending_clarify" not in server._live_session_payload("s", session, omit_messages=True)
    assert len([f for f in writes if f["route_name"] == "clarify.respond"]) == 1
    session.pop("_compute_host_active_request_id", None)  # outer turn can settle before retry
    assert respond()["result"] == {"status": "ok"}
    assert respond("changed")["result"] == {"status": "conflict"}
    assert inspect()["answers"] == ["yes"]


def test_unattached_reconnect_and_generation_replacement_cannot_answer(bridge):
    session, _, pipe, _, respond, inspect, writes, _, transport, _ = bridge
    class Other:
        def write(self, _):
            return True
    other = Other()
    assert respond(caller=other)["error"]["code"] == 4030
    assert writes == []
    server._live_session_payload("s", session, omit_messages=True, transport=other)
    pipe.send("replace")
    assert pipe.poll(5) and pipe.recv() == {"replaced": True}
    assert respond(caller=other)["error"]["code"] == 5032
    assert inspect()["answers"] == []
    assert server._live_session_payload("s", session, omit_messages=True)["pending_clarify_unavailable"] is True


def test_boot_replacement_and_unavailable_child_do_not_retarget_or_expire(bridge, monkeypatch):
    session, host, _, _, respond, inspect, writes, _, _, _ = bridge
    server._live_session_payload("s", session, omit_messages=True)
    host._hello = {"boot_id": "replacement-boot"}
    before = len(writes)
    assert respond()["error"]["code"] == 5032
    assert len(writes) == before and inspect()["answers"] == []
    monkeypatch.setattr(host, "is_ready", lambda: False)
    snapshot = server._live_session_payload("s", session, omit_messages=True)
    assert snapshot["pending_clarify_unavailable"] is True
    assert "pending_clarify" not in snapshot


def test_completed_or_cancelled_child_receipt_is_authoritative(bridge):
    session, _, pipe, _, respond, inspect, writes, drop, _, batch = bridge
    server._live_session_payload("s", session, omit_messages=True)
    if batch:
        assert respond("blue", "q0")["result"]["remaining"] == ["q1"]
        drop.append(True)
        assert respond("packet", "q1")["error"]["code"] == 5032
        assert inspect()["pending"] is False
        assert respond("packet", "q1")["result"] == {"status": "ok", "remaining": []}
        assert respond("blue", "q0")["result"] == {"status": "ok", "remaining": []}
        assert respond("changed", "q0")["result"] == {"status": "conflict"}
        assert len(inspect()["answers"]) == 1
    else:
        pipe.send("cancel")
        assert pipe.poll(5) and pipe.recv() == {"cancelled": True}
        before = len(writes)
        assert "pending_clarify" not in server._live_session_payload("s", session, omit_messages=True)
        assert respond()["result"] == {"status": "conflict"}  # cancelled with an empty answer
        assert inspect() == {"answers": [""], "pending": False}
        assert len(writes) == before + 2  # one read and one explicit answer, no retry


def test_parent_rebind_during_answer_ack_remains_uncertain(bridge, monkeypatch):
    session, host, _, _, respond, inspect, writes, _, transport, _ = bridge
    server._live_session_payload("s", session, omit_messages=True)
    send = host._send_owner_control_bounded

    def rebind(frame, **kwargs):
        send(frame, **kwargs)
        if frame["route_name"] == "clarify.respond":
            replacement = _session(running=False)
            replacement.update(transport=transport, _compute_host_active=True)
            server._sessions["s"] = replacement

    monkeypatch.setattr(host, "_send_owner_control_bounded", rebind)
    assert respond()["error"]["code"] == 5032
    assert inspect() == {"answers": ["yes"], "pending": False}
    assert len([f for f in writes if f["route_name"] == "clarify.respond"]) == 1


def test_host_answer_wait_leaves_reader_available_for_stop_dispatch(bridge, monkeypatch):
    session, host, _, rid, _, inspect, _, _, transport, _ = bridge
    server._live_session_payload("s", session, omit_messages=True)
    entered, release, replied = threading.Event(), threading.Event(), threading.Event()
    original_send, original_write = host._send_owner_control_bounded, transport.write
    responses = []

    def paused(frame, **kwargs):
        if frame["route_name"] == "clarify.respond":
            entered.set()
            assert release.wait(5)
        return original_send(frame, **kwargs)

    def receive(frame):
        if frame.get("id") == "held-answer":
            responses.append(frame)
            replied.set()
        return original_write(frame)

    monkeypatch.setattr(host, "_send_owner_control_bounded", paused)
    monkeypatch.setattr(transport, "write", receive)
    # This test owns reader dispatch only. The unchanged targeted-stop suite
    # independently exercises the actual interrupt mutation and acknowledgement.
    stops = []
    monkeypatch.setitem(server._methods, "session.interrupt", lambda rpc, params:
        (stops.append(params["session_id"]), server._ok(rpc, {"status": "reader-reached"}))[1])
    try:
        assert server.dispatch({"id": "held-answer", "method": "clarify.respond", "params": {
            "session_id": "s", "request_id": rid, "answer": "yes"}}, transport) is None
        assert entered.wait(2)
        stop = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {"session_id": "s"}}, transport)
        assert stop["result"]["status"] == "reader-reached" and stops == ["s"]
        assert not replied.is_set()
    finally:
        release.set()
    assert replied.wait(5)
    assert responses[0]["result"]["status"] == "ok"
    assert inspect()["answers"] == ["yes"]


def test_queued_answer_cannot_rebind_to_successor_record(bridge, monkeypatch):
    _, _, _, rid, _, inspect, writes, _, transport, _ = bridge
    jobs, replies = [], []

    class Pool:
        def submit(self, job):
            jobs.append(job)

    monkeypatch.setattr(server, "_pool", Pool())
    monkeypatch.setattr(transport, "write", lambda frame: replies.append(frame) or True)
    assert server.dispatch({"id": "queued-answer", "method": "clarify.respond", "params": {
        "session_id": "s", "request_id": rid, "answer": "yes"}}, transport) is None
    replacement = _session(running=False)
    replacement.update(transport=transport, _compute_host_active=True)
    server._sessions["s"] = replacement
    assert len(jobs) == 1
    jobs[0]()
    assert replies[0]["error"]["code"] == 4030
    assert writes == [] and inspect()["answers"] == []
