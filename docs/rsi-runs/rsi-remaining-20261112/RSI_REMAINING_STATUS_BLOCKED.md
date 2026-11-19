# RSI remaining work — status & blocked (evidence-led)

**Recorded:** 2026-11-14 (CST)  
**Goal:** finish RSI-related work, reusing existing repos/crates/beginnings (no new repos/crates created).  
**Activation:** **OFF**. No product/source installs, no live grants, no provider calls, no shared-config/credential mutation, no deployment/adoption.

## Repos checked (reuse-first)

- **Libraries** (`~/Coding/Libraries`, `remediation/libraries-observability-20260822`, dirty): relevant crates/beginnings present — `forge-pilot`, `forge-memory-bridge`, `living-memory`/`forge-engine`, `semantic-memory*`, `pilot-bridge`, `recursive-kernel-*`, `kernel-*`, `verification-*`, `constraint-compiler`, `boundary-compiler`, `Primitives/{check-runner,sandbox-workspace,typed-patch,forge-policy,effect-signature}`, `stack-ids`, `agent-graph`, `job-queue`, `context-governor`, `claim-ledger`, `agent-evidence-workbench`, etc. RSI PR: [#95](https://github.com/RecursiveIntell/Libraries/pull/95) (`c5b146aa`, mergeable at snapshot).
- **recursive-agent** (`~/Coding/recursive-agent`, `remediation/recursive-agent-20260822`, ahead/dirty): crates `recursive-agent-{contracts,ledger,policy,provider,runner,sandbox,tools,daemon,cli,mcp,memory,skills,mcts}`; governed/budget/funding/terms/root/campaign/recovery beginnings in run candidates. PRs: [#36](https://github.com/RecursiveIntell/recursive-agent/pull/36) (mergeable, checks passing) and [#37](https://github.com/RecursiveIntell/recursive-agent/pull/37) (**conflicting/dirty**, large composition overlap).
- **Ares** (`~/Coding/Ares`, dirty): Option A helper/dispatch/managed-call/route/lease/governor/mission/kanban surfaces and OpenRouter/free-tier hints. PR: [#259](https://github.com/RecursiveIntell/Ares/pull/259) (mergeable at snapshot).
- **Run evidence preserved:** `~/.ares/runs/rsi-remaining-20261112` (519 receipt-native receipts, 20 result zips, frozen candidates/reports).

## Concrete artifacts produced/preserved this continuation

- `campaign-authority-02/RSI_REPO_CRATE_REUSE_INVENTORY.md`, `RSI_REPO_CRATE_REUSE_INVENTORY.json`
- `campaign-authority-02/R08_FREE_OPENROUTER_ADMISSION_CONTRACT_V1.md` (draft, not admitted)
- `campaign-authority-02/R08_IMPLEMENTATION_CHECKLIST.md`
- `campaign-authority-02/R08_ZERO_PAID_OWNER_EXTENSION_PROPOSAL.md` (design-only, not implemented)
- `campaign-authority-02/R08_FREE_OPENROUTER_CANDIDATE_MODELS.md` (observed hints, not admitted)
- `campaign-authority-02/RSI_PR_STATUS_SNAPSHOT.json`
- Prior preserved: `PUSH_AND_PRESERVATION_MANIFEST.txt`, `stack-reuse-01/REUSE_DECISIONS.md`, `CAMPAIGN_AUTHORITY_ADDENDUM_02.md`, `APPROVED_CONTINUATION_REPORT.md`, `R03_REPORT.md`, `RESUME_REPORT.md`, tranche/composition evidence zips.

## Progress summary (qualified in isolated candidates; not whole-program completion)

- Substrate/tranche repairs and regressions (admission/policy/evidence/oracle/repeat/python adapter slices) evidenced in prior run packets (e.g. 344/345-class passes, 0 failed, 1 ignored at recorded snapshots; Clippy/format/receipts preserved).
- Option A helper (file pagination) qualified in `approved-helper` and present in runtime-integration captures (**157 contract tests passed** in recorded runs); not freeze-stably admitted as mainline/runtime activation.
- Native R08 beginnings: budget/readback, campaign funding/terms/root association/bank, dispatch/mission/runtime scaffolding, recovery-behavior slices — **library/composition subgates only**, not end-to-end $0-paid physical-send enforcement.

## Decisive remaining gaps

- **R08 (blocking):** no proven fail-closed end-to-end **$0.00 paid / free OpenRouter only** enforcement at every physical send (incl. retries/auxiliaries). Needed: truthful owner representation (current `InferenceBudgetLimitsV1`/terms reject zero paid semantics), authoritative exact route/model/api-mode/effort/request/tariff admission, reserve-before-egress + once-only conservative settlement binding to Ares physical-send/dispatch-governor and native owners, refusal on paid/unknown/mismatch.
- **R05/R06/R09–R12:** hostile-candidate containment qualification, independent evaluator custody/frozen holdout, qualified write-capable producer, canonical campaign/adapter integration, supervised bounded improvement, disposable adoption/verified active identity/backout/genuine successor consumption.
- **R13/R14/closeout (+ optional R15/R16):** matched-budget/restricted-unattended qualification if pursued, extensions only if evidenced, cross-family receipt-backed closeout.
- **Hygiene:** freeze drift across runtime/composition generations; canonical checkouts dirty/unrelated; recursive-agent #37 conflicts with #36/main intent.

## Blocked — decisions/admissions required to continue

1. **Exact R08 free-route qualification target:** which specific OpenRouter `:free` model, endpoint/protocol/api mode, reasoning effort, and request/token bounds to qualify first, and what evidence is acceptable that this exact path is **zero paid with no paid fallback**.
2. **Owner-extension authority:** approval to extend existing `recursive-agent-policy` budget/terms/inference owners (isolated candidate, receipt-native tests/mutants) for truthful $0-paid semantics while preserving once-only, conservative unknown/crash charging, no-refund/replay, and readback invariants (no shadow bank).
3. **PR/reconciliation direction:** how to reconcile recursive-agent **#37 vs #36/main** (and advance #95/#259/#36) without disturbing unrelated dirty work in canonical checkouts.
4. **Later admissions (separate gates):** R05 containment backend/witness scope, R06 independent evaluator custody/frozen holdout, R09 producer route/resources, R12 adoption/backout authority.

## Conclusion

**RSI-related work is not complete.** Reusable repos/crates/beginnings have been checked, preserved, and mapped; design/proposal artifacts for the next decisive step (R08) are prepared. Further progress is **blocked** on the admissions/decisions above and cannot be honestly advanced to a qualified $0-paid physical-send boundary, supervised improvement cycle, or activation without them. No activation performed.
