# Recovered session/control repair checkpoint — 2026-09-28

**State: source work preserved; overall repair incomplete; combined Ares activation not qualified.**

This is a rebuildable recovery projection of the existing P00–P18 plan, its 39 acceptance cases, seven decomposer assignments, Graph prerequisite repair, and C40/C41 incident additions. It does not replace their acceptance contracts or turn imported source into completed tasks.

## Snapshot and preservation

- Ares candidate: `0da95d9efa3d15978a88f29a3ff01596e44e33de`, [PR #85](https://github.com/RecursiveIntell/Ares/pull/85), based on deployed source `ad547c6c4a31adf9a848d264734d2ec3cea90651`.
- Graph source: `13591c88bbcffcb5069d7e356a34ac02cf53b1f5`, [PR #2](https://github.com/RecursiveIntell/agent-graph-mcp/pull/2).
- Governor test repair: `34fa0719ee84b01ba268b5849c2455ac8159ef49`, [Libraries PR #37](https://github.com/RecursiveIntell/Libraries/pull/37), merged into main as `c77273a808242c8159288ef5cf04a0a50ba6e808`; exact feature ancestry verified.
- The interrupted session's stored messages, user requests, prior final responses, native goal, task definitions, receipt links, source snapshots and incremental Git bundles were preserved privately. Private conversations, credentials, profile configuration, live databases and execution logs are not part of this commit.
- The native goal is paused after repeated compression exhaustion. Its older nested checkpoint asks for continuation; that does not override the current paused state. No old-goal resume, clear, budget reset or invented epoch occurred during recovery.

## Recovered published increments

| Commit | Preserved work |
|---|---|
| `1f9a35090e7` | Dependency-complete request/Stop ownership, hydration and partial renderer repair checkpoint |
| `00e59f664bd` | Stale option-target rejection and host-owned reasoning readback |
| `8ab72cbbf03` | Idle reasoning writes forwarded to compute host |
| `7301002350d` | Goal CAS and host-observer fixture reconciliation |
| `5543a316386` | Durable goal creation and early Stop admission fixtures |
| `ac5bee004dc` | Provider catalog and TUI readiness fixture reconciliation |
| `4faa1a23331` | Option writes moved off the connection reader |
| `09f94a47e17` | Block estimated-over-window dispatch after no-progress preflight |
| `fba50fd353a` | Respect Rust owner's omitted epoch-zero wire default |
| `025033e4f01` | Governor prefix diagnostic follows production operation ordering |
| `015ab74a3bf` | Warning fixture remains under model window; exhaustion safety assertion retained |
| `b46257f219e` | Reject expired queued option writes before execution |
| `10281af78a2` | Fresh host readback and session-scoped composer model picks |
| `d01885a5f16` | Reconcile recognized reasoning/Fast uncertainty by readback, not write replay |
| `0da95d9efa3` | Pin the merged Governor test-only refusal-race repair in focused CI |

## Evidence boundaries

- Retained controller evidence at `d01885a5f16`: **6,174 passing renderer tests across 622 files**. The receipt's complete 11-file increment manifest and log hash match current bytes. This is reused evidence, not a new recovery-session rerun. The earlier full-UI timeout remains an incomplete attempt.
- Hosted checks observed at that same head: **47 successful, 1 failed, 14 skipped, 1 neutral**. The failure was the native Governor dependency test path; the current one-line pin is intended to address its test-harness race. The new-head downstream qualification must be inspected separately; earlier CI is not transferred to a new SHA. Headed Desktop E2E was skipped, not passed.
- Libraries PR #37 had **14 successful hosted checks** before merge. The retained local record reports 198 passed and one ignored release-only certification test, with formatting/Clippy passing; those logs were hash-verified. Recovery freshly reran the exact early-refusal regression: **1 passed, 7 filtered out**. This is a test-only change, not an epoch-continuation fix or binary activation.
- Older Ares backend selections overlap. Later edits changed some recorded source hashes; their old passes remain historical rather than blanket current-head certification.
- The running Ares source observed during recovery is still `ad547c6c`. Candidate settings and exhaustion repairs are not claimed to be active.

## Graph prerequisite: preserve, do not redo

| Package | Recovered state | Remaining boundary |
|---|---|---|
| G01 — connection lifetime | Retained Rust qualification and release lifecycle evidence; all recorded Rust source bytes still match committed candidate | No universal connection/runtime guarantee beyond tested envelope |
| G02 — persistent reads/control | Maintained bounded readers and explicit submission helpers committed; later portable client/wrapper updates separately tested | New portable coding wrapper has no fresh real multi-worker batch qualification |
| G03 — evidence/admission | Truthful descriptive policy response, evidence-preserving analysis and controller admission safeguards retained | Graph output cannot grant operator authority or independently accept code |
| G04 — qualification/cutover | Historical frozen qualification logs verify: 286 Rust tests, 8 release-lifecycle tests, 59 release-pair read-path tests; overlapping counts. Prior activation/canary receipt retained. Recovery verified live daemon/proxy hashes equal those qualified artifacts | Graph PR has no exposed hosted checks. Four Python/support paths changed after original qualification; later publication evidence does not certify a fresh portable coding batch. No new Graph activation occurred here |

Graph is a controller-mediated advisory prerequisite, not a native Graph-to-coding dispatcher. Cancellation remains provider-best-effort and restart interruption is not generic resumability. Do not reopen the whole Graph repair solely because the old planning document says implementation had not started.

## Complete UI task denominator

The status column distinguishes historical preparation, a bounded verified increment, and full task acceptance. No percentage-complete estimate is inferred from task counts.

| ID | Task / recovered state | Remaining gate |
|---|---|---|
| P00 | **Freeze input bytes and establish one integration owner** — verified historical preparation. Source/donor freeze preserved; recovery saved current HEADs, the uncommitted patch and verified incremental Git bundles. | Recheck source ownership before each new writer. |
| P01 | **Install the reusable regression baseline** — verified historical baseline. Imported backend/renderer negative witnesses and retained RED logs are present in the repair lineage. | Do not rerun old RED expectations as if the repaired candidate were the baseline. |
| P02 | **Port host request identity and settlement first** — bounded source gate accepted; full acceptance open. Host-specific request IDs, SID/request/boot terminal fences and exact settlement are integrated. Retained independent review accepted this source gate; selected tests reported 26 passes. | Complete the end-to-end zero-mutation oracle and reconstruction combinations; P04 remains separate. |
| P03 | **Close adopted-host hydration without a phantom builder** — implemented; integrated/headed acceptance open. Adopted-host display hydration now schedules its producer without building a parent agent; original selected evidence reported 13 hydration checks and a broader 798-test backend selection. | Qualify cold/adopted/history-failure behavior in the integrated and headed paths. |
| P04 | **Make adoption and observer lifetime one fenced transition** — open. Exact-request observers are imported, but no recovered receipt closes the complete adoption lifetime gate. | Terminal-before-observer, abort-after-dequeue and old-boot races need explicit proof/repair. |
| P05 | **Preserve current-release and opaque-route switch guards** — partially integrated; acceptance open. Current-release model/provider reconstruction and donor guards were preserved in the combined source. | Qualify enrolled-root refusal across immediate, deferred, config-sync and reconstruction paths without losing legitimate opaque routes. |
| P06 | **Port host-owned option writes and readback** — partially implemented. Stale option targets reject before global writes; idle reasoning writes and fresh reasoning/Fast readback use the compute-host owner. | Busy-turn behavior, next-invocation consistency, persistence failures and provider-level readback remain open. |
| P07 | **Finish a coherent model/options intent contract** — open. Existing settings endpoint and owner readback are available; this is not a complete compound intent contract. | Validate the whole model/effort/Fast tuple, preserve confirmed state, and distinguish pending, applied, rejected, unknown and partial outcomes. |
| P08 | **Repair live-slice updates and intent-aware rollback** — verified increments; task open. Live-slice and stale-callback fences plus recognized reasoning/Fast uncertainty readback are present; full renderer selection at d01885a5f16 passed 6174 tests in 622 files. | Overlapping rejected intents must restore owner-confirmed state; model-switch uncertainty, generic/partial errors and same-ID incarnation remain open. |
| P09 | **Connect compound outcomes and explicit scope UI** — partially implemented. Composer model picks use session scope; explicit Settings defaults remain separate; unresolved option readback has visible warning state. | Complete compound, running-versus-next-turn, pending/rejected/unknown presentation and old-backend capability handling. |
| P10 | **Separate history display, attachment, and execution readiness** — open. Hydration backend prerequisite and existing renderer source are preserved. | Prove independent display/attachment/execution readiness, owner-bound progress, degraded/exhausted states and usable Stop during reattachment. |
| P11 | **Integrate targeted Stop, steering and chain presentation** — source imported; acceptance open. Dependency-complete targeted Stop, steering and goal-chain presentation were included rather than stubbed. | Qualify cancellation settlement separately from acceptance, preserve post-cut input, and reject parent-agent fallback on uncertainty. |
| P12 | **Qualify durable Stop cut and data-format rollback** — source imported; activation blocker. Canonical accepted-input cancellation/Stop receipt source is included. | Real DB commit-order proofs and cancelled-V2 old-reader/rollback compatibility are required before activation. |
| P13 | **Close goal lifecycle and accepted-input recovery** — partial source/fixture work; acceptance open. Goal CAS, creation and early-Stop fixtures were reconciled in published test increments. | Close durable goal races, FIFO/attachments/accepted IDs, selected continuation fences and inspect/resolve of dispatched uncertainty without replay. |
| P14 | **Adapt operator recovery UI to current canonical receipts** — open. The earlier recovery UI is a donor, not a replacement queue authority. | Adapt UI to current canonical unknown input/turn receipts after P13; closing a dialog must not imply resolution. |
| P15 | **Reuse Clarify visibility and reproduce answer delivery** — open. Earlier Clarify renderer work is preserved as a reuse input, not proof of answer delivery. | Trace exact request/session answer to owner and persisted result across reconnect, expiry and Stop; reuse only admitted renderer delta. |
| P16 | **Freeze and run one integrated failure matrix** — open. Broad local UI and hosted checkpoint tests exist; counts overlap and do not constitute the full acceptance matrix. | Freeze one candidate and execute all required negative, headed, provider-parameter and provider-behavior cases. |
| P17 | **Qualify packaging, platform scope and recovery-capable release** — open. No Ares candidate runtime activation or data-compatible rollback qualification is recovered. | Packaging/install identity, platform scope, static gates and rollback readability must pass; no cross-platform claim from Linux-only transport proof. |
| P18 | **Run an explicitly authorized pilot then close evidence** — authorized but not qualified. Operator now permits incremental merge/activation of qualified sections. The combined Ares candidate remains a draft. | Managed activation only after dependencies, durable format compatibility, rollback and pilot gates; verify live source and behavior separately. |

## Incident extensions

### C40 — session option timeout and requested executing settings: OPEN

The candidate now rejects stale targets, forwards idle reasoning writes, keeps writes/readback off the connection reader, expires unstarted queued writes, reads the actual host, scopes composer changes to the session, and reconciles recognized reasoning/Fast uncertainty without replay.

Still required: busy-turn and compound outcomes, confirmed rollback under multiple rejection, model-switch uncertainty, full incarnation fences, and provider-level proof of the requested model/reasoning effort. A readback fixture does not prove the real executing provider tuple.

### C41 — exhaustion and continuation: OPEN

The recovered native goal explicitly records repeated compression exhaustion. Read-only classification of the latest retained errors for this session found five successive `compact-v2` refusals with `lineage_integrity_mismatch` immediately preceding provider context-window errors and `Cannot compress further`. **The observed stop is integrity refusal, not `lineage_generation_limit`; epoch rollover must not be used to bypass it.** The exact at-the-time live process/host-projection divergence behind those refusals remains unproven. The nested continuation checkpoint is historical and not permission to revive the paused goal.

The candidate includes no-progress estimated-over-window dispatch containment and the Rust-defined omitted epoch-zero adapter correction. Historical real-binary tests covered epoch/restart behavior. The earlier copied-state lifecycle passed generations 2 and 3 for **another session**, so it did not certify this incident.

A subsequent **read-only diagnosis of this historical session's current replay** identified a verified generation-4 tip. Its raw and merely todo-normalized replay prefixes differ, but the production-ordered todo normalization **and authenticated parent-prefix reconciliation** match the complete tip. On a consistent SessionDB and four-receipt ancestor-chain copy, `compact-v2`, finalization, preparation and activation then reached verified generations 5 and 6 without provider calls. Independent readback compared all 3,906 original message rows unchanged, confirmed unchanged source-chain hashes, zero pending receipts, SQLite `quick_check=ok` and zero foreign-key violations. The second scratch generation uses an explicit synthetic continuation suffix. These are **copy-only** results: they neither reproduce the historical live integrity refusal nor prove its at-the-time cause, do not create an epoch, and do not recover, resume or activate the paused live session.

Still required: compare the failed live process's exact projection/adapter state against the authenticated tip at its failure boundary without rewriting it; qualify the managed runtime/process cutover and then verify a real live continuation plus subsequent restart and ordinary generation with authentic input, goal budget and unresolved effects. Test the generation ceiling and owner-safe epoch transition only if that typed ceiling is actually reached; never use an epoch to bypass an integrity refusal. Do not enable physical rebase, raise a generation ceiling, delete receipts, or fabricate parentage merely to remove the error.

## Decomposer assignments retained, not seven new jobs

- **W01 (P02)**: Produce a bounded backend port-contract proposal identifying donor reuse, boot/request/session authority, and transitive coupling before assigning the single backend integration writer. Resolve ownership of physical admission in server.py and preserve current model reconstruction. Do not design full Stop behavior or a new queue.
- **W02 (P08)**: Produce the missing owner/outcome contract proposal for confirmed rollback baselines, same-ID incarnation authority, and unknown transport outcomes. Specify how acknowledged, explicitly rejected, deferred, and unknown outcomes differ; identify exact-owner readback prerequisites without implementing them.
- **W03 (P02)**: Prepare one focused parent-settlement regression showing that a terminal for the wrong SID or request cannot mutate the active parent owner, busy state, history, or queue, while the matching terminal settles once. Use the existing parent callback seam; do not wait for supervisor implementation.
- **W04 (P08)**: Extend the existing hook authority suite with overlapping explicitly rejected model choices: confirmed A, optimistic B, optimistic C, then rejection of both. Assert that final model/provider and associated selection state return to confirmed A rather than the optimistic B snapshot.
- **W05 (P08)**: Add a remaining overlapping-rejection witness for direct runtime option changes in the existing UI audit suite: two optimistic choices for one dimension both explicitly reject, and the final live value must equal the last confirmed owner value.
- **W06 (P08)**: Prepare a focused preset regression for two overlapping explicitly rejected preset applications. Verify restoration of the confirmed effort/Fast tuple, including a painted Fast value whose write was never sent, without repeating the already-passing single-preset rejection case.
- **W07 (P08)**: Extend the hook authority suite with the unresolved stale-success/recovery authority matrix: a same-ID incarnation replacement, late success from its predecessor, and recovery completion after a newer intent. Verify that stale continuations cannot repaint, invalidate a replacement owner's cache, or send a recovered model mutation.

W01/W02 advisory work and W03-related parent-settlement source/tests have historical artifacts; their existence is not acceptance of every assignment obligation. W04–W07 remain governed by the P08/P09 gaps above. Re-admit against current bytes and dependency/write ownership before dispatch.

## Original 39-case acceptance denominator

These are the original oracles, not 39 claimed passes. Some have partial fixture evidence; final integrated acceptance remains P16. C40/C41 are separate incident extensions.

| ID | Owner task | Case | Required oracle |
|---|---|---|---|
| C01 | P03 | `cold_history` | Cold load completes model/display projections once; positive control stays green. |
| C02 | P03 | `adopt_idle_host` | Surviving idle host plus absent parent yields no unset event without producer; approval poll never constructs a phantom agent. |
| C03 | P04 | `adopt_running_host` | Resume retains host/request/boot and Stop availability while transcript independently loads. |
| C04 | P03 | `history_error_retry` | Controlled reopen/read/projection errors end loading honestly; one bounded retry cannot mint duplicate owners. |
| C05 | P03 | `history_close_supersede` | Real close/reap/supersede settles waiters with cancellation, never fake empty-history success. |
| C06 | P06 | `stale_reasoning_target` | Supplied unknown runtime returns refusal before profile/config write; only proven refusal permits one stored-owner recovery. |
| C07 | P06 | `warm_host_reasoning` | Owner ACK/readback and captured next provider request agree on selected effort; parent shadow does not win. |
| C08 | P06 | `explicit_off_normal` | none/normal survive default high/priority, resume and reconstruction as explicit values. |
| C09 | P08 | `primary_tile_state` | Primary and tile each paint their live runtime slice; draft state changes only by own explicit intent. |
| C10 | P08 | `overlapping_model_failure` | B pending, C success, late B failure cannot revert C or its preset. |
| C11 | P08 | `cross_session_option_failure` | A request fails after navigating B; B draft/live state stays unchanged. |
| C12 | P08 | `preset_rejection` | Explicit rejected preset restores confirmed owning tuple only if still current. |
| C13 | P09 | `mutating_ack_loss` | ACK loss is unknown, not rejected: no resend, no guessed rollback; exact owner readback resolves or exposes recovery. |
| C14 | P07 | `busy_compound_selection` | Busy model+effort+Fast selection stays a coherent pending next-turn tuple; current provider request is not mutated mid-flight. |
| C15 | P07 | `invalid_compound_selection` | Invalid capability/model combination changes neither live/persisted settings nor defaults; disabling inherited unsupported Fast remains possible. |
| C16 | P07 | `partial_exception` | First/second option returned error or raised exception retains prior applied tuple or explicit unresolved state; no silent partial success. |
| C17 | P09 | `legacy_capability` | Explicit method-not-found compatibility is distinct from connection failure; busy legacy path cannot pretend compound apply succeeded. |
| C18 | P09 | `settings_scope` | This-chat, next-draft and profile-default actions have separate labels/effects; primary/tile location never silently changes write scope. |
| C19 | P09 | `catalog_recovery` | Empty/stale catalog is recoverable without restart and does not overwrite a valid manual/live selection. |
| C20 | P02 | `wrong_terminal` | Wrong SID/request/boot terminal causes zero owner/busy/queue/observer mutation; correct terminal settles once. |
| C21 | P04 | `terminal_before_observer` | Terminal racing observer registration is retained and reconciled exactly once. |
| C22 | P04 | `aborted_adoption_callback` | Callback dequeued before adoption abort performs zero publication/drain after abort; authoritative outcome remains recoverable. |
| C23 | P11 | `stop_ack_vs_settled` | Stop accepted but turn live retains busy/partial output; only matching settlement retires Stop. Rejection and unknown remain visible. |
| C24 | P11 | `stop_before_start` | Cancellation wins before admission/provider boundary: zero provider calls; late Stop cannot cancel successor request. |
| C25 | P11 | `full_pipe_deadline` | Blocked pipe/host startup/late ACK share a bounded control deadline; possibly offered outcome is never classified not-sent. |
| C26 | P12 | `stop_cut_new_input` | Before-cut accepted B and after-cut C obey the chosen Stop contract; failed proven-unsent cut restoration preserves order and C. |
| C27 | P13 | `goal_clear_handoff` | Cleared/paused goal cannot be revived by cached/deferred write or selected continuation; independent user input survives. |
| C28 | P13 | `accepted_input_parent_loss` | Acknowledged B survives parent loss with same ID/FIFO/attachment/goal identity and single admission. |
| C29 | P14 | `unknown_host_loss` | Possibly executed B after host loss requires exact inspect/resolve, never automatic replay; C/D remain ordered. |
| C30 | P13 | `missing_attachment` | Unavailable attachment/owner is explicit blocked input, not text-only downgrade or silent deletion. |
| C31 | P12 | `cancelled_v2_rollback` | Fresh/old readers, reopened cancelled V2 state and rollback rehearsal retain proof and post-snapshot input; no blind downgrade. |
| C32 | P14 | `projection_worker_failure` | Failure before/after terminal emission or worker start remains inspectable/recoverable without duplicate message/provider effect. |
| C33 | P05 | `opaque_route_guard` | Ordinary opaque replay remains allowed; enrolled root unsafe route refuses before mutation on immediate/deferred/config-sync/restart paths. |
| C34 | P05 | `cold_model_reconstruction` | PR83/ad547 positive control: pick before first host turn then reconstruct; readback and first captured request use the selected normalized tuple. |
| C35 | P15 | `clarify_visibility` | Question remains visible before request hydration; interrupted/expired card has no stale Continue effect. |
| C36 | P15 | `clarify_answer_delivery` | Actual answer reaches matching request owner; reconnect/ACK loss/Stop cannot turn acknowledged answer into skipped or duplicate it. |
| C37 | P10 | `navigation_streaming` | A->B->A/hidden pane streaming keeps transcripts, busy and selections with their owners; no route-mismatch loss. |
| C38 | P18 | `authorized_provider_canary` | After separate approval, record actual endpoint request model/effort/tier without secrets; provider outcome distinguished from synthetic capture. |
| C39 | P18 | `managed_pilot_readback` | After safe authorized activation, binary/source/backend identities align and user workflow passes; rollback remains data-compatible. |

## Next gates and rollback

1. Inspect exact-head downstream qualification after the recovered Governor pin. Do not equate the generic CI aggregate with the separate continuity workflow.
2. Keep a single backend writer; close P04's adoption/observer negative witnesses before claiming the request/hydration prefix independently activation-ready.
3. Admit the remaining settings/rollback work against existing contracts and receipts; preserve original donor work rather than recreating it.
4. Do not activate the combined Stop/input branch before cancelled-V2 reader compatibility, recovery UX, integrated/headed validation and rollback are qualified. The optional narrow pilot is not automatically safe merely because its intended features are earlier in the plan.
5. Continue with explicit-path commits and remote/PR readback at each qualified increment. Never force-push or combine unrelated work silently.

Rollback for this checkpoint is a normal revert of its documentation or CI-pin commits. The Libraries merge changes a test only. No data migration, live-store restoration, credential change, runtime symlink change or service restart occurred in this recovery checkpoint. Existing receipts and failed attempts stay intact.
