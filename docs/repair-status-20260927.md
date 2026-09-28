# Session/control repair — historical preservation checkpoint

> Historical snapshot of the initial preservation increment (`1f9a35090e7`).
> For recovered later increments, the full remaining task/case denominator, and
> current proof boundaries, see [the 2026-09-28 recovery checkpoint](repair-recovery-20260928.md).
> The requirements below remain historical evidence, not the current progress projection.

This branch preserves an in-progress repair, not a merge-ready or activated UI release.
It retains the source baseline `ad547c6c4a31adf9a848d264734d2ec3cea90651`,
including its model/provider guards, and integrates existing Stop/request-owner
work with controller corrections rather than replacing shared files wholesale.

## Included source

- Physical compute-host request identity, exact terminal settlement and bounded
  control transport; shared run ownership remains with the compute host.
- Targeted Stop, canonical accepted-input cancellation receipts, goal follow-up
  precedence and terminal presentation dependencies.
- Adopted-host display hydration: schedule its producer, bind the worker to the
  exact record, close profile handles once, and avoid constructing a parent
  agent or revoking host controls when display-history reading fails.
- Partial frontend model/effort/preset intent fences. These do not yet provide
  the full confirmed/pending/unknown outcome contract.
- Behavioral regression tests, including deliberately retained witnesses for
  unfinished settings defects. A negative witness is not a passing check.

## Local verification boundary

The controller reran 798 selected backend tests across 22 files after the
hydration changes; all passed. A separate hydration/DB ownership selection
passed 13 tests. The earlier hydration attempt failed two assertions because
its mock aliased independently opened database handles; the corrected fixture
asserts each separately opened handle closes exactly once.

Earlier frontend evidence reported 448 passing tests on the narrower frontend
increment. That historical pass does not certify this entire combined branch.
Full combined UI tests, headed acceptance, packaging, hosted CI, data-compatible
rollback and live activation remain open. No merge-readiness claim is made.

## C40 — model options timeout (open)

Reported symptom: `Model option update failed, request timed out after 30s: config.set`.
Requested result: Astra with extra-high reasoning for the selected session,
not a profile-default rewrite. The actual executing effort is not yet verified.

Required remaining work:

- Route option writes and readback to the compute-host owner.
- Keep desired, pending, applied, executing, rejected, unknown and partial
  outcomes distinct. Timeout is not proof of rejection or non-application.
- Reconcile unknown delivery without blind resend or cosmetic rollback.
- Roll back overlapping rejected choices to owner-confirmed state, not an
  earlier optimistic choice; preserve all existing late-callback fences.
- Prove same-connection responsiveness and owner/provider readback rather than
  increasing the timeout alone.

## C41 — compression exhaustion without continuation (open)

The current source has two different transitions:

1. The Governor adapter requests Rust-owned `compact-continue-v2` only after
   authenticated `lineage_generation_limit`. It must not use that operation to
   bypass `lineage_integrity_mismatch` or arbitrary compression errors.
2. Physical SessionDB working-context rebase is gated by
   `compression.context_rebase_enabled`, defaulting false, and route/owner
   admission. A failed compaction does not unconditionally create a successor.

Source seams: `plugins/context_engine/_context_governor/__init__.py`,
`agent/agent_init.py`, `agent/turn_context.py`, `agent/conversation_loop.py`,
`ares_runtime/continuity/runtime.py`, and `hermes_state_continuity.py`.

The new symptom is not yet tied to an exact live error/receipt. Prior incident
evidence distinguished lineage mismatch from a receipt generation ceiling;
that distinction must be rechecked, not assumed for this occurrence.

Acceptance requires a copied-state reproduction, no oversized provider dispatch
following exhaustion, qualified opaque/Responses replay, and a durable
parent-to-child transition preserving authentic input, goal budget and unresolved
effects. Prove a second transition and restart/readback. Do not enable the flag,
raise a generation ceiling, delete receipts or fabricate parentage to mask failure.

## Remaining integration scope

Host-owned options and compound outcomes; adoption/observer lifetime; full
history/attachment/execution readiness; Stop data-format rollback; accepted-input
recovery UI; Clarify answer delivery; combined regression/headed/platform gates.
All remain explicit gates, even where prerequisite source is already included.

Historical and fresh private receipts remain outside Git. No live database,
credential file, profile configuration, generated cache or private transcript is
included in this source checkpoint.
