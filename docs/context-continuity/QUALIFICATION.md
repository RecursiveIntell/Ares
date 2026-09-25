# Context continuity qualification

The V4 packet dated 2026-09-23 remains the full acceptance contract. This is
an implementation branch. Qualification is tied to source hashes and named
oracles; passing unit tests does not qualify a selected provider route.

## Durable recovery and dispatch repair

The repair from `68ec16bcb70a56fd6d7da54897b7aa38b0aa5091` makes child
publication, canonical goal/heartbeat/loop migration, the complete native
custody transfer set, and a bounded recovery intent one SessionDB transaction.
Owner conflicts roll back the whole publication. Lost acknowledgments reconcile
the committed child without publishing another child. READY requires the live
turn lease and current, bounded recovery reservation. Engine lifecycle failures
are propagated; the real Governor pending-receipt inventory must acknowledge
its lineage before activation.

Requests bind durable source before materialization, including ordered human
instruction occurrences. Final admission checks the unchanged route, exact
materialization, output reserve, conservative text bound, current lease,
stop/control state and immutable attempt identity. Unqualified middleware model,
body and SDK merge overrides stop before provider egress. Incoming corrections
invalidate the source seal. An unavailable durable stop cannot prevent local
socket/tool/child cancellation and leaves an explicit unresolved stop flag.

Response settlement quarantines superseded attempts. Streaming continues at the
transport for cancellation and health checks, but text, reasoning, completed
Codex commentary, display/TTS and plugin events are buffered until settlement.
Failed partial streams discard their buffer and return to the qualified outer
retry boundary. Internal stream retries are disabled for the continuity route.
Observer errors remain isolated from provider errors.

## Reproduction

`.github/workflows/context-continuity-qualification.yml` is the executable gate.
It uses Python 3.13, frozen `dev` and `anthropic` extras, the canonical isolated
per-file runner, four workers and zero per-file retries. It builds and tests
Libraries `0b099ec416de60f6adafb182f1d1e83795d05c56` with Rust 1.94.0, then
supplies that exact binary through `HERMES_TEST_CONTEXT_GOVERNOR_BIN`.

The paired native tests exercise real descriptor/key transport and disposable
stores: a pending receipt blocks acknowledgment, activated source bytes remain
recoverable after rebase, and missing keys refuse acknowledgment. No configured
provider or live profile is used by these tests. The native owner's own suite
passed 198 tests with one ignored test. The integrated Python gate passed 1,471 tests across 36 files, with four
existing skips. All three paired native tests executed. Independent follow-up
review resolved eleven findings across the repair. Evidence, logs and source
hashes are recorded in `recovery-dispatch-evidence.json`.

The prior snapshot repair remains documented in
`snapshot-repair-qualification.md`, `repair-evidence.json` and
`repair-test-run.log`. Those are historical evidence, not the current source
identity. Nondefault snapshot read limits are now bound into candidate identity
and the publication readback.

## Remaining contract gates

The 108 CT IDs and 172 inherited IDs remain in `acceptance-crosswalk.json`.
A whole V4 stage is not promoted by this repair. Required remaining work includes
external/native effect-owner transition acknowledgments, a root-scoped queued
input contract, autonomous recovery wakeups through the existing controller,
goal-less checked task binding, recovery after loss of process-owned custody
handles, source/worktree and current-policy retrieval qualification, objective
progress and shared recovery-resource accounting, reader/backup compatibility,
and selected-route behavioral/calibration/canary/endurance evidence.

Unsupported or ambiguous state remains a typed refusal. Earlier transitions
without the new recovery intent are not silently promoted. A code rollback is
not a live-state repair protocol: do not reopen a committed parent, remove
lineage, replace owner tokens, or replay uncertain effects.
