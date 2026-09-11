# Context Governor runtime closure — 2026-09-09

Status: implementation in progress; isolated branch only.
Base: `5bf10e831b1106bd92669eb80abe8802743f8ab5`.
Companion core owner: `RecursiveIntell/Libraries#17`.

This branch supersedes the stale implementation base in Ares #40 while preserving its hostile-audit intent. It must not modify `hermes-ares-recon`, main, live state, keys, provider routes, or installed runtime state.

## Required repairs

1. Certified subprocess failures use the negotiated `ContextGovernorFailureV1` wire contract. Human stderr is not a certified semantic interface.
2. Certified success payloads are strictly validated, including final token fields.
3. Adapter-level cross-caller compression coalescing may not share one pending receipt across independent host settlement attempts. Ares already has a host compression lease; do not create a second settlement authority in the adapter.
4. POSIX timeout cleanup must prove process-group quiescence or return an explicit indeterminate-cancellation failure. All waits and pipe drains are bounded.
5. Certified descriptor execution is rejected on platforms where the current `/proc/self/fd` authority transport is unsupported; process-tree enumeration must not be advertised as Windows certification.
6. Lost `activate-v2` responses are recovered only by exact replay against the idempotent activation contract from Libraries #17. No blind retry exists for other mutations.
7. Tests must exercise real subprocess descendants and real prepare/activate settlement boundaries, not only mocks.

## Completion gates

- targeted Context Governor adapter tests via the repository's canonical test runner;
- blocking Ruff / Windows-footgun checks;
- real Linux descendant-fencing witness;
- exact binary/adapter protocol probe against the companion Context Governor candidate;
- final-head CI with failures/skips reported explicitly;
- no temporary publisher/export files in the proposed final tree.

No merge or live activation is authorized by this document.
