"""The record must keep the logical and wire request digests distinct."""

from __future__ import annotations

import hashlib

from ares_runtime.execution_record import ExecutionRecordV1, ExecutionState
from ares_runtime.continuity.budget import final_request_upper_bound
from ares_runtime.managed_calls import logical_request_bytes


def _record(logical_digest: str, wire_digest: str):
    return (
        ExecutionRecordV1
        .create({
            "execution_id": "turn:api:1:0",
            "session_ref": "session:test",
            "session_generation": 0,
            "turn_ref": "phase:1",
            "call_ref": "ares-call:turn:api:1:0",
            "admission_ref": "context-dispatch:turn:api:1:0",
        })
        .transition(
            ExecutionState.MATERIALIZED.value,
            materialization_ref="ares-materialization:" + "a" * 64,
            logical_request_digest=logical_digest,
        )
        .transition(
            ExecutionState.SEND_STARTED.value,
            wire_request_digest=wire_digest,
            wire_bytes=128,
            governor_qualified=True,
            governor_mode="enforce",
        )
    )


def test_timeout_and_headers_change_the_admission_projection():
    base = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    for suffix in ({"timeout": 30}, {"extra_headers": {"x": "y"}}):
        payload = {**base, **suffix}
        admission = final_request_upper_bound(
            route_ref="materialization", payload=payload
        ).payload_digest
        logical = "sha256:" + hashlib.sha256(logical_request_bytes(payload)).hexdigest()
        assert admission != logical


def test_bare_payload_projection_matches_logical_bytes():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    admission = final_request_upper_bound(
        route_ref="materialization", payload=payload
    ).payload_digest
    logical = "sha256:" + hashlib.sha256(logical_request_bytes(payload)).hexdigest()
    assert admission == logical


def test_record_accepts_distinct_logical_and_blake3_wire_digests():
    logical = "sha256:" + "b" * 64
    wire = "blake3:" + "c" * 64
    record = _record(logical, wire)
    value = record.to_dict()
    assert value["logical_request_digest"] == logical
    assert value["wire_request_digest"] == wire
    assert value["logical_request_digest"] != value["wire_request_digest"]
    assert value["state"] == "send_started"


def test_record_accepts_matching_projection_when_sdk_adds_nothing():
    digest = "sha256:" + "d" * 64
    record = _record(digest, digest)
    assert (
        record.to_dict()["logical_request_digest"]
        == record.to_dict()["wire_request_digest"]
    )
