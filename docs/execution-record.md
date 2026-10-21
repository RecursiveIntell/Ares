# Execution record

`ares_runtime/execution_record.py` owns the closed, versioned record for one
managed model attempt. `hermes_state_execution.py` owns its persistence in
`SessionDB.state_meta`. `plugins/dispatch_governor` commits the pre-send marker
and the terminal outcome. `scripts/verify_execution_record.py` is the
independent, stdlib-only verifier.

## Identity

`execution_id` is the existing attempt id (`<api_request_id>:<retry_count>`),
not a fresh UUID. Admission, materialization, physical send, and settlement
must all refer to that same id.

## States

```text
admitted -> materialized -> send_started -> settled
    |             |                  |
    +-> denied    +-> denied         +-> indeterminate
    +-> abandoned +-> abandoned
```

`settled`, `denied`, `abandoned`, and `indeterminate` are terminal and
immutable. `abandoned` means the enforcing governor proved the attempt never
reached the provider. `indeterminate` means the attempt may have been sent and
must never be auto-retried.

## Digests

- `logical_request_digest` is the pre-SDK projection (`ares-json-sorted-v1`).
- `wire_request_digest` is the exact bytes observed at the transport
  (`sha256`); the physical observation also retains the blake3 digest.
- `record_digest` is the canonical digest of the record with the digest field
  removed.

Never assume the logical and wire digests are equal. SDK-added defaults,
timeouts, and headers make them diverge.

## Governor modes

- `observe` (default): writes what it can, never denies, and does not relax
  recovery because absence of a marker is not proof that no send occurred.
- `enforce`: commits `send_started` before bytes; a failed commit raises
  `EgressDispatchDenied` and the provider callback never runs.

The plugin is config-gated under `dispatch_governor.mode`. The live profile is
not enabled by this change; activation is a separate operator gate.

## Verification

```bash
python -I scripts/verify_execution_record.py \
  --db "$HERMES_HOME/state.db" \
  --capture /path/to/capture.jsonl
```

Capture JSONL accepts `{"body_b64": "..."}` or `{"body": "..."}` per line.
The verifier reads `state_meta` directly and does not import producer modules.

## Retention

The record head is authoritative. Up to 32 event rows are retained per
execution; pruning removes only the event projection and never the head or a
terminal outcome. Use `scripts/prune_execution_records.py` for manual
maintenance. Do not delete a head to reclaim space; quarantine or archive it
instead.
