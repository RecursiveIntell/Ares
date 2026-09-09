# Context Governor adapter closure — 2026-09-09

Status: implementation in progress; NOT merge-ready, installed, or activated.
Base: `fa74bfdb98e541fe719bb0f99f6bd44bce034f44`.
Core owner: RecursiveIntell/Libraries PR #17, `context-governor/`.

The hostile audit and core repair plan live in Libraries at
`context-governor/docs/closure-audit-20260909.md`; this document records only
Ares adapter ownership and integration gates, not another source of truth.

## Scope

- Negotiate a versioned CLI failure envelope and reject malformed/untyped
  certified failures rather than parsing their English text.
- Validate final budget fields before preparing a receipt. Preserve the
  core's declared counting metric; do not claim provider-exact token counts.
- Bound process timeout cleanup. Parent exit is not group death, and process
  death is not proof a prepare/activation mutation did not commit.
- Preserve operation-local governed descriptor ownership and the existing
  pending/admission/activation recovery owner. Do not invent another journal.
- Check actual descriptor-platform capabilities before promising Windows
  certified execution. Current Rust governed descriptors resolve through
  `/proc/self/fd`; a psutil tree walk cannot manufacture that capability.

## Forbidden changes

No edits to main, hermes-ares-recon, active config, live stores, keys, provider
routes, or the installed runtime. No merge, force-push, installation, activation,
secret export, receipt re-signing, widening to stock compression, or blind retry.
No new runtime/permission/receipt authority in this adapter.

## Acceptance

Use scripts/run_tests.sh (not direct pytest), existing hermetic fixtures,
platform-native process tests, the real pinned Rust governor for integration,
strict protocol-negative cases, timeout ambiguity cases, and current
Context Governor conformance tests. Preserve exact-head results including
fail/skip/not-run. Do not treat Ares's normal unit mocks as real binary proof.

Rollback before adoption is closing/reverting this branch. Future adoption
must pair the negotiated Rust binary and adapter; an old binary must be
rejected explicitly rather than silently falling back to text classification.
