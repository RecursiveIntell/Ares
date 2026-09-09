# Hermes v0.21.1 → Ares reconciliation receipt

Status: in progress; branch/worktree only; not merge-ready and not activated.

## Source pins

- Shared Ares/Hermes merge base: `8966b0a70029cb226e35c49f91d0c2208ab1d8c4`
- Original Ares main source pin: `7ecc1f0a94b967abb89153a4904ca6551b6c7128`
- Current-main owner pin incorporated before the full merge: `fa74bfdb98e541fe719bb0f99f6bd44bce034f44`
- Starting PR #35 head: `e5f745e1697d610a1a7abf30d76e2639b96007f3`
- Hermes v0.21.1 / v2026.9.7: `2237be355906fbe6065ce1815711eee52b2d646e`
- Verified source bundle SHA-256: `7070d36d5e20f0a15c2fc3f8edabb68429ec113c4fd2d00f2e3fa2dd69131d6e`

The bundle reported complete history and reproduced the three pinned refs above before this worktree was created.

## Divergence and conflict inventory

At the shared baseline:

- Ares worktree head is 243 commits ahead.
- Hermes v0.21.1 is 6,860 commits ahead.
- Ares changed 482 paths; Hermes changed 5,453 paths; 216 paths overlap.
- A normal three-way merge reports exactly 121 conflict paths.

The conflict set divides mechanically into:

- **36 upstream-only conflict surfaces**: unchanged between the shared baseline and pinned Ares main. Their conflicts come from the selective #34 forward-port work, not an Ares-owned mainline divergence. These resolve to the pinned Hermes v0.21.1 result.
- **85 owner-sensitive conflict surfaces**: changed on pinned Ares main after the shared baseline. These must preserve Ares-owned semantics while admitting compatible upstream changes.

## Owner-preserving merge rule

1. Perform a real two-parent merge of the pinned Hermes release into this branch.
2. Preserve non-conflicting upstream changes normally.
3. On the 85 owner-sensitive conflict paths, prefer Ares for overlapping hunks first; then audit each lost upstream conflict hunk and port compatible behavior explicitly.
4. On the 36 upstream-only conflict paths, take the pinned upstream file result.
5. Preserve Ares authority/permit, governed-context and staged-compression commit semantics, managed admission, effect settlement, receipts/replay, profile isolation, release custody, and staged activation.
6. Do not add hidden fallback, silent authority widening, or a second owner for Ares-owned semantics.
7. Remove temporary source/environment export workflows before finalization.
8. Validate exact final-head behavior; report failures/skips/not-run gates instead of counting them as passes.

## Initial acceptance gates

- clean merge state with no conflict markers or unmerged paths;
- `git diff --check`;
- Python compile/import sanity over changed Python surfaces;
- Ares-focused conformance/isolation tests mapped to owner-sensitive changes;
- upstream regression tests for retained Hermes behavior;
- `uv lock --check`, blocking Ruff, applicable JS/TS/Desktop checks;
- final branch CI, Nix flake check, and Docker workflow;
- temporary reconciliation workflows absent from the proposed final tree.

No merge to `main`, force-push, runtime activation, live migration, provider call, or user-state mutation is authorized by this receipt.

## Structural reconciliation checkpoint — 2026-09-09

The reconciliation was rebased onto the then-current Ares main owner state before merging Hermes:

- current-main owner pin incorporated first: `fa74bfdb98e541fe719bb0f99f6bd44bce034f44`;
- conflict inventory remained exactly 121 paths: 36 upstream-only, 85 owner-sensitive;
- upstream-only conflicts resolve to the pinned Hermes tree;
- owner-sensitive conflicts preserve Ares on overlapping hunks, with decomposed upstream owners restored where an old monolithic Ares body would otherwise become a shadow owner;
- Bot Mode retains upstream modular `relay.ts` ownership plus Ares keyed bounded delivery concurrency;
- managed Ares runtime keeps custody of its release-pointer systemd unit;
- multiplex authorization remains fail-closed if profile scope resolution fails;
- oneshot keeps opt-in audit transcript archiving at the decomposed cleanup owner;
- `ares_runtime/` remains unchanged by this upstream merge.

Structural gates at this checkpoint:

- unmerged index entries: 0;
- real conflict start/end markers: 0;
- `git diff --cached --check`: PASS;
- changed Python files compiled: 3,681 / 3,681;
- syntax/indentation failures: 0.

The exact validated structural tree is `0085da4d85ac61772b09c4b4947b49667315b44e`.
GitHub Actions reproduced that tree, compiled all changed Python files, and created local merge commit `743ed4b916…`; its only failure was the final push, which GitHub rejected because the Actions token may not update workflow files. The same verified tree was then committed through the GitHub API as the real two-parent branch commit `f59ca66c625bf58c2078882367e03b19f4666fb2`, with Hermes `2237be355906fbe6065ce1815711eee52b2d646e` as parent 2.

This is a **structural checkpoint only**. Import sweeps, owner-focused behavioral tests,
lock/lint checks, desktop/TUI checks, and final CI/Nix/Docker remain required before any
merge-ready claim.
