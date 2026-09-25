# Context continuity qualification

The V4 packet dated 2026-09-23 remains the full acceptance contract. This is
an implementation branch. Qualification is tied to source hashes and named
oracles; passing unit tests does not qualify a selected provider route.

## Retained recovery custody and external receipt reconciliation

The native SessionDB owner now renews an expired retained handle only for the
same process and private token, under the actual root lease and existing bounded
recovery reservation. Checkpoint files are re-observed; generation, controls and
deadline are checked again inside admission. V1 history and V2 task bindings are
preserved. Generic expired refresh and dead-owner takeover remain unchanged.
Lost renewal ACKs remain unknown and cannot trigger an automatic second mutation.

Ares retains the consume-time binding and adapter for outcome settlement. A lost
outcome ACK permits one exact native readback. It never reissues a permit or
redispatches an effect. Response validation now uses the actual native reported
outcome shape, including ambiguous reports. Reported success still does not mean
independently confirmed external success or task completion.

The native prerequisite is preserved as an exact patch against recursive-agent
`11fefae`, with candidate tree and dependency identities in
`native-external-owner.json`. It adds semantic outcome idempotency, closed exact
readback IPC, and trusted-clock checks under the permit lock. Local compilation
and Clippy passed. Native runtime execution is blocked by the local systemd user
bus; the PR runs the native repository's existing supported hosted paired-root
contract against that exact patched tree. The local test guard is unchanged.
The patch is not a published native release or an installed capability.

The frozen local Ares gate passed 2,865 tests across 89 files; independent review
passed 72 and closed AUD-H1. Five existing skips remain. Unix sockets are denied
in this workspace, so the local gate excludes five named collaboration socket
tests and the code-execution file; hosted CI retains them. The sole full-CI
failure on `dbbc704a`, an emergency-stop fake event missing native event fields,
is reproduced and fixed with the real MessageEvent. Evidence and raw logs are
in `owner-recovery-evidence.json` and its linked artifacts.

EF-1/EF-2 native generation, incarnation, current-policy and retirement fencing
remain open. This increment does not complete or qualify full V4.

## Native cold input recovery and response lifetime

Accepted gateway input now carries immutable actor, route and transport-owner
provenance in the same native transaction. The existing startup/reconnect and
periodic controller can discover it without a new message. Current authorization
covers every actor in the drained batch, including the request, response, buffered
delivery and tool boundaries. Restored role/upstream trust flags are never used.

The native input phase distinguishes projection, admitted execution and a
successfully persisted response. Construction/recovery attempts retain fixed
budgets across lease changes and new arrivals. A living controller cannot be
replaced merely because its lease expired. Projected input with no admitted
request can recover without another authentic transcript occurrence; admitted
unfinished execution remains parked. Observed failed calls retain spent request
records even when their buffered response bytes are discarded.

Normal final text and its native response receipt commit together. Completion
binds the last admitted request and a later, exact active assistant tail; lost
publication acknowledgements use readback. A response receipt does not assert
that the user's task or its external effects are complete. A completed phase
also does not block separately owned synthetic work.

The frozen local gate passed 2,765 tests across 86 files with five existing skips;
independent review passed 136 tests. See `cold-input-evidence.json`, the source
hash manifest, raw logs and audit verdict. The five hosted quick-command fixture
failures on `120baddb` are reproduced and fixed using real events. Package replay
includes the new native module. Focused qualification, Nix and Docker passed on
that base; candidate hosted checks follow publication.

This qualifies selected stateless text recovery only. Missing legacy provenance,
tokenless cold transport identity and pending control ownership remain parked.
Selected role/upstream-authorized routes refuse before enrollment until current
actor verification is available. Other ingress, owner-qualified effect/task
settlement, all shared parent budgets, workspace/current-policy ownership and
installed subscription-route canary/endurance remain required for full V4.

## Selected gateway ingress

The real BaseAdapter now awaits runner-owned native input acceptance before
ordinary text is queued or spawned. Existing transport authorization and profile
routing select the owner; stable transport IDs deduplicate redelivery without
binding unstable receive-time defaults. Busy text retains distinct FIFO entries.
Plugin rewrites and API enrichment preserve the original human input separately.
The actual agent rereads the exact receipt against its current native root.
Session reset drift refuses before turn preparation, and an agent construction
failure leaves pending input without fabricating a second transcript occurrence.

