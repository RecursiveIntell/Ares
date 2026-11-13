# Scientific falsification: implementation handoff and program tracker

Recorded 2026-09-20 and revalidated 2026-09-21. Status: review-ready experimental vertical slice in three draft PRs; no merge, installation, runtime activation, support admission, publication, or theorem certification.

## Entry points and source pins

| Owner | PR | Code revision exercised |
|---|---|---|
| Libraries: exact finite witness kernel and synthetic corpus | RecursiveIntell/Libraries#26 | `fab2349572e3c576b8e60579ea113cd460919ab9` |
| ClaimLedger: scientific evidence checking and replay | RecursiveIntell/ClaimLedger#1 | `403a9256a7ed5c36ef3a1184c4ad4bec72ab4aa4` |
| Ares: optional falsify skill and bounded runner | RecursiveIntell/Ares#59 | `942d96c664a3568296e75a83f35731899ca1250a` |

These are the tested code revisions. A later documentation-only commit may update this handoff without changing the exercised code. Changes to executable code after these pins require appropriate revalidation. Existing repo instructions, ownership, and merge gates still apply.

## What is implemented

- Exact rational finite systems `A b = r`, `T b = 0`, `b >= 0`: primal validation and standard Farkas dual separation, bounded candidate search and explicit unresolved results.
- Periodic edge-diffusion/shear operators with discrete energy/mass checks, explicit material-label equalities, and seven synthetic regression cases.
- Strict scientific statement declarations and field-level contract diffs, independently rechecked certificates, input/statement binding, detached output digests and replay. No general prose-to-math equivalence or source authentication.
- Optional Ares `falsify` skill with fixed `verify`, `diff`, and `solve` operations. Explicit trusted backend identities, selected-workspace inputs, fresh private outputs, streamed output limits, per-step deadlines, scrubbed subprocess environment and process-group cleanup.
- Real three-repo smoke entrypoint: `optional-skills/research/falsify/scripts/integration_smoke.py`.

Libraries is an isolated nested Cargo workspace, not a root default member. The Ares skill is optional and has not been installed into a live profile. ClaimLedger remains the sole support/admission owner.

## Observed validation

- Libraries scoped CI run **35557609650** passed formatting, strict Clippy, 14 Rust tests, the actual CLI build, all seven independently rechecked native corpus outcomes, and the declared Rust 1.75 floor.
- ClaimLedger scoped CI run **35557643920** passed 29 scientific tests, compile checks and repository-policy lint on Python 3.11. Its pinned three-repo job checked out the exact revisions above and passed all seven real kernel -> evidence -> runner cases. Corporate release-quality run **35557644021** passed on Python 3.11 and 3.12.
- Ares optional-skill workflow **35556955457**, Docker workflow **35556955554**, and Nix workflow **35556955423** passed. Full CI run **35556956074** passed every technical job; only the intentional `ci-reviewed` label gate and its aggregate remained blocked for operator review.
- Local verification of byte-matched new Python surfaces: **29 ClaimLedger tests passed** on Python 3.11 and 3.12; **12 Ares tests passed** on Python 3.11, including the explicitly selected real ClaimLedger integration test; targeted Ruff checks passed; Ares authoring standards passed **1214 tests**. Local static corpus checking passed all seven cases.
- No local Rust compiler was available; native Rust, formatting, Clippy and MSRV evidence comes from CI, not a substituted local run.

Public CI: https://github.com/RecursiveIntell/Libraries/actions/runs/35557609650 and https://github.com/RecursiveIntell/Ares/actions/runs/35556955457 . Authorized operators can inspect the integration evidence at https://github.com/RecursiveIntell/ClaimLedger/actions/runs/35557643920 .

Corpus digest: `bb38cfb08e05db0fc797382a2a0d6148ad83454313cea0106efb690170b69484`.
Observed outcomes: four infeasible finite systems, two feasible controls, and one intentionally unresolved budget-exhaustion case. All seven expected behaviors passed; this does not mean all seven claims were refuted.

## Reproduce the real integration

Select reviewed checkouts at the exact pins above and an existing trusted Python environment containing ClaimLedger's dependencies. This script is a test entrypoint, not an installer.

```sh
cargo test --manifest-path "$LIBRARIES/constitutive-witness/Cargo.toml" --locked --offline
cargo build --manifest-path "$LIBRARIES/constitutive-witness/Cargo.toml" --locked --offline
python "$LIBRARIES/receipt-bench/fixtures/falsify/check_corpus.py" \
  --binary "$LIBRARIES/constitutive-witness/target/debug/constitutive-witness"
python "$ARES/optional-skills/research/falsify/scripts/integration_smoke.py" \
  --claimledger-root "$CLAIMLEDGER" --libraries-root "$LIBRARIES" \
  --kernel "$LIBRARIES/constitutive-witness/target/debug/constitutive-witness" \
  --python /absolute/path/to/trusted/python --out /existing/parent/new-smoke
```

The output path must not already exist. Keep generated sources and logs private where the inputs require it. The skill's own documentation explains explicit reviewed checker/binary digest selection for operator use.

## Independent auditor handoff

Attempt to falsify certificate sign/shape/overflow handling, omitted transport rows, changed statement/problem binding, canonical type distinctions, rehashed tampering, unresolved-to-supported widening, output overwrites, stale backend identity, secret inheritance, subprocess pipe deadlock and cleanup. Examine actual code and replay receipts, not this summary. Confirm private source material is not copied into public outputs. Reproduce under full selected checkouts and record pass/fail/skip with reasons.

No OS containment is claimed: trusted backend code has local account authority. A checksum is not signed identity, and imported system dependencies are not fully content-addressed. Same-user adversarial mutation, descendant session escape, platform coverage and stronger sandboxing remain separate qualification work.

## Remaining tracked gates

### Before merging or packaging

- [ ] Operator review and explicit merge decision. The Ares `ci-reviewed` label is deliberately not self-applied.
- [x] Scoped final-head gates: Rust format/Clippy/native corpus/MSRV, ClaimLedger Python 3.11 evidence and Python 3.11/3.12 release quality, Ares optional skill/Docker/Nix and all technical full-CI jobs.
- [ ] Broader supported-OS and reproducible dependency qualification beyond the exercised Linux CI lanes.
- [ ] Explicit decision on root-workspace/package integration and optional skill installation, with rollback and real target-host evidence.

### Research and capability expansion

- [ ] Larger primal/dual backends, coefficient bounds and smoothness constraints, with equal-problem baselines before any performance claim.
- [ ] Pressure-projection and material-flow adapters with rigorous discretization-error evidence. Componentwise diffusion and symmetric stress must remain distinct models.
- [ ] Additional scientific evidence methods: symbolic identities, continuum counterexamples, scaling diagnostics, convergence and actual formal-proof receipts. Each needs method-specific acceptance semantics.
- [ ] Reviewed prose/equation/code/Lean statement mappings using existing ClaimLedger and spec-execution ownership; no duplicate truth store.
- [ ] Expand the seven-case regression seed into independently different falsification tasks with feasible controls, held-out evaluation and leakage checks. A separate FalsifyBench repo is not justified by this seed alone.
- [ ] Formalize small obstruction lemmas with toolchain pins, real builds, exact theorem types and axiom reports.
- [ ] Independently review the historical conditional transported-scalar theorem and its upstream hypotheses. Preserve selected-scalar and retuned-force restrictions. Novelty review must precede any public mathematics repository or publication claim.

Ares repository Issues were disabled when an umbrella issue was attempted; no repository settings were changed. This committed tracker plus the linked PRs is the durable tracking surface.

## Rollback and final boundary

Close the draft PRs or revert their additive commits; remove/disable the optional skill if separately installed later. No claim-store migration, runtime-pointer change, manuscript overwrite, credential change or side-effect reconciliation is needed for this source-only work. The conditional mathematics is not admitted by these tests. No solver novelty, benchmark superiority, commercial ROI, or production readiness is asserted.
