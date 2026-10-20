# Retained Reliability Work: Current-Main Implementation Plan

> **For Ares:** Execute with one controller by default. Use the current profile-collaboration relevance gate for a decision-changing specialist; no automatic per-task fan-out or commit.

**Goal:** Implement and qualify the still-relevant reliability repairs on current `main`, preserving deployed context fixes and existing contribution history.

**Architecture:** Keep Ares input, goal, policy, lifecycle and release state in their existing owners. Reconcile the existing reliability PR stack rather than replaying an old dirty tree. Treat Graph protocol repair and native coding-worker development as separate repository boundaries, not prerequisites for an independently qualified Ares repair.

**Tech stack:** Python, SQLite, Electron/TypeScript, the existing pytest/desktop test runners, and the existing Rust Graph/native owners.

**Status:** Planning-only PR. No application source, configuration, dependencies, workflow, service, goal, model setting or release selection changes. Approval of this document is not a runtime qualification result or permission to bypass a failing gate.

## 1. Source cutoff and corrections

The inspected Ares `main` is `0369fdd20d7419f332e0c718d469d6898682fb2e`. It preserves the merged context work in [#164](https://github.com/RecursiveIntell/Ares/pull/164), managed Governor admission in [#160](https://github.com/RecursiveIntell/Ares/pull/160), and desktop focus/selector work in [#161](https://github.com/RecursiveIntell/Ares/pull/161), and now includes [#167](https://github.com/RecursiveIntell/Ares/pull/167) OpenCode session-affinity, retry and summary-exit fixes. The earlier runtime assessment inspected installed `12962d39c7deb2772f734eca2db08e5a84861052`, with a hash-verified bundled Governor sourced from Libraries `2fcf600b8c92f058b3a3ff3d0d079d4445919c84`. That installation observation is historical; this planning PR does not recertify live process adoption.

The exact inspected heads are in [the source snapshot](2026-10-15-retained-work-sources.json). Refresh them at implementation admission; this cutoff is not a lock on a moving branch.

Two earlier classifications need correction:

- **Not on `main` does not mean unpublished.** The reliability source is already in draft PRs #162, #163, #165, #166 and #168. The absence of a differently named local branch from GitHub did not establish absence from other PRs. Preserve these carriers and their review/test histories.
- **The October 12 private PDF implementation is not the preferred patch wholesale.** [#166](https://github.com/RecursiveIntell/Ares/pull/166) subsequently preserves flat public attachment paths with no-clobber publication. Reuse its later contract and retain the relevant behavioral witnesses; do not reintroduce an incompatible returned-path shape.

The diagnostic against installed source `12962d39…` used retained regression tests, imports verified from that source, disposable state and an existing external development environment: 21 staging cases yielded 12 failures/9 passes; 17 PDF responsiveness cases yielded 17 failures. The concrete inspected witnesses reached same-reader control blocking and real-Poppler cross-session page-path aliasing. These are diagnostic observations, **not exact-lock CI parity, live-GUI interaction, 29 distinct bugs or a release verdict**. Historical large passing cohorts qualify only their recorded source/input/command selections and must not be summed or transferred to a new composite.

## 2. Existing source carriers and contribution boundaries

All listed PRs are authored by RecursiveIntell; preserve their actual authored commits and predecessor credit. This plan does not merge, retarget, close, replace or reassign any existing PR.

| Carrier | Inspected head | Base | Reconciliation role |
|---|---|---|---|
| [#162](https://github.com/RecursiveIntell/Ares/pull/162) | `f354416f278cf5c3fc4d04be90094f662d1709f7` | `main` | Broad reliability carrier; published candidate source, with recorded regression debt, not an accepted release. |
| [#163](https://github.com/RecursiveIntell/Ares/pull/163) | `74d8bfde8af48fc3cebf9fafa7f6646e35647efc` | #162 branch | Packaging, retained child ownership/retirement, catalog compatibility and continuation-reply repairs. |
| [#165](https://github.com/RecursiveIntell/Ares/pull/165) | `954adf3f0bbc65f5ac8c9d3038e9de4cbb436183` | #163 branch | Empty-secret epoch stability, durable budget stops, LSP failure identity and lazy RPC dispatch. |
| [#166](https://github.com/RecursiveIntell/Ares/pull/166) | `99fa8ad7b86ca6bfa5a4df5e0bd486cb114943e8` | #165 branch | No-clobber attachment publication, preserved public paths, Stop/input and TLS fixture corrections, Poppler CI prerequisites. |
| [#168](https://github.com/RecursiveIntell/Ares/pull/168) | `4af08f766d095ab9596dd12cb0a13e1c92cd6a34` | #166 branch | Prior-main integration carrier; includes `12962d39…` as an ancestor, but not the newer #167 merge. Its small PR-relative diff is not the whole main-relative reliability delta. |
| [#154](https://github.com/RecursiveIntell/Ares/pull/154) | `cccc329412b72e5dcccb00701a1403fbad085245` | `main` | Adjacent runtime-option contribution; resolve shared selector/catalog/compute-host surfaces before integration, without duplicating its implementation. |

The inspected ancestry chain is #162 → #163 → #165 → #166 → #168. Prior `main@12962d39…` is also an ancestor of #168; current `main@0369fdd2…` is not, so #167 must be reconciled before implementation qualification. This makes #168 an integration input, **not an automatically selected survivor or merge approval**. Decide the implementation branch and landing topology explicitly at R0. Prefer small independently qualified slices; do not copy the entire 152-file #162 carrier onto `main` or hide its full delta behind a stacked base.

The inspected rollups still contain failures on #162/#163/#165 and a CI-sensitive maintainer review gate on #166. #168's inspected checks include success and skipped states; neither its parent results nor a path-filtered/stack-relative result proves full main-relative parity. Read current-head runs and required rules again before landing. The known regression ledger and each actual failing test ID must receive a current disposition, not be closed by a PR title or a broad local pass count.

## 3. Scope and preserved invariants

Keep the repairs independently releasable while preserving these owner, identity and safety boundaries through every slice.

### In scope

1. PDF/attachment publication and reader/control responsiveness.
2. Launch-local policy, single-pass bounded reads, sandbox progress and LSP enrichment.
3. Delegate admission/deadlines/schema repair, owned settlement, and goal/input recovery.
4. Route-preserving catalog discovery and runtime-option compatibility.
5. Versioned input/media custody, final-wire accounting, safe readers and backup/backout.
6. Source-bound evidence, package/CI parity, controlled native integration and separately admitted rollout.
7. A separate Graph tool-response protocol repair.
8. Explicitly parked native coding-worker work and optional operational dispositions.

### Excluded or already satisfied

- Do not reinstall an old `49450ed5…`/`be0088f8…` release, reapply already landed #157/#160/#161/#164/#167 blindly, or overwrite their newer source.
- Do not carry the rejected native C5 shared-directory leaf-overlay mechanism into any implementation branch.
- Do not widen the supplemental Codex broker's filesystem/tool authority, add a shadow task/input/acceptance database, or treat a model score as acceptance.
- Do not change credentials, provider/model defaults, the current orchestration chat, unrelated profiles, live goals or services as a side effect of reconciliation.
- Do not automatically retry accepted/started/unknown effects, reset cumulative budgets, suppress failed checks, or promote skipped/platform-ineligible cases to passes.
- No registry publishing, broad dependency upgrade, unrelated PR closure or blanket fleet restart is included in this planning PR.

Prompt prefix/role invariants, exact route/profile/credential identity, immutable receipt history, explicit owner epochs, current runtime controller contract and real rollback custody remain mandatory.

## 4. Acceptance namespaces and coverage

[The coverage index](2026-10-15-retained-work-coverage.csv) preserves every inherited task `0.1`–`6.3`, issue `R01`–`R40`, case `A01`–`A74` and `B01`–`B20`, gate `G0`–`G12`, completion card `C00`–`C13`, and subordinate context task `T01`–`T17`/criterion `CR01`–`CR44`. Rows are obligations/dispositions, not test results or grants. Card links are many-to-many scheduling links; they do not transfer ownership of an inherited case.

The source packet's current Markdown and its older generated DAG declare different plan hashes. Preserve both snapshots. R0 must reconcile that difference and regenerate any implementation projection from admitted source; do not silently stamp the old DAG with a new hash or treat its old validation as current. The published coverage index supplies identifiers, definitions and owner/card links for this reconciliation; it does not replace the full safety clauses with a count-based completion test.

**Namespace distinction:** master `C04` is lifetime policy, master `C05` is containment, and master `C09` is native Ares multi-process integration. The native coding-worker lane's historical C4/C5 labels and a checkpoint lane's historical “C09” are different scopes. Neither a native-worker test pass nor a checkpoint receipt closes master C09.

Use exact source/command/input/artifact bindings to reconcile retained evidence. A typed case result requires the actual subchecks and outcomes. All remaining required subcriteria retain their force; optional auth/environment deferrals need explicit owners and must not become “all repaired.”

## 5. Implementation sequence

Every code task follows: inspect the exact current caller/callee → add a behavioral negative/positive witness → record genuine RED → make the smallest owner-correct change → record GREEN → run adjacent/canonical gates → freeze source and review. The commands below are **prospective implementation gates**, not tests executed by this planning PR. Run them only in admitted isolated worktrees with neutral test homes and the repository's supported dependency environment.

### R0 — Admit one current-main implementation source

**Owners/files:** Git/worktree and existing qualification owners; `scripts/ci/reliability_qualification.py`, `scripts/ci/reliability_completion_projection.py` on the candidate; current runtime descriptor and package manifests.

1. Fetch all three owner repositories and snapshot the exact base/head pair, dirty/untracked state, complete main-relative changed paths, relevant PR bodies/reviews/checks and inherited source hashes.
2. Preserve old candidates. Choose an existing #168-based implementation continuation or a current-main slice derived from it; record the decision and exact authored inputs. Never assume this docs branch contains implementation code.
3. Reconcile current `main` plus #154's overlapping runtime-option work. Preserve both `config.set` runtime-option handling and PDF/Stop admission; preserve cold-replay summary markers, completed-summary terminal/fallback handling and Codex sidecar accounting. Preserve #167's session-bound OpenCode affinity across auxiliary calls, retries and summary exits; inspect `agent/auxiliary_client.py`, `agent/chat_completion_helpers.py` and `agent/opencode_affinity.py` and carry its three `test_opencode_*` regression families into the composite selection.
4. Reconstruct the candidate in disposable indexes and inspect actual old/incoming/composite blobs. Compare the complete main-relative denominator, not only the top stacked PR's diff.
5. Admit the source packet/hash relationship and reconcile each inherited case with literal evidence. Classify regression rows as candidate-caused, baseline, environment-sensitive, repaired on a later exact head, or still unknown.

**Gate:** exact source/lock/toolchain/import custody, complete obligation coverage and a declared landing topology. No dependent implementation or ready label on missing/ambiguous owners. Hash/source checks validate planning custody, not application behavior.

### R1 — Finish attachment publication and PDF/control ownership first

**Modify:** `tui_gateway/server.py`, `tui_gateway/methods_prompt.py`.

**Tests on the carrier:** `tests/tui_gateway/test_attachment_publication.py`, `test_attachment_staging_integrity.py`, `test_pdf_dispatch_responsiveness.py`, `test_stop_input_order.py`, `test_session_images_dir.py`, `test_attach_does_not_wait_for_agent.py`.

1. Reproduce the current-main same-reader control blockage and same-timestamp/two-session alias cases with generated neutral media and explicit barriers.
2. Reuse #166's later publication contract: private occurrence staging, closed/0600 completed files, no-replacement publication, bounded collision retries and refusal on non-collision link failure. Preserve inherited flat public image/file/clipboard path/ref behavior; private PDF batch staging remains under its own queue/publication owner. Keep already-in-workspace files zero-copy.
3. Retain prequeue record/transport/stored-key/profile/Stop-generation binding, one owned per-session PDF reservation, queued-start expiry and owner revalidation before conversion and publication. Pool refusal, converter failure and stale ownership must not publish partial pages or delete earlier/peer inputs.
4. Preserve the distinction between Stop invalidating publication and actual converter termination. Do not report physical cancellation when the existing converter may still run to its bounded timeout; retain the reservation until that worker settles.
5. Exercise real Poppler success separately from held-converter control races. Validate the backend/renderer/file-serving references, package layout and supported Windows/macOS publication behavior where claimed.

```bash
scripts/run_tests.sh -j1 --file-retries 0 --file-timeout 180 \
  tests/tui_gateway/test_attachment_publication.py \
  tests/tui_gateway/test_attachment_staging_integrity.py \
  tests/tui_gateway/test_pdf_dispatch_responsiveness.py \
  tests/tui_gateway/test_stop_input_order.py \
  tests/tui_gateway/test_session_images_dir.py \
  tests/tui_gateway/test_attach_does_not_wait_for_agent.py -q
```

**Exit:** no alias/partial publication, correct inherited path/ref shape, responsive controls, bounded/observable worker ownership and exact current-head adjacent qualification. Poppler CI installation changes require the repository's real human CI-sensitive review; a specialist report is not that review. This slice may be landed independently only with its scoped acceptance and normal release gates; it does not close all media/custody cases.

### R2 — Requalify launch policy, bounded reads and LSP without security regression

**Modify:** `hermes_cli/config.py`, `hermes_cli/env_loader.py`, `tools/env_passthrough.py`, `tools/environments/local.py`, `tools/file_operations.py`, `tools/patch_parser.py`, `agent/lsp/{client,manager,workspace}.py` and the existing package export.

1. Reproduce malformed/missing-policy remap and wrong-profile/epoch refusals with neutral sentinels. Reuse typed launch-local policy and #165's identical-empty-publication epoch fix; never borrow root/sibling secrets or silently turn failed policy reads into empty valid policy.
2. Exercise single-pass reads through the real environment/file owner. Match complete returned content, encoding, redaction, pagination, truncation, error and exact/unknown total-lines behavior. Preserve special-file guards, allowed CWD/symlink and remote-host semantics; bound streaming memory and scan deadlines.
3. Measure copy/registry/backend-launch counts on the same fixture before/after. Collect nonoverlapping phase/lock spans and resource state; historical timings are not a new production speed claim.
4. Bound cold/repeated LSP failure and preserve mandatory syntax checking. Reuse the later post-resolution failure identity; actual executable/root changes must permit recovery. Missing diagnostics stay degraded, not “validated.”

**Carry-forward test required:** `tests/tools/test_remediation_read_concurrency.py` exists only in the retained local source, not current main or the inspected public stack. Port its reviewed neutral fixture as an actual committed test (or explicitly document an equivalent renamed behavioral target) before running the command below. This is a required new carry-forward target, not permission to omit a missing test.

```bash
scripts/run_tests.sh -j1 --file-retries 0 \
  tests/tools/test_launch_policy_snapshot.py \
  tests/tools/test_local_read_single_pass.py \
  tests/tools/test_local_env_blocklist.py \
  tests/tools/test_remediation_read_concurrency.py \
  tests/agent/lsp/test_enrichment_budget.py \
  tests/agent/lsp/test_broken_set.py -q
```

**Exit:** valid-policy environment equality, zero spawned effects on refusal, bounded read/lookup work, correct result contracts and real LSP recovery controls. Decide absent source/test paths at R0; never silently omit them from the requested selection.

### R3 — Close lifetime, retirement, goal and manual input-recovery races

**Modify:** `tools/delegate_tool.py`, `tools/async_delegation.py`, `tools/code_execution_tool.py`, `agent/subagent_lifecycle.py`, `agent/background_review.py`, `run_agent.py`, `hermes_cli/{delegation_policy,goals}.py`, `gateway/run.py`, `tui_gateway/{server,compute_host,host_supervisor,methods_prompt,methods_tools}.py` and the existing desktop recovery/goal projections.

1. Freeze the effective deadline policy at admission, with distinct queue, setup, attempt, meaningful-progress, client-wait and settlement budgets. Initialization, initial conversation and schema repair receive remaining budget, not a reset. Reject invalid/bool/nonfinite/unknown values before effects; distinguish child-origin errors from owner wait expiry.
2. Reuse #163's child-owner locking/retirement and #165's budget/epoch corrections. Deny new work on closing owners; retain DB/credential/capacity/registry ownership while execution or cleanup is live/unknown. A returned Future, dead leader, terminal row or zero visible slot count is not physical settlement.
3. Preserve partial output/RPC/artifacts on failure and lazy dispatcher creation only after admission. Never kill a shared provider client/pool or a sibling operation to make one timeout look settled.
4. Test pause/Stop/clear against late checkpoint writes, queued continuation, notification, replacement goal, restart and schema failure. Preserve cumulative budgets and exact generation/CAS consume; a scheduling resume is not completion approval. A wake suppressed by authority/block/budget must not mint a judge call or counted turn.
5. Keep prompt recovery manual and receipt-aware. Preserve accepted-not-started versus started/unknown/Stop/re-home distinctions, exact frozen content/attachment identity and event-ID conflicts. No automatic effect replay.

**Lifetime policy decision:** current main's helper defaults to no timeout; the existing stack describes a `0`/disabled default, while earlier operational contracts preserved a configured 600-second cap. Source default, explicit configuration, inherited admitted policy and a new policy choice are different facts. Record their actual values/origins. Do not change a configured 600 to 0, reinterpret total as inactivity, or weaken a native worker's separate cap by inference. Preserve admitted work's policy. Any default/mode change needs an explicit compatibility decision and its Stop/stall/containment gate; this plan does not select one silently.

```bash
scripts/run_tests.sh -j1 --file-retries 0 \
  tests/tools/test_delegate_attempt_deadline.py \
  tests/tools/test_delegate_deadline_validation.py \
  tests/tools/test_delegate_settlement_contract.py \
  tests/tools/test_agent_retirement_guards.py \
  tests/tools/test_code_timeout_progress.py \
  tests/hermes_cli/test_goal_exhausted_evaluation.py \
  tests/hermes_cli/test_goal_lifecycle_contract.py \
  tests/tui_gateway/test_prompt_recovery_contract.py \
  tests/tui_gateway/test_compute_host_phase1.py -q
```

**Exit:** deterministic before/after-admission controls, persisted pause/budget semantics, retained uncertainty/capacity, sibling survival, late-write/lease ordering and real subprocess/pipe-death coverage. If required hard containment is unavailable, keep the affected topology/policy blocked; do not promise forceful thread cancellation or remote exactly-once effects.

### R4 — Preserve independent catalog routes and reconcile #154 options

**Modify:** `hermes_cli/{inventory,models,model_switch}.py`, `tui_gateway/{methods_config,selector_snapshot,compute_host}.py`, and existing desktop model menu/visibility/option stores and hooks.

1. Retain distinct connection/profile/provider/endpoint/API-mode/credential-scope/opaque-ID routes. No name-only cross-provider subtraction, invented catalog entry, case normalization or endpoint retargeting.
2. Test mixed ID grouping, explicit hide/show and ordering, last-known-good refresh, pagination/delisting proof, failed/foreign-scope refresh, reopen/restart and auxiliary picker parity. Original named model targets require a current authoritative catalog observation or explicit delisting disposition; their old names do not prove paid inference availability.
3. Compare shared paths with #154 at its exact refreshed head. Preserve explicit reasoning/Fast-off intent, independently applied/deferred/rejected/uncertain options, supported-model wire fields and replacement/retirement guards. Close review findings at the actual current source, not by resetting valid guards for old fixtures.
4. Record actual backend ACK, stored preference, renderer observation and next serialized request separately. Do not edit the operator's model defaults to test the path.

**Tests:** the carrier inventory/catalog/provenance/discovery suites and desktop model-visibility/goals/menu tests; #154's option-owner, capability-refresh, runtime-options-pending, renderer recovery and isolated native-selector workflow. Inspect supported package scripts before selecting Vitest/Playwright arguments.

**Exit:** route-preserving discovery and options survive current real host reconstruction and serialized request checks. Mock inference/native UI tests do not establish live provider access or production containment.

### R5 — Complete media/final-wire/reader contracts under their existing owners

**Modify:** `agent/context_input.py`, `hermes_state_{inbox,input_turns,input_media,continuity}.py`, `ares_runtime/continuity/{budget,dispatch}.py`, existing provider serialization/projection/backup owners and their tests. Resolve an absent or moved path at R0 before changing it.

1. Preserve closed legacy V1 readers. Version media/origin changes; distinguish mutable convenience staging, immutable original intent/custody, derived provider-transformed bytes, durable ACK and execution permission.
2. Bind original bytes/order/validated MIME/length/dimensions/text intent, profile/root and observed original owner epoch. Current execution authorization is checked separately. No legacy origin fabrication, arbitrary-URL fetch on recovery or path-as-immutable-identity shortcut.
3. Test quota reservation, publish/DB-commit/ACK crash order, duplicate/reference ownership, symlink/replacement/tamper, cross-profile refusal and failed cleanup. Define process-crash versus power-loss durability honestly.
4. Count/seal the final provider wire request only under a route/model/detail/transform-qualified method, including history/tools/media and reserves. Heuristic/API/catalog/byte-length estimates are not automatically exact upper bounds. Late transformation invalidates admission; unavailable support retains input and refuses before effects.
5. Prove old/new reader, backup-plus-reachable-media manifest, conservative blocked-goal backout and neutral restore. A database restore over newer production history is a separate decision, not routine rollback.

```bash
scripts/run_tests.sh -j1 --file-retries 0 \
  tests/ares_runtime/test_context_media_custody.py \
  tests/ares_runtime/test_context_media_stop_origin.py \
  tests/ares_runtime/test_final_media_budget.py \
  tests/ares_runtime/test_reliability_backout_contract.py \
  tests/tui_gateway/test_native_input_recovery.py -q
```

**Exit:** qualified supported forms/routes, exact intent/ACK/readback identity, compatible backout and no false claim of power-loss or universal multimodal support. Do not remove the text/media refusal guard to bypass an unresolved contract.

### R6 — Qualify and land the actual Ares composite, then admit rollout separately

**Owners/files:** current CI classifiers/workflows, manifests/lockfiles, existing qualification/projection tools, all changed test families and the managed runtime controller.

1. Reconcile each recorded RG regression row against exact candidate and main under the same admitted environment. Preserve earlier failed runs. A pass on a later parent or a skipped top-PR job is not a pass on the chosen main-relative composite.
2. Audit every changed existing/new test against the actual final diff. Missing/zero-collection/timeout/teardown failures cannot disappear into a green summary or a smaller hand-picked cohort. Run required full CI and native host lanes; no retry-only reliability closure.
3. Preserve the landed #164/#160/#161/#167 behaviors on all overlapping paths, including semantic checks on `compute_host.py` and `server.py`. Do not reuse old packaging/CI pins without proving their current role and ancestry in the correct owner repository.
4. Recheck dependency reachability, root/nested copies, build scripts and package integrity. #158's landed dependency repairs are not an invitation to replay stale overrides. Do not use force upgrades, suppress advisories or call an audit listing exploitability proof.
5. Build/package from the exact chosen source, without registry publication. Validate wheel/module inclusion, native dependencies, renderer/backend/compute-host/SQLite seams, current route/goal/manual-recovery restart cases and no owned orphan. Preserve compatible public attachment refs from R1.
6. Obtain real maintainer CI-sensitive review where required, resolve actual current review threads, pin the landing head/base and verify the published main content. Preserve authored history; if collapsing the existing linear stack, require actual target ancestry of every lower head and workflow pin before any separately authorized closure.
7. Only then prepare managed source-bound activation: current controller/descriptor compatibility, adequate staging space, consistent backup/media custody, protected configuration preimages, settled work, exact rollback target and separately authorized outage/canary boundaries. Never manually repoint release links or kill unrelated active work.

**Native integration gate:** `apps/desktop/e2e/reliability-native-restart.spec.ts` on the carrier, existing submit/selector and Stop/input/goal workflows, real temporary authenticated backend/compute host and SQLite. Use the exact built artifact; source simulation is not installed adoption.

**Exit:** selected implementation scope has case-bound source/package/CI evidence. Master C10 costed provider canaries, C11 optional operational dispositions, C12 managed activation/backout and C13 final evidence challenge remain separate gates. Prior scope/merge authority must be matched to the actual candidate/effects; this plan neither grants paid calls nor repeats already valid approvals. Installed identity alone is not end-to-end behavior or endurance proof.

### R7 — Publish the narrow Graph protocol repair independently

**Repository/base:** `RecursiveIntell/agent-graph-mcp` at `720f06055b0bb8b301cbbdd9969aea664592d03d`; refresh before implementation. **Modify:** `src/nodes.rs`. **Tests:** its ToolNode tests and `tests/tool_runtime.rs`.

1. Reproduce top-level JSON-RPC error versus MCP `result.isError` handling with real local stdin/stdout fixture brokers and zero LLM calls.
2. Reuse the retained narrow validator only after current-source review. Validate version/request-response correlation, exactly one result/error branch, object/array shape and boolean `isError`; error/malformed results publish neither success nor accepted result. Preserve valid empty output and the supported text projection; do not infer domain errors from words in ordinary text.
3. Exercise stalled/EOF/handshake/read cancellation separately. The initial protocol fix does not prove every synchronous read is bounded or every underlying request is physically stopped.

```bash
cargo test --locked --lib
cargo test --locked --test tool_runtime
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

Run the repository's declared dependency/advisory and daemon integration gates before its own release. The observed live Graph build is dirty at `d709316…`; its source fingerprint is not a deployment certificate for the retained patch. Publish/review and stage the proxy and daemon under their service owner, preserve the real store/rollback and in-flight work, then read back binary and behavior identity. Do not replace this with an Ares restart or widen the supplemental broker. This lane does not block an independently qualified Ares repair and does not claim model-backed coding or restart-resumable arbitrary work.

### R8 — Keep native coding-worker development separate and fail closed

**Repository/base:** `RecursiveIntell/recursive-agent` at `88e121e479e48accfbf361ad9d392042ef4d7eda`; refresh before adoption. **Candidate owners:** `crates/recursive-agent-{contracts,ledger,runner}`, including the bounded coding-worker runtime/sandbox and execution/runtime tests.

The retained C4 increment is a default-disabled fixed-plan/execution-stage Stop prototype, not a general model coder, immutable consumed-input proof, complete preparation/repair Stop or process restart/uncertainty qualification. The installed daemon observed in the relevance assessment is still sourced from `11fefae…`; Ares has no integration with the new coding-task family at the inspected main.

**Rejected method:** sealed input bytes plus a read-only leaf overlay under an externally mutable shared workspace did not pin lexical source paths. A host-side atomic replacement after a real namespace barrier let the worker write the replacement. Keep the rejected C5 variant and its evidence quarantined. A timeout, partial code write, reviewer suggestion or ordinary Stop success cannot override that counterexample.

Before any future adoption, admit a native-owned private/copy-on-write workspace and explicit output verification/publication contract that preserves legitimate in-place coding. Version identity/schema/permission transitions and bind real source/dependency/executor provenance, task/operation/permit IDs, input/output artifacts and budgets in the existing ledger. Require host replacement/parent changes, source-output alias/in-place edits, preparation/execution/repair Stop, duplicate/late result, process crash/restart, cleanup uncertainty retaining capacity, and independent output acceptance witnesses. Only an actually integrated consumer and matched useful-work qualification can justify a general Graph-backed coding claim. Do not implement this through a generic delegate wrapper or a shadow task/acceptance store.

This is a parked design/qualification lane, not a prerequisite for R1–R6 or a feature to enable by merging this plan.

## 6. Master-card and context disposition map

| Master card | Current reconciliation owner | Required disposition |
|---|---|---|
| C00 source admission | R0 | Fresh main/carrier/caller ownership; preserve newer context fixes. |
| C01 evidence/gates | R0, R6 | Current case/subcheck bindings; no count/title-based acceptance. |
| C02 input/media/origin | R1, R5 | Staging is not custody/ACK/authority; qualify supported forms. |
| C03 final-wire media budget | R5 | Keep unqualified forms refused and original intent retained. |
| C04 lifetime | R3 | Explicit source/config/admitted policy and compatibility decision. |
| C05 containment | R3 | Owned settlement/charged uncertainty, not Future/thread cancellation claims. |
| C06 dependency reachability | R6 | Preserve landed repairs; current actual distribution/call-path disposition. |
| C07 readers/backup/backout | R5, R6 | Safe older reader and consistent reachable-media restore. |
| C08 package/performance | R2, R6 | Exact package, complete changed-test denominator, controlled measurements. |
| C09 native Ares integration | R4, R6 | Actual multi-process route/input/goal/Stop/restart seams. |
| C10 real-provider canary | R6, separate cost gate | Explicit target/data/budget/Stop plan; no simulated-response substitution. |
| C11 optional operations | R6, operational owner | Repair or explicit defer; no credential/radio/provider side effects. |
| C12 activation/backout | R6, managed owner | Exact published source, protection/backup/rollback and live adoption. |
| C13 final closure | R6 | Every required obligation and residual has source-bound evidence and owner. |

Context tasks T01–T02 map to R0; T03–T04 to evidence attribution/copy preservation in R0/R6; T05–T07 to typed refusal, goal containment and owned lifecycle in R3; T08–T11 to preservation/requalification of current context, checkpoint/review-fork and exact replay behavior in R0/R5/R6; T12–T14 to current cold-host compression/budget/activation recovery in R5/R6; T15–T16 to the real-source regression/copied-session gate in R6; T17 remains the separately admitted live canary. #157/#160/#164 address portions of this area, not automatic closure of all CR subcriteria. Preserve fixed-budget failures and conditional “reproduce first” gates; do not widen the target or normalize authenticated bytes to obtain PASS.

## 7. Rollback, stop rules and completion

- **Source:** preserve dirty carriers; apply only reviewed hunks/commits in isolated owners. Reverse this implementation increment only after matching source hashes and settling owned work. Do not reset another writer's tree or copy full files over newer main.
- **Input/state:** preserve accepted intent, epochs, receipts, referenced media and unknown effects. No destructive transcript rewrite, lossy media-to-text migration, automatic retry or live database restore to hide a failure.
- **Publication:** this planning PR does not supersede or close the code stack. Later implementation PRs retain exact predecessors, base/head/source scope, current checks/review and tested bytes; no blanket force push or bypass.
- **Activation:** use the actual managed owner and tested descriptor/backout contract. Revert code through history or invoke admitted managed rollback; do not stamp new custody onto an old artifact or alter selected pointers manually.
- **Failure:** source drift, wrong imports, policy/authority ambiguity, unsafe cleanup, missing evidence or a failed mandatory check stops its dependent lane. After two distinct failed methods, classify and change method/quarantine rather than spending indefinitely on the same proposal.
- **Completion:** planning validation, source implementation, artifact integrity, task acceptance, hosted CI, merged source, selected release and exercised runtime behavior remain distinct. No global completion while a required case is failed/unknown/missing; optional deferrals never become repaired services.

## 8. First implementation handoff

Start at R0 with the refreshed PR stack and source snapshot; do not create another root plan, resurrect predecessor goals or launch parallel writers on shared files. Then implement/requalify R1 using #166's current contract and R2/R3 as independently scoped owners permit. Resolve #154 overlaps before the composite R4–R6 gate. R7 is separate; R8 stays parked pending its design and qualification.

The accompanying source/coverage files are reviewed planning projections, not runtime acceptance ledgers. This PR runs document/link/identifier/source-snapshot checks only; it does not claim a fresh application test pass, an implementation merge or runtime activation.