The final pinned local gate passed 2,720 tests across 83 files with five existing
skips; independent review passed 114 tests. See `gateway-input-evidence.json`.
This also repairs all 20 reproduced failures from full CI on `20728d3`: exact
nonpositive integer row coordinates remain provisional, and watermark tests now
own fresh summary dictionaries for their separate databases. Positive and
malformed locators remain strict. Focused qualification, Nix and Docker passed
on that published base; candidate hosted checks follow publication.

This scope is stateless text through BaseAdapter. Active pending control replies,
attachments and non-BaseAdapter transports remain unqualified. Proxy delivery
refuses a typed native receipt. Invocation-only IDs do not promise redelivery
deduplication. The later native cold-input increment above adds bounded wake,
whole-batch actor checks and response lifetime. Task completion and effect
settlement remain separate; unknown outcomes cannot authorize replay.

## Obligation lifetime and input provenance

Released checkpoint obligations remain in the canonical conversation snapshot,
including unresolved outcomes, restrictions and findings. They do not become new
custody authority. Compacted authentic task anchors preserve their original V2
binding digest; edited, revoked, synthetic and derived anchors refuse admission.

Native copy/projection receipts preserve occurrence identity through compaction
and context rebase. Equal text and timestamps do not conflate distinct inputs.
Derived projections cannot become authentic instructions or task authority.
First-time unflushed input and unknown tool results remain durable occurrences.
Receipt-bound compaction atomically preserves clean input and its API sidecar
under the existing turn lease, including real agent preflight paths.

The pinned local gate passed 2,334 tests across 65 files with four existing skips.
Independent review passed 176 tests and closed AUD-P1/P2 and the profile/default/
occurrence findings. See `obligations-evidence.json` and the associated raw logs
and audit verdict. All hosted checks on the preceding `bc889de` passed, including
full CI, focused qualification, Nix and Docker. Candidate checks follow publication.

The 105-edge native materializer witness proves bounded occurrence provenance,
not automatic runtime endurance. Legacy rows without provenance are not inferred
to be copies. Gateway ingress, autonomous wake, completion/effect ownership and
full selected-route qualification remain required.

## Already-READY controller recovery

An already-READY successor now reconstructs process-owned custody and adopts its
canonical history before an ordinary turn. Its bounded controller phase is
separate from completed publication recovery; unfinished attempts retain the
same deadline across lease changes. Final native admission binds the current
phase, process, lease, controls and complete custody inventory. Lost completion
acknowledgments use exact readback. Cleanup retains custody until local adoption
has succeeded, including failed adoption after durable READY.

One authentic input accepted after a stop may reconstruct custody before its
transcript projection; provider admission still requires projection and the
ordinary stop fence. Pre-stop queued input cannot resume execution. Real turn
and separate-process regressions cover both paths and assistant-tail history.

The pinned local gate passed 2,002 tests across 60 files with four existing skips;
independent review passed 74 and closed AUD-RC1. See
`ready-controller-evidence.json`. The hosted workflow retains the Unix-socket
suite excluded locally. It also covers repairs to two failures discovered on
`f8a87ad`: fault-injector keyword forwarding and disabled-continuity TUI scope.
Nix and Docker passed on that published base.

This closes local READY reconstruction, not autonomous wake scheduling or task
completion. Released obligations and compacted task anchors are covered by the later
increment above; the remaining full V4 qualification still applies.

## Durable input and selected TUI ingress

The existing SessionDB owner now accepts immutable, root-sequenced authentic
input before the executing turn lease. Stable transport IDs deduplicate exact
redelivery and reject changed payloads. Ordered transcript projection requires
the current root lease and live continuation tip, and commits its receipt in the
same transaction. New accepted input invalidates stale dispatch and tool controls
before its transcript row exists. Versioned stop controls bind the accepted-input
sequence, so projecting a pre-stop queue cannot resume work.

The real agent loop and selected TUI ingress use this owner. TUI queues preserve
distinct submissions and forward identity through compute-host dispatch. Deferred
sessions resolve their own profile database and configuration. Clean input stays
exact while API-only notes and file-reference expansion retain their sidecar.
A parked successor projects arriving input once across retry and recovery.

The local canonical gate passed 1,020 tests across 42 files with the pinned
Governor; independent audit passed 24 tests and closed AUD-I1/I2/I3. See
`input-evidence.json`, `input-test-run.log`, and `input-audit-verdict.json`.
All hosted baseline checks on `cc06a62` passed. Candidate checks follow publication.
The attempted generic wheel build was refused by the repository's intentional
packaging guard; no bypass was used. Supported packaging remains editable install,
Docker and Nix.

