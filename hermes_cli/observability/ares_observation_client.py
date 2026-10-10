"""ares_observability_client.py — R0 candidate observation client (S1-07/S1-08).

Speaks the stack-monitor unix-socket protocol observed at Libraries HEAD 5ff3b414:
- 4-byte big-endian length-prefixed JSON envelope frames (ipc.rs FRAME_HEADER_BYTES=4)
- max frame 68192 bytes (MAX_PAYLOAD_BYTES 64KiB + 4096)
- envelope contract: stack-observation ObservationEnvelope v1 (schema_version=1)
- metadata-only default policy (S1-07.04): redaction happens BEFORE crossing the socket

Loss accounting (S1-08.02): attempted == accepted + rejected + dropped, exactly, on close().
Never blocks the caller; never invents correlation ids (S1-07.02): unknowns stay None.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 64 * 1024
FRAME_HEADER_BYTES = 4
MAX_FRAME_BYTES = MAX_PAYLOAD_BYTES + 4096

KINDS = ("llm_call", "token_progress", "retry", "parse", "transport",
         "graph_run", "graph_node", "contract", "tool", "memory",
         "embedding", "receipt", "health", "tracing_fallback")
STATUSES = ("started", "streaming", "completed", "failed", "cancelled", "retried", "health")
PROVENANCE = ("canonical", "adapted", "inferred", "duplicate")
CORRELATION_KEYS = ("session_id", "run_id", "trace_id", "span_id", "parent_span_id",
                    "node_id", "attempt_id", "trial_id", "request_id")

REDACT_KEYS = ("content", "prompt", "completion", "messages", "body", "text",
               "secret", "token_raw", "api_key", "password", "authorization")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AresObservationClient:
    """Bounded, non-blocking producer for the ares-observatory collector socket."""

    def __init__(self, socket_path: str, producer_id: str = "ares-candidate",
                 source_crate: str = "ares-runtime", adapter_id: str = "ares-observation-adapter",
                 local_queue_cap: int = 1024):
        self.socket_path = socket_path
        self.producer_id = producer_id
        self.source_crate = source_crate
        self.adapter_id = adapter_id
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._sock = None
        self._sock_lock = threading.Lock()
        self._cap = local_queue_cap
        self._pending: list[bytes] = []
        self._pending_lock = threading.Lock()
        self.closed = False
        # counters (S1-08.02 accounting identities)
        self.attempted = 0
        self.accepted = 0   # envelope valid + pushed toward socket
        self.sent = 0       # bytes actually written to the socket
        self.rejected = 0   # schema/limit violations
        self.dropped = 0    # queue overflow / socket unavailable after retry
        self.connection_failures = 0

    # ---- contract validation (S1-07.02/04, schema v1) ----
    @staticmethod
    def _redact(value):
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if any(r in k.lower() for r in REDACT_KEYS)
                        else AresObservationClient._redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [AresObservationClient._redact(v) for v in value]
        return value

    def _build_envelope(self, kind: str, status: str, payload: dict,
                        correlation: dict | None, timing: dict | None,
                        provenance: str) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown-kind:{kind}")   # typed, no fallback kind
        if status not in STATUSES:
            raise ValueError(f"unknown-status:{status}")
        if provenance not in PROVENANCE:
            raise ValueError(f"unknown-provenance:{provenance}")
        corr = {}
        if correlation:
            for k, v in correlation.items():
                if k in CORRELATION_KEYS and v is not None:
                    corr[k] = str(v)[:128]
        env = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "observed_at": _utcnow(),
            "producer_id": self.producer_id[:128],
            "process_id": os.getpid(),
            "source_crate": self.source_crate[:128],
            "adapter_id": self.adapter_id[:128],
            "provenance": provenance,
            "correlation": corr,
            "producer_sequence": self._next_seq(),
            "kind": kind,
            "status": status,
            "timing": dict(timing) if timing else {},
            "privacy": self._privacy_metadata(payload),
            "payload": self._redact(payload or {}),
        }
        return env

    @staticmethod
    def _count_content_fields(payload: dict) -> int:
        if not isinstance(payload, dict):
            return 0
        return sum(1 for k in payload if any(r in str(k).lower() for r in REDACT_KEYS))

    @staticmethod
    def _privacy_metadata(payload: dict) -> dict:
        """Match stack-observation serde contract EXACTLY (lib.rs:406-420):
        PrivacyTier and RedactionState have NO rename_all -> PascalCase wire values."""
        n = AresObservationClient._count_content_fields(payload)
        tier = "MetadataOnly"
        redaction = "Redacted" if n > 0 else "ContentDisabled"
        return {"tier": tier, "redaction": redaction, "content_fields": n}

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # ---- transport ----
    def _connect(self):
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.25)
            s.connect(self.socket_path)
            self._sock = s
        return self._sock

    def _frame(self, env: dict) -> bytes:
        payload = json.dumps(env, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload-too-large:{len(payload)}")
        return len(payload).to_bytes(FRAME_HEADER_BYTES, "big") + payload

    def _drain(self) -> int:
        sent = 0
        while self._pending:
            frame = self._pending[0]
            try:
                sock = self._connect()
                sock.sendall(frame)
            except (OSError, ValueError):
                self.connection_failures += 1
                break
            self.sent += 1
            sent += 1
            with self._pending_lock:
                self._pending.pop(0)
        return sent

    # ---- public API ----
    def emit(self, kind: str, status: str, payload: dict | None = None,
             correlation: dict | None = None, timing: dict | None = None,
             provenance: str = "adapted") -> str:
        """Validate, redact, and (best-effort) send ONE event. Non-blocking.

        Returns one of: 'accepted' (handed to a live socket), 'queued'
        (bounded local queue; will drain later), 'dropped' (rejected locally).
        Never raises on transport failure; raises ValueError on contract violations.
        """
        if self.closed:
            raise ValueError("client-closed")
        self.attempted += 1
        try:
            env = self._build_envelope(kind, status, payload or {}, correlation,
                                       timing, provenance)
            frame = self._frame(env)
        except ValueError:
            self.rejected += 1
            raise
        with self._pending_lock:
            if len(self._pending) >= self._cap:
                # Contract-invalid AND queue-dropped events each count exactly once:
                # attempted == accepted + rejected + dropped (identity must hold).
                self.dropped += 1
                return "dropped"
            self.accepted += 1
            self._pending.append(frame)
        self._drain()
        return "queued" if self._pending else "accepted"

    def stats(self) -> dict:
        return {"attempted": self.attempted, "accepted": self.accepted,
                "sent": self.sent, "rejected": self.rejected,
                "dropped": self.dropped, "queued": len(self._pending),
                "connection_failures": self.connection_failures}

    def close(self):
        """Drain what is drainable, then surface the final accounting identity.

        The caller (harness) asserts: attempted == accepted + rejected + dropped.
        """
        self._drain()
        self.closed = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        return self.stats()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()