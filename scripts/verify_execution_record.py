#!/usr/bin/env python3
"""Independent verifier for Ares execution records.

This script deliberately imports only the Python standard library.  It reads
``state_meta`` directly and reimplements the canonical digest and the state
checks instead of importing ``ares_runtime`` or ``hermes_state``.  A producer
self-check is not an independent oracle.

Capture format (JSONL, one request per line):
    {"body_b64": "<base64 of the exact wire body>"}
or
    {"body": "<utf-8 wire body>"}
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

TERMINAL = {"settled", "denied", "abandoned", "indeterminate"}
STATES = {"admitted", "materialized", "send_started"} | TERMINAL
OUTCOMES = {
    "ok",
    "provider_error",
    "interrupted",
    "discarded",
    "denied",
    "never_sent",
    "unknown",
}
GOVERNOR_MODES = {"observe", "enforce", "unavailable"}


class Failure(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code + (": " + detail if detail else ""))


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def record_digest(value: dict[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "record_digest"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise Failure(code, detail)


def verify_record(
    execution_id: str, record: dict[str, Any], meta: dict[str, str]
) -> dict[str, Any]:
    _require(
        record.get("schema_version") == "ares.execution-record/v1", "UNSUPPORTED_SCHEMA"
    )
    _require(record.get("execution_id") == execution_id, "EXECUTION_ID_MISMATCH")
    _require(
        record.get("record_digest") == record_digest(record), "RECORD_DIGEST_MISMATCH"
    )
    state = record.get("state")
    _require(state in STATES, "INVALID_STATE", str(state))
    _require(
        isinstance(record.get("sequence"), int) and record["sequence"] >= 1,
        "INVALID_SEQUENCE",
    )

    if state == "materialized":
        _require(
            record.get("materialization_ref") is not None, "MATERIALIZATION_REF_MISSING"
        )
        _require(
            record.get("logical_request_digest") is not None, "LOGICAL_DIGEST_MISSING"
        )
    if state == "send_started":
        _require(record.get("wire_request_digest") is not None, "WIRE_DIGEST_MISSING")
        _require(isinstance(record.get("wire_bytes"), int), "WIRE_BYTES_MISSING")
        _require(record.get("governor_qualified") is True, "GOVERNOR_NOT_QUALIFIED")
        _require(
            record.get("governor_mode") in {"observe", "enforce"},
            "INVALID_GOVERNOR_MODE",
        )
    if state in TERMINAL:
        _require(record.get("terminal_outcome") in OUTCOMES, "TERMINAL_OUTCOME_MISSING")
    if state == "settled":
        _require(
            record.get("response_digest") is not None
            or isinstance(record.get("http_status"), int),
            "SETTLEMENT_EVIDENCE_MISSING",
        )

    admission_ref = record.get("admission_ref")
    if not isinstance(admission_ref, str) or admission_ref not in meta:
        raise Failure("ADMISSION_REF_MISSING")
    admission = json.loads(meta[admission_ref])
    _require(
        admission.get("schema") == "SessionDBContextDispatchV2",
        "ADMISSION_SCHEMA_MISMATCH",
    )
    _require(admission.get("attempt_id") == execution_id, "ADMISSION_ID_MISMATCH")

    materialization_ref = record.get("materialization_ref")
    if materialization_ref is not None:
        _require(materialization_ref in meta, "MATERIALIZATION_REF_MISSING")
        materialization = json.loads(meta[materialization_ref])
        _require(
            materialization.get("schema") == "ares.dispatch-materialization/v1",
            "MATERIALIZATION_SCHEMA_MISMATCH",
        )
        context = materialization.get("context") or {}
        _require(
            context.get("request_digest") == record.get("logical_request_digest"),
            "MATERIALIZATION_DIGEST_MISMATCH",
        )
        _require(
            context.get("call_ref") == record.get("call_ref"),
            "MATERIALIZATION_CALL_REF_MISMATCH",
        )

    return record


def verify_events(
    execution_id: str, record: dict[str, Any], rows: Iterable[tuple[str, str]]
) -> None:
    events = []
    for key, value in rows:
        suffix = key.rsplit(":event:", 1)[-1]
        _require(suffix.isdigit(), "INVALID_EVENT_KEY", key)
        events.append((int(suffix), json.loads(value)))
    if not events:
        return
    events.sort(key=lambda item: item[0])
    last_sequence, last = events[-1]
    _require(last_sequence == record["sequence"], "EVENT_HEAD_SEQUENCE_MISMATCH")
    _require(
        last.get("record_digest") == record["record_digest"],
        "EVENT_HEAD_DIGEST_MISMATCH",
    )
    for number, (sequence, _value) in enumerate(events, start=1):
        _require(sequence == number, "EVENT_SEQUENCE_GAP", str(sequence))


def verify_capture(record: dict[str, Any], capture_path: Path | None) -> None:
    if capture_path is None:
        return
    wanted = record.get("wire_request_digest")
    if wanted is None:
        return
    algorithm, _, expected = wanted.partition(":")
    _require(algorithm == "sha256", "UNSUPPORTED_WIRE_ALGORITHM", algorithm)
    found = False
    for line in capture_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "body_b64" in item:
            body = base64.b64decode(item["body_b64"], validate=True)
        else:
            body = str(item.get("body", "")).encode("utf-8")
        if hashlib.sha256(body).hexdigest() == expected:
            found = True
    _require(found, "WIRE_CAPTURE_MISMATCH")


def verify_db(db_path: Path, capture_path: Path | None) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key,value FROM state_meta WHERE key LIKE 'execution:%' AND key NOT LIKE '%:event:%'"
            )
        }
        failures: list[dict[str, str]] = []
        checked = 0
        for key, raw in sorted(meta.items()):
            execution_id = key[len("execution:") :]
            try:
                record = json.loads(raw)
                verify_record(
                    execution_id,
                    record,
                    {
                        row["key"]: row["value"]
                        for row in conn.execute(
                            "SELECT key,value FROM state_meta WHERE key IN (?,?)",
                            (
                                record.get("admission_ref", ""),
                                record.get("materialization_ref", ""),
                            ),
                        )
                    },
                )
                event_rows = [
                    (row["key"], row["value"])
                    for row in conn.execute(
                        "SELECT key,value FROM state_meta WHERE key LIKE ? ORDER BY key",
                        (f"execution:{execution_id}:event:%",),
                    )
                ]
                verify_events(execution_id, record, event_rows)
                verify_capture(record, capture_path)
                checked += 1
            except (Failure, ValueError, TypeError, KeyError) as exc:
                failures.append({
                    "execution_id": execution_id,
                    "code": getattr(exc, "code", type(exc).__name__),
                    "detail": getattr(exc, "detail", str(exc)),
                })
        return {
            "schema": "ares.execution-record-verification/v1",
            "db": str(db_path),
            "capture": str(capture_path) if capture_path else None,
            "checked": checked,
            "failures": failures,
            "ok": not failures and checked > 0,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    assert "ares_runtime" not in sys.modules
    assert "hermes_state" not in sys.modules
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_db(args.db, args.capture)
    except (OSError, sqlite3.Error) as exc:
        report = {
            "schema": "ares.execution-record-verification/v1",
            "db": str(args.db),
            "ok": False,
            "checked": 0,
            "failures": [
                {"execution_id": None, "code": "DB_UNAVAILABLE", "detail": str(exc)}
            ],
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
