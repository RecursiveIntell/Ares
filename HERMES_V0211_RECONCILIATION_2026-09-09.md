# Hermes v0.21.1 → Ares reconciliation receipt

Status: in progress; branch/worktree only; not merge-ready and not activated.

## Source pins

- Shared Ares/Hermes merge base: `8966b0a70029cb226e35c49f91d0c2208ab1d8c4`
- Ares main source pin: `7ecc1f0a94b967abb89153a4904ca6551b6c7128`
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
