"""Contract tests for ares_observability_client.py (S1-07/S1-08 candidate evidence).

Run: python3 test_ares_observability_client.py  (exit 0 = all pass)
No collector required for these; transport behavior tested separately end-to-end.
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

from hermes_cli.observability.ares_observation_client import (AresObservationClient, MAX_FRAME_BYTES,
                                                             FRAME_HEADER_BYTES, MAX_PAYLOAD_BYTES)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{': ' + detail if detail else ''}")


def wire_server(sock_path, frames_read, stop, max_frames=4):
    """Tiny collector stand-in: reads length-delimited frames, records them."""
    os.path.exists(sock_path) and os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(4)
    srv.settimeout(0.2)

    def reader():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(2)
            with conn:
                buf = b""
                while len(frames_read) < max_frames:
                    while len(buf) < FRAME_HEADER_BYTES:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    ln = struct.unpack(">I", buf[:FRAME_HEADER_BYTES])[0]
                    while len(buf) < FRAME_HEADER_BYTES + ln:
                        chunk = conn.recv(65536)
                        if not chunk:
                            return
                        buf += chunk
                    frames_read.append(json.loads(buf[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + ln]))
                    buf = buf[FRAME_HEADER_BYTES + ln:]
            if len(frames_read) >= max_frames:
                return
        srv.close()

    def safe_reader():
        try:
            reader()
        except OSError:
            pass
    t = threading.Thread(target=safe_reader, daemon=True)
    t.start()
    return srv, t


# T-030-class: envelope validity + redaction BEFORE transport
def t_envelope_and_redaction(tmp):
    frames, stop = [], threading.Event()
    srv, reader = wire_server(f"{tmp}/s1.sock", frames, stop)
    c = AresObservationClient(f"{tmp}/s1.sock", producer_id="p1")
    s = c.emit("tool", "started", {"tool": "terminal",
                                   "content": "SECRETPROMPTBODY",
                                   "messages": [{"role": "user", "text": "x"}]},
               correlation={"session_id": "sess-1", "not_a_key": "v"})
    for _ in range(20):            # bounded wait for the reader to record the frame
        if frames:
            break
        time.sleep(0.05)
    stop.set()
    reader.join(timeout=2)
    srv.close()
    check("emit returns queued/accepted", s in ("queued", "accepted"), s)
    check("one frame on wire", len(frames) == 1, str(len(frames)))
    e = frames[0]
    check("schema_version 1", e["schema_version"] == 1)
    check("redaction applied pre-transport",
          "[REDACTED]" in json.dumps(e["payload"]), json.dumps(e["payload"])[:80])
    check("SECRETPROMPTBODY absent from frame", "SECRETPROMPTBODY" not in json.dumps(e))
    check("unknown correlation key dropped", "not_a_key" not in e["correlation"])
    check("known correlation preserved", e["correlation"].get("session_id") == "sess-1")
    check("privacy metadata present (PascalCase wire enum)",
          e["privacy"]["tier"] == "MetadataOnly"
          and e["privacy"]["redaction"] == "Redacted"
          and e["privacy"]["content_fields"] >= 2, json.dumps(e["privacy"]))
    check("producer_sequence monotonic bump", e["producer_sequence"] >= 1)
    c.close()


# T-031-class: typed rejections, no fallback
def t_typed_rejections(tmp):
    c = AresObservationClient(f"{tmp}/nonexistent.sock", producer_id="p2")
    for name, kwargs, expect in [
        ("unknown kind rejected", dict(kind="bogus_kind", status="started"), "unknown-kind"),
        ("unknown status rejected", dict(kind="tool", status="bogus"), "unknown-status"),
        ("oversized payload rejected (non-redacted key)", dict(kind="tool", status="started",
         payload={"n": "x" * (MAX_PAYLOAD_BYTES + 10)}), "payload-too-large"),
    ]:
        try:
            c.emit(provenance="canonical", **kwargs)
            check(name, False, "no error raised")
        except ValueError as ex:
            check(name, expect in str(ex), str(ex)[:60])
    st = c.stats()
    check("attempted identity after rejections",
          st["attempted"] == st["accepted"] + st["rejected"] + st["dropped"], json.dumps(st))
    check("rejections counted", st["rejected"] == 3, json.dumps(st))
    c.close()


# T-032-class: queue overflow drops + accounting under load
def t_queue_overflow(tmp):
    c = AresObservationClient(f"{tmp}/none2.sock", producer_id="p3", local_queue_cap=2)
    sent = sum(1 for _ in range(50)
               if c.emit("health", "health", {"n": _}) != "dropped")
    st = c.stats()
    check("overflow dropped counted", st["dropped"] > 0, json.dumps(st))
    check("accounting identity under load",
          st["attempted"] == st["accepted"] + st["rejected"] + st["dropped"], json.dumps(st))
    check("no hang on dead socket", True)  # reaching this line IS the test
    c.close()


def t_frame_encoding():
    c = AresObservationClient.__new__(AresObservationClient)  # no transport needed
    import uuid as _u
    from datetime import datetime, timezone
    c.producer_id, c.source_crate, c.adapter_id = "pt", "sc", "ad"
    c._seq, c._seq_lock = 0, threading.Lock()
    old = _u.uuid4
    _u.uuid4 = lambda: _u.UUID(int=0)
    try:
        env = c._build_envelope("health", "health", {}, None, None, "canonical")
    finally:
        _u.uuid4 = old
    frame = c._frame(env)
    hdr = struct.unpack(">I", frame[:4])[0]
    check("frame header = payload length", hdr == len(frame) - 4)
    check("frame within contract bound", len(frame) <= MAX_FRAME_BYTES)
    check("header width is 4 bytes", FRAME_HEADER_BYTES == 4)




# ---- pytest integration wrappers (also runnable directly: python3 <this file>) ----
def _run_checked(fn, tmp=None):
    """Run one scenario function; fail the pytest test if any check() recorded a failure."""
    fails_before = len(FAIL)
    if tmp is None:
        fn()
    else:
        fn(str(tmp))
    assert len(FAIL) == fails_before, "scenario failures: %s" % FAIL[fails_before:]


def test_scenario_envelope_and_redaction(tmp_path):
    _run_checked(t_envelope_and_redaction, tmp_path)


def test_scenario_typed_rejections(tmp_path):
    _run_checked(t_typed_rejections, tmp_path)


def test_scenario_queue_overflow(tmp_path):
    _run_checked(t_queue_overflow, tmp_path)


def test_scenario_frame_encoding():
    _run_checked(t_frame_encoding)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        t_envelope_and_redaction(tmp)
        t_typed_rejections(tmp)
        t_queue_overflow(tmp)
        t_frame_encoding()
    print(f"PASS: {len(PASS)}")
    for p in PASS:
        print("  ok:", p)
    if FAIL:
        print(f"FAIL: {len(FAIL)}")
        for f in FAIL:
            print("  xx:", f)
        sys.exit(1)
    print("ALL_CONTRACT_TESTS_OK")