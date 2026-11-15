# R08 Implementation Checklist — free OpenRouter, $0 paid (reuse-first)

**Current state:** design-only progress recorded; no product-source changes made by this step; activation OFF.

## Immediate next engineering steps (isolated, receipt-backed)

1. **Freeze/admit target pair for integration**
   - [ ] Select or create an admitted isolated candidate generation for Ares/native/runtime composition (do not mutate frozen `runtime-integration-r08` bytes in place; follow existing freeze/gate scripts).
   - [ ] Carry approved Option A helper fix + `test_multi_path_pagination_contract.py` into the admitted Ares generation with scope verification and focused regression receipts.
   - [ ] Record exact HEADs, path-state manifests, dependency identities, and rollback patch scope.

2. **Close zero-paid owner contract in existing native policy (no shadow store)**
   - [ ] Inspect `InferenceBudgetLimitsV1` / `InferenceBudgetTermsV1` / campaign inference reservation/settlement invariants.
   - [ ] Design explicit zero-paid/free-tier representation compatible with conservative non-refund/once-only laws (or document why current owner cannot honestly represent it and what extension is required).
   - [ ] Add tests: accepted admitted zero-paid terms, refused paid/unknown/zero-invalid forms per chosen semantics, no balance initialization, no send authority from deserialized/report artifacts.

3. **Close authoritative route/model admission in existing Ares/native composition**
   - [ ] Define exact admitted OpenRouter `:free` model(s), endpoint/protocol/api mode, effort/request bounds, and canonical identity mapping.
   - [ ] Implement fail-closed checks: route/model mismatch, missing qualification, paid fallback/unknown pricing, unsupported protocol/bounds, credential/config leakage.
   - [ ] Treat `is_free_tier_model` / catalog labels as hints only; require canonical qualified evidence.

4. **Bind monetary reservation/release to physical send boundary**
   - [ ] Compose dispatch-governor/send observation with native reservation-before-egress and settlement-after-outcome for every physical send/retry/auxiliary path.
   - [ ] Prove no bypass: direct client send, managed call, plugin callback, IPC/report paths cannot release without owner agreement.
   - [ ] Add crash/timeout/unknown-outcome recovery tests and receipt verification.

5. **Qualification gates**
   - [ ] Run existing receipt-native/native/runtime gate scripts for touched candidates; preserve failed receipts.
   - [ ] Produce source snapshots, diffs, rollback applicability, hash/package verification, and residual-risk matrix.
   - [ ] Do not claim campaign admission, live egress, adoption, or RSI completion.

## Blockers / decisions needed
- Explicit admission of the exact free OpenRouter route/model set and any owner-contract extension for truthful $0-paid semantics.
- Permission/process for reconciling dirty canonical repos/PRs (#37 conflict, #36/#259/#95 review state) without disturbing unrelated work.
- Independent evaluator custody (R06), containment backend qualification (R05), producer admission (R09), adoption authority (R12) remain separate gates.

