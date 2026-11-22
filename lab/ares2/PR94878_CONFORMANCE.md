# PR #94878 — exact child-authority contract gate

**Required for Ares 2.0 before any capability port.** This branch holds intentionally
unqualified conformance witnesses against pinned stock Hermes; it is NOT a
completed security port.

- Oracle: [NousResearch/hermes-agent#94878](https://github.com/NousResearch/hermes-agent/pull/94878), exact head `61e2dede7f92a76919ca797c314c5a2e83541bbd`.
- Context: [#91293](https://github.com/NousResearch/hermes-agent/pull/91293) is overlapping provenance history, not a separate second landing object.
- Conditional Singularity component: [NousResearch/hermes-official-plugins#362](https://github.com/NousResearch/hermes-official-plugins/pull/362), draft `4d6dcb23063613865abda1fa641be401dd6dea79`.

## First red-witness cases

`tests/tools/test_ares2_profile_child_boundary_contract.py` exercises only
source→target snapshot/ownership semantics using real temporary `.env` files:

1. Remove source-owned values when no target grants exist.
2. Replace colliding source values with target-authorized values.
3. Preserve monotonic ownership after a source revokes a value.
4. Refuse unreadable source provenance rather than treating it as empty.
5. Remove an Apptainer forwarding carrier without losing a target's direct grant.

These tests are expected to be RED on the current Hermes pin: the
`build_profile_env_boundary` API is absent. This is source reasoning; NO
test run or expected failure has been observed in CI. Do not label the tests
`xfail`, mark them skipped, or convert them to source-text assertions.

## Required follow-through

The same-contract acceptance gate must additionally cover the actual
foreground/background/PTY, SSH, Docker, Kanban, cron, compute-host, and shell
snapshot execution paths using original PR #94878 tests, after porting the API
and its callers. For supported paths, require a real child env capture and
zero foreign credential values. Ordinary process environment policy and
OS-level confinement are distinct guarantees.

The desktop Clarify lifecycle and raw-author contributor attribution changes
in PR #94878 belong in independently reviewed parity slices. The Singularity
plugin remains external to stock Hermes; enabling it requires #362 evidence.

## Evidence boundary

**Source implementation: NOT PORTED. Tests: NOT RUN. Security gate: OPEN.**

Run under upstream's managed Python 3.14 environment and isolated home:
```bash
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/tools/test_ares2_profile_child_boundary_contract.py -q
```
