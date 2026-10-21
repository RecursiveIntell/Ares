"""Real-process crash points at the stream and settlement boundaries."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_state import SessionDB
from tests.conformance.persistence._harness import kill9_and_reap, spawn_child, wait_for

CHILD = r"""
import os, time
from pathlib import Path
from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from hermes_state import SessionDB
import httpx

db = SessionDB(db_path=Path(os.environ["DB_PATH"]))
execution_id = "turn:api:1:0"
db.create_execution_record(ExecutionRecordV1.create({
    "execution_id": execution_id,
    "session_ref": "session:test",
    "session_generation": 0,
    "turn_ref": "phase:1",
    "call_ref": "ares-call:" + execution_id,
    "admission_ref": "context-dispatch:" + execution_id,
}))
db.transition_execution_record(execution_id, ExecutionState.MATERIALIZED.value,
    materialization_ref="ares-materialization:" + "a" * 64,
    logical_request_digest="sha256:" + "b" * 64)
db.transition_execution_record(execution_id, ExecutionState.SEND_STARTED.value,
    wire_request_digest="sha256:" + "c" * 64,
    wire_bytes=1, governor_qualified=True, governor_mode="enforce")
Path(os.environ["READY"]).write_text("ready")
with httpx.stream("POST", os.environ["URL"], json={"model": "local", "messages": []}, timeout=30) as response:
    for line in response.iter_lines():
        if line:
            Path(os.environ["CHUNK"]).write_text("chunk")
            break
time.sleep(30)
"""

SETTLE_CHILD = r"""
import os, time
from pathlib import Path
from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from hermes_state import SessionDB

db = SessionDB(db_path=Path(os.environ["DB_PATH"]))
execution_id = "turn:api:1:0"
db.create_execution_record(ExecutionRecordV1.create({
    "execution_id": execution_id,
    "session_ref": "session:test",
    "session_generation": 0,
    "turn_ref": "phase:1",
    "call_ref": "ares-call:" + execution_id,
    "admission_ref": "context-dispatch:" + execution_id,
}))
db.transition_execution_record(execution_id, ExecutionState.MATERIALIZED.value,
    materialization_ref="ares-materialization:" + "a" * 64,
    logical_request_digest="sha256:" + "b" * 64)
db.transition_execution_record(execution_id, ExecutionState.SEND_STARTED.value,
    wire_request_digest="sha256:" + "c" * 64,
    wire_bytes=1, governor_qualified=True, governor_mode="enforce")
Path(os.environ["RESPONSE"]).write_text("response-received")
time.sleep(30)
"""


def _stream_server():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers["content-length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"ok": true}\n\n')
            self.wfile.flush()
            time.sleep(30)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, bodies


def test_kill_after_first_stream_chunk_is_indeterminate(tmp_path):
    server, thread, bodies = _stream_server()
    child = None
    db = None
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        ready = tmp_path / "ready"
        chunk = tmp_path / "chunk"
        db_path = tmp_path / "stream.db"
        child = spawn_child(
            CHILD,
            env={
                "DB_PATH": str(db_path),
                "URL": url,
                "READY": str(ready),
                "CHUNK": str(chunk),
            },
        )
        wait_for(lambda: ready.exists(), what="send_started barrier", child=child)
        wait_for(lambda: chunk.exists(), what="first stream chunk", child=child)
        kill9_and_reap(child)
        child = None

        db = SessionDB(db_path=db_path)
        assert db.list_execution_ids() == ["turn:api:1:0"]
        assert db.read_execution_record("turn:api:1:0").state == "send_started"
        reconciled = db.reconcile_execution_record("turn:api:1:0")
        assert reconciled.state == "indeterminate"
        assert reconciled.to_dict()["terminal_outcome"] == "unknown"
        assert len(bodies) == 1
    finally:
        if child is not None:
            kill9_and_reap(child)
        if db is not None:
            db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_kill_after_response_before_settlement_is_indeterminate(tmp_path):
    child = None
    db = None
    try:
        response = tmp_path / "response"
        db_path = tmp_path / "settle.db"
        child = spawn_child(
            SETTLE_CHILD, env={"DB_PATH": str(db_path), "RESPONSE": str(response)}
        )
        wait_for(
            lambda: response.exists(), what="provider response barrier", child=child
        )
        kill9_and_reap(child)
        child = None

        db = SessionDB(db_path=db_path)
        assert db.read_execution_record("turn:api:1:0").state == "send_started"
        reconciled = db.reconcile_execution_record("turn:api:1:0")
        assert reconciled.state == "indeterminate"
        assert reconciled.to_dict()["terminal_outcome"] == "unknown"
    finally:
        if child is not None:
            kill9_and_reap(child)
        if db is not None:
            db.close()
