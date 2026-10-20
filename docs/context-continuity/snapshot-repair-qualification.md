# Context continuity qualification

PR #80 is an implementation branch, not a qualified release. The V4 packet
dated 2026-09-23 remains the acceptance contract. This report records the
2026-09-25 repair against baseline
`e25246701ad00338d51961b35be87e2fde690b45`.

## Verified repair

SessionDB now reads a continuation source under one SQLite snapshot. A
savepoint preserves a caller's outer transaction and leaves pooled readers
clean on both success and refusal. Compilation binds the resulting observation
set to a digest. Publication reconstructs and compares that set under its
existing `BEGIN IMMEDIATE` transaction before creating a child, closing the
parent, or rebinding local owners. A message watermark alone did not detect
changes to goals, heartbeats, loops, user text, effect state, or workspace.

Automatic NORMAL continuation refuses unresolved effects. Turn-start, pre-API,
and provider-overflow callers stop on BLOCKED outcomes. They cannot fall
through to another provider/tool admission. Unadmitted request reservations are
refunded; already attempted provider requests remain charged. Early refusal
saves the latest authentic input through the existing persistence owner.

The independent review found the early-input persistence regression during this
repair. The final version includes two real-store regression cases for it.

## Evidence and reproduction

The unchanged baseline passed the original 20-file gate (946 tests). Added
regressions reproduced eight snapshot/publication failures and eight dispatcher
failures on that baseline. The corrected snapshot fixture, rather than its
initial missing-anchor probe, is the RED witness.

The final test selection is maintained in
`.github/workflows/context-continuity-qualification.yml`. Reproduce its install
and test steps with Python 3.13, frozen `dev` and `anthropic` extras, four workers,
and zero per-file retries. Use `scripts/run_tests.sh`; do not bypass its per-file
isolation. The Anthropic extra is necessary for an existing caller test that
imports the optional SDK. The missing-extra failure also reproduced on the
unchanged baseline.

The final pinned gate passed **1,289 tests in 26 files, zero failures**. Its
raw output is retained in `repair-test-run.log`. A prior run was invalidated
because an unpinned lint invocation recreated the Python environment during
testing; that run contributes no qualification evidence.

Machine-readable results and source hashes are in `repair-evidence.json` and
`route-receipt.json`. An independent, read-only review ran five relevant files
(74 passing tests), then reran the final dispatch suite (12 passing tests).
Its verdict is limited to the default automatic route.

## Compatibility boundaries

- Automatic compilation/publication uses the default snapshot limits. A direct
  `build_live_candidate(recent_limit != 12)` call can produce an unchanged
  candidate that publication refuses. Configurable publication is not qualified;
  supporting it requires binding and validating the read limits.
- New publications require the snapshot digest. Replay of transitions created
  before that binding was introduced has not been migration-qualified. Existing
  pending transitions must remain stopped until reconciled; do not reopen a
  parent or delete committed lineage to make a retry succeed.
- This pass did not change schemas, activate a live profile, invoke paid
  providers, certify native Rust owners, or qualify external-service effects.
  Before activation, rollback is a source revert of this repair. A source
  rollback is not a protocol for repairing live committed state.

## Remaining V4 gates

`acceptance-crosswalk.json` preserves all 108 continuity case IDs, required
stages and evidence classes, plus all 172 inherited program IDs. Its test-file
references are supporting locations, not complete acceptance oracles. No case
is promoted to whole-contract qualification by this inventory.

The next implementation pass must close the actual owner protocol: native
cross-owner fences and acknowledgments, crash/restart reconciliation, and
request-bound custody/admission at the final dispatch boundary. It must also
verify current-policy retrieval and the installed capability tuple against the
case-specific oracles. Selected-route calibration, canary readiness, authorized
live canaries, and endurance evidence remain separate mandatory gates.

These are source and qualification gaps, not merely a request to run the current
unit suite again. Do not merge, activate, or describe the V4 program as complete
on the strength of this report.
