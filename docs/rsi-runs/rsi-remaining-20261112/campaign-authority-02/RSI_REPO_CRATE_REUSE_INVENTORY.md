# RSI Repo/Crate Reuse Inventory (evidence-led, no new repos/crates)

**Recorded:** 2026-11-14 (CST)  
**Scope:** checked canonical repos, worktrees/candidates, and preserved run artifacts for existing RSI-related crates/modules/beginnings before creating anything new. **RSI activation remains OFF.** This is an inventory/status record, not a completion, admission, or enforcement claim.

## 1. Canonical repos checked (current observed state)

| Repo | Observed ref/state | RSI relevance / beginnings present | Preservation |
|---|---|---|---|
| `~/Coding/Libraries` (Cargo workspace) | `remediation/libraries-observability-20260822` (`5ff3b414`), dirty/unrelated changes; RSI branch `rsi/bounded-rsi-tranche-20261112` (`c5b146aa`) exists locally and on origin | **Experiment/evidence/policy primitives:** `forge-pilot`, `living-memory/living-memory` (`forge-engine`), `forge-memory-bridge`, `semantic-memory`, `semantic-memory-forge`, `pilot-bridge`. **Kernel/verification:** `recursive-kernel-core`, `kernel-execution`, `kernel-oracles`, `kernel-conformance`, `verification-policy`, `verification-control`, `verification-calibration`, `verification-adjudication`, `constraint-compiler`, `boundary-compiler`. **Execution/primitives:** `Primitives/check-runner`, `sandbox-workspace`, `typed-patch`, `forge-policy`, `Primitives/effect-signature`, `stack-ids`. **Orchestration/governance/runtime (present, qualification varies):** `agent-graph`, `job-queue`, `profile-runtime`, `authority-delegation`, `effect-runtime`, `assurance-runtime`, `continuity-runtime`, `constitutional-memory`, `knowledge-runtime`, `mechanism-runtime`, `bitemporal-runtime`, `claim-ledger`, `discovery-portfolio`, `agent-evidence-workbench`, `context-governor`, `llm-pipeline`, `turbo-quant`, etc. (workspace members per `Cargo.toml`). | [RecursiveIntell/Libraries#95](https://github.com/RecursiveIntell/Libraries/pull/95) open (`rsi/bounded-rsi-tranche-20261112`) |
| `~/Coding/recursive-agent` | `remediation/recursive-agent-20260822` (`a6418cb`, ahead/dirty/untracked receipts/plans residue) | **Crates:** `recursive-agent-{contracts,ledger,policy,provider,runner,sandbox,tools,daemon,cli,mcp,memory,skills,mcts}`. Existing beginnings for permits/lineage/budgets/session controls, provider/runner/sandbox/daemon surfaces, bwrap dispatch; governed-model-call history/branches exist in repo/worktrees. Advanced RSI candidates (budget/funding/terms/root/campaign inference/readback/recovery compositions) live in run candidates, not yet reconciled into canonical mainline here. | [#36](https://github.com/RecursiveIntell/recursive-agent/pull/36) `rsi/r08-inference-budget-primitive-20261112`, [#37](https://github.com/RecursiveIntell/recursive-agent/pull/37) `rsi/r08-campaign-composition-20261112` (open; **#37 conflicting/overlaps #36**) |
| `~/Coding/Ares` | `skills/profile-collaboration-job-scout` (dirty, mixed unrelated changes) | **Relevant surfaces:** `tools/file_operations.py` multi-path search/pagination (Option A target), `ares_runtime/dispatch_route.py`, `physical_dispatch.py`, `transport_lease.py`, `managed_calls.py`, `collaboration.py`, `plugins/dispatch_governor`, `hermes_cli/middleware.py` dispatch hooks, `hermes_cli/models.py`/OpenRouter model/free helpers, `agent/credits_tracker.py::is_free_tier_model`, `agent/auxiliary_client.py` OpenRouter/free hints, mission/kanban/task owners. Approved Option A fix/tests qualified in run candidates (`approved-helper`, runtime-integration evidence). | [RecursiveIntell/Ares#259](https://github.com/RecursiveIntell/Ares/pull/259) `rsi/runtime-integration-dispatch-20261112` |
| Other relevant local repos/snapshots (inspected by name/path as prior art; not duplicated) | `~/Coding/{mnemes,semantic-memory-plugin,agent-graph-mcp*,agent-graph-release,forge-workbench,ClaimLedger,Ares-*,gmc-*,libraries-*}` and `~/.ares/*` | Additional governed/graph/memory/evidence/runtime beginnings and historical snapshots; reuse/audit before any new implementation. | Local artifacts/worktrees preserved; not all pushed (canonical dirty trees contain unrelated work). |

## 2. Preserved run candidates and evidence (authoritative for recent RSI progress)

**Run root:** `~/.ares/runs/rsi-remaining-20261112` (~26G, 519 receipt-native receipts, 20 result zips). Key records: `CONTRACT.md` (frozen), `APPROVAL_ADDENDUM.md`, `CAMPAIGN_AUTHORITY_ADDENDUM_02.md` (Option A target, **$0 paid / OpenRouter free models only**, supervised/fail-closed; enforcement not admitted), `PUSH_AND_PRESERVATION_MANIFEST.txt`.

| Candidate/artifact | What exists (beginning/qualified slice) | Reuse note |
|---|---|---|
| `approved-helper/Ares` + `APPROVED_CONTINUATION_REPORT.md`, `rsi-approved-helper-results.zip` | Option A pagination helper fix + contract tests qualified in isolation | Carry into admitted unfrozen composition via existing gate/freeze workflow; do not mutate frozen captures in place. |
| `native-r08/recursive-agent` + `rsi-r08-native-budget-results.zip` | Conservative inference budget/readback library subgate (once-only dispatch, immutable settlement, conservative unknown/crash charging, readback without authority) | Library-level only; no daemon registration/IPC/physical-send authority claimed. |
| `native-composition-r08/recursive-agent` (+ funding/terms/root/bank/recovery/campaign evidence zips and freezes) | Composed policy extensions: funding enrollment/authority separation, monetary terms validation, root association, campaign inference/bank V2 accounting/readback, recovery-behavior qualification slices (bounded processes) | Strong beginnings; **not** end-to-end physical-send $0 enforcement; freeze/source-generation boundaries must be respected. |
| `runtime-integration-r08/Ares|recursive-agent|Libraries` (+ gate scripts, freezes, slice reports) | Runtime integration candidates, dispatch-governor/managed-call/route observations, Option A helper identity present in captured Ares | Freeze drift exists across generations; `runtime_integration_gate.py` and admitted freeze discipline required before qualification claims. |
| `stack-reuse-01/*`, `campaign-authority-02/*` | Reuse census/decisions, R00 decisions, route/admission gaps, Option A admission evidence, R08 free-OpenRouter admission contract/checklist/progress (design-only) | Reuse-first authority; no new owners/stores/providers. |

## 3. Gate-aligned reuse map (no new components)

| Gate | Existing repos/crates/beginnings to reuse | Missing / not qualified |
|---|---|---|
| R00 | Decisions recorded (`CAMPAIGN_AUTHORITY_ADDENDUM_02.md`, `R00_DECISIONS_RECORDED.json`) | Campaign enforcement/admission not bound in canonical owners |
| R01/R02/R03/R04/R07 | Libraries `forge-pilot`, `verification-policy`, `typed-patch`, `forge-policy`, `check-runner`, `sandbox-workspace`, `living-memory/forge-engine`; tranche/resume repairs and approved helper evidence | Remaining truthfulness/scope/TOCTOU/repetition/heuristic-shortcut closure per task matrix; admitted freezes/current-source reconciliation |
| R05 | `sandbox-workspace`, `check-runner`, native sandbox/provider/runtime containment beginnings | Qualified hostile-candidate containment/resources/stop/readback (product sealed backend) |
| R06 | Forge checks/suites, `verification-*`, `claim-ledger`/evidence primitives | Independently custodied evaluator, frozen holdout/protocol/data bindings, negative controls |
| R08 | Ares dispatch-governor/managed-call/route/lease + OpenRouter hints; native policy funding/terms/root/bank/inference/readback/recovery candidates; `llm-pipeline`/AiDENs budget primitives (domain-limited) | **Decisive:** truthful $0-paid/free-tier representation in existing owner, authoritative free-route/model/api-mode/request/tariff admission, durable reserve/release/settle at **every physical send** incl. retries/auxiliaries, fail-closed unknown/paid fallback, no shared-config mutation/live egress |
| R09–R12 | Native runner/provider/autonomous/planning beginnings, Ares mission/kanban/task/dispatch owners, Forge pilot/engine, evidence spine | Qualified write-capable producer, canonical campaign/adapter integration, supervised bounded improvement, disposable adoption + verified active identity + backout + genuine successor consumption |
| R13–R16/closeout | Existing experiment/evidence/calibration/graph/memory surfaces | Preregistered matched-budget study, restricted unattended/recovery/fault gates, optional extensions only if evidenced, cross-family/receipt-backed closeout |

## 4. Explicit “do not create” (already covered by existing owners)

No new bridge/compiler/kernel scheduler/general loop/procedural-memory store/queue/task DB/budget bank/provider integration/shadow promotion or adoption controller. Extend/compose existing owners in isolated, receipt-backed candidates only.

## 5. Concrete next step (blocked on admission decisions)

1. **R08 engineering (reuse-only, isolated):** create/admit a new unfrozen Ares/native composition generation; carry approved Option A helper + tests; extend existing `recursive-agent-policy` (and compose Ares send owners) for truthful $0-paid free-OpenRouter semantics, exact route admission, and reserve-before-egress / once-only conservative settlement at every physical send with negative/crash/retry proofs. **No live provider calls, no shared config mutation, no activation.**
2. **Hygiene:** reconcile admitted freezes vs drift; decide handling for recursive-agent **#37 vs #36/main** conflict and review advancement for #95/#259/#36 without disturbing unrelated dirty work.

## 6. Blockers requiring user input

- Exact free OpenRouter `:free` model(s)/endpoint/protocol/api-mode/effort and request/token bounds to qualify first, plus acceptable evidence for “zero paid / no paid fallback”.
- Approval to extend existing `recursive-agent-policy` budget/terms/inference owners for truthful $0-paid semantics (isolated candidate, tests/receipts).
- Reconciliation/merge direction for conflicting/overlapping PRs and dirty canonical checkouts.
- Later admissions: R05 containment backend/witness scope, R06 independent evaluator custody/frozen holdout, R09 producer route/resources, R12 adoption/backout authority.

**Bottom line:** existing repos/crates/beginnings have been checked and mapped to RSI gates; substantial reusable substrate exists, but the full RSI program is **not complete**. Progress is currently **blocked on R08 admission/enforcement decisions and subsequent qualification gates**, not on creating new foundational components.
