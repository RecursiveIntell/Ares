"""The oracle must reject a tampered record and a tampered capture."""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from hermes_state import SessionDB

ROOT = Path(__file__).resolve().parents[3]
ORACLE = ROOT / "scripts" / "verify_execution_record.py"


def _seed(tmp_path):
    body = b'{"model":"local","messages":[{"role":"user","content":"hello"}]}'
    execution_id = "turn:api:1:0"
    materialization_ref = "ares-materialization:" + "a" * 64
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.set_meta(
            "context-dispatch:" + execution_id,
            json.dumps(
                {
                    "schema": "SessionDBContextDispatchV2",
                    "attempt_id": execution_id,
                    "payload_digest": "sha256:" + "b" * 64,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        db.set_meta(
            materialization_ref,
            json.dumps(
                {
                    "schema": "ares.dispatch-materialization/v1",
                    "context": {
                        "call_ref": "ares-call:" + execution_id,
                        "request_digest": "sha256:" + "b" * 64,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        db.create_execution_record(
            ExecutionRecordV1.create({
                "execution_id": execution_id,
                "session_ref": "session:test",
                "session_generation": 0,
                "turn_ref": "phase:1",
                "call_ref": "ares-call:" + execution_id,
                "admission_ref": "context-dispatch:" + execution_id,
            })
        )
        db.transition_execution_record(
            execution_id,
            ExecutionState.MATERIALIZED.value,
            materialization_ref=materialization_ref,
            logical_request_digest="sha256:" + "b" * 64,
        )
        db.transition_execution_record(
            execution_id,
            ExecutionState.SEND_STARTED.value,
            wire_request_digest="sha256:"
            + __import__("hashlib").sha256(body).hexdigest(),
            wire_bytes=len(body),
            governor_qualified=True,
            governor_mode="enforce",
        )
        db.transition_execution_record(
            execution_id,
            ExecutionState.SETTLED.value,
            terminal_outcome="ok",
            response_digest="sha256:" + "c" * 64,
            http_status=200,
        )
    finally:
        db.close()
    capture = tmp_path / "capture.jsonl"
    capture.write_text(
        json.dumps({"body_b64": base64.b64encode(body).decode("ascii")}) + "\n",
        encoding="utf-8",
    )
    return db_path, capture, execution_id, body


def _run(db_path, capture):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(ORACLE),
            "--db",
            str(db_path),
            "--capture",
            str(capture),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_oracle_accepts_a_complete_record(tmp_path):
    db_path, capture, _execution_id, _body = _seed(tmp_path)
    result = _run(db_path, capture)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["checked"] == 1


def test_oracle_rejects_a_tampered_record(tmp_path):
    db_path, capture, execution_id, _body = _seed(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (f"execution:{execution_id}",)
        ).fetchone()
        record = json.loads(row[0])
        record["wire_bytes"] = record["wire_bytes"] + 1
        conn.execute(
            "UPDATE state_meta SET value=? WHERE key=?",
            (
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                f"execution:{execution_id}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    result = _run(db_path, capture)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["failures"][0]["code"] == "RECORD_DIGEST_MISMATCH"


def test_oracle_rejects_a_tampered_capture(tmp_path):
    db_path, capture, _execution_id, _body = _seed(tmp_path)
    capture.write_text(
        json.dumps({"body_b64": base64.b64encode(b"different").decode("ascii")}) + "\n",
        encoding="utf-8",
    )
    result = _run(db_path, capture)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["failures"][0]["code"] == "WIRE_CAPTURE_MISMATCH"


def test_oracle_runs_isolated_without_producer_imports(tmp_path):
    db_path, capture, _execution_id, _body = _seed(tmp_path)
    result = _run(db_path, capture)
    assert "No module named" not in result.stderr