Gateway ingress, autonomous cold inbox draining, task completion ownership, native
external fences and full selected-route qualification remain separate required
V4 work. Projection is not completion, and a duplicate already-projected event
cannot silently start a second execution. Generated IDs cover only an invocation;
cross-delivery deduplication requires the transport's stable ID.

## Pending-child cold custody recovery

File-bound checked claims now persist versioned recovery locators in the same
SessionDB transaction. They contain paths bound to exact checkpoint/history
identities, never a replayable request or old lease. Recovery independently
reads the files and proves the previous process identity is gone; expiry alone
cannot transfer ownership. Native admission repeats those checks, including
the current lease, controls and bounded recovery reservation/deadline.

The existing runtime recovery path can reconstruct missing private handles
without creating a second child. A partial group remains pending through normal
turn cleanup, a new lease and controller exit. Unknown mutation/readback
outcomes remain quarantined. READY checks exact custody and controls inside the
native transaction. Explicit release retains its previous meaning. Stale locator
bindings require explicit checked renewal after checkpoint/source advancement.

The exact local gate passed 536 tests across 30 files. Independent post-audit
passed 32 tests and closed AUD-C1. The 24 cold-recovery cases include separate
publisher/recovery processes, immutable V1 history, current V2 bindings, partial
cleanup/restart, deadline races, READY races and lost acknowledgements.
`cold-custody-evidence.json` binds source and raw logs. The existing host still
needs durable wake scheduling and recovery integration for already-READY cold
starts; this gate covers committed pending children, not the entire controller.

Hosted CI on `ab7c2af` exposed a goal-command test fixture without a canonical
session row. The fixture now creates the isolated durable session corresponding
to its simulated completed turn. All 12 goal-command tests pass locally and the
file is included in the focused hosted gate.

## Ordinary-task custody repair

`SessionDBRunCustodyV2` binds an ordinary task to a genuine, active SessionDB
user row and canonical conversation/profile lineage. Its versioned compact
refresh envelope is separate from V1; V1 historical-goal requirements remain
strict. The checked claim revalidates current controls and the live root lease
inside native write admission. The file client, active gateway RPC, turn owner
and read-only context inspector accept the explicit V2 contract.

Independent review closed cross-profile anchors, tool-child root confusion and
invalid session transfers. A real child-process claim, confirmed process exit
and checked takeover prove fresh generation/token fencing and exact checkpoint
retention. Live owners, synthetic/deactivated/edited anchors, stale controls,
mixed schemas and lost acknowledgements refuse without silent reclaim.

The local gate passed 500 tests across 28 files, including the real pinned
Governor. Independent post-audit passed 155 tests across six files.
`task-custody-evidence.json` records exact hashes and boundaries. This explicit checked-takeover gate is supplemented by the pending-child
recovery proof above; broader controller integration remains separate.

The preceding `050ddf9` hosted qualification passed 1,630 tests in 44 files,
with five existing skips, including all 44 execute-code tests. This closes the
preceding local socket-environment validation gap.

## Current control and progress repair

The follow-up from `9f5d4a310408f77cd41ad7d64cd7c4773a640e45` rechecks
a settled response's authentic input, stop, goal/recurring controls and lease
at tool execution and again at the registry after approval and middleware.
The native permit now consumes the final rewritten arguments. These checks
are local prerequisites; the required native cross-owner transition fence is
still unqualified.

Disabling new rebases preserves dispatch seals, streaming retry restrictions
and durable cancellation for committed children and compression descendants.
Continuity keeps authentic user occurrences distinct until provider projection.
A small provider prompt no longer resets the conversation no-progress counter.
Explicit `/goal resume` uses one native goal/episode CAS transaction and a
one-use action receipt. Lost acknowledgements resolve through owner readback;
unresolved readback disables stale manager writes. Missing owners cannot defer
half of a resume.

The exact local gate passed 1,587 tests in 43 files, with four existing skips.
All three paired native Governor tests ran. Independent review closed the
three initial findings and two follow-up acknowledgement/fallback findings.
`control-progress-evidence.json` binds source hashes and raw logs. The complete
Unix-socket `execute_code` suite is added to hosted qualification: this local
workspace denies socket creation, so its 17 affected cases are not reported
as passes. The pinned native Governor's separate release generation-32 test
passed and is retained in `native-governor-release-lineage.log`.

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
