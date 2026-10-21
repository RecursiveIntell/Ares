---
name: profile-collaboration
description: Route substantive work to relevant Ares profiles.
version: 1.4.1
author: Josh Stevenson / RecursiveIntell Ares
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [profiles, collaboration, evidence, publication, routing]
    category: productivity
    related_skills: [ares-agent-skill-authoring, governed-work-closeout]
---

# Profile-Separated Ares Collaboration

## Overview

This skill is the durable operating contract for collaborating with the named
Ares profiles. `ares_runtime.specialist_routing` owns deterministic need
routing; this skill owns the repeatable procedure; `scripts/run_panel.py` owns
bounded invocation and receipt capture; `scripts/verify_receipt.py` owns
mechanical artifact verification. Profile reports remain advisory until Ares
independently checks them against current source and runtime state.

Policy presence, a spawned process, a returned report, and a controller-verified
panel are different evidence states. Never collapse them into one claim.

## When to Use

Use only when a specialist lane is directly decision-changing or the work is externally consequential, and its expected information gain or risk reduction exceeds the latency, cost, and session noise. Do not consult profiles for routine mechanical work, facts available from current source, or as ceremony. Select the smallest sufficient specialist set. Escalate to the full panel only when the task is genuinely cross-domain, high-stakes, release/publication-critical, or the operator explicitly requests it. When direct work and specialist work are independent, launch the consultation in the background and continue the direct work immediately; synchronize only at the first decision or publication gate that actually needs the reports.

## Rule

Actual profile-separated Ares bots are preferred over generic delegated
subagents when specialist consultation is justified. Route only to named lanes
that can materially improve the result. Give selected profiles the same
self-contained brief, integrate their outputs, preserve dissent, and
independently verify decisive claims. A profile cannot replace the operator's
authority or widen its own scope. Omitted irrelevant profiles are not failed
lanes; record the selected set and routing reason in the receipt.

## Operator model-targeting boundary

The operator's recorded 2026-10-12 worker default was `gpt-6-luna` / `openai-codex` / `xhigh`; treat dated model defaults as historical context, not permission to overwrite a currently configured profile. Resolve any current explicit operator instruction first, then inspect the selected profile's actual model/provider/effort and bind those settings to the run. On 2026-10-17, the inspected `longmemeval-bench` profile explicitly selected `gpt-6.1-sol` / `openai-codex` / `xhigh`; the exact run-owned session confirmed Sol and the execution receipt confirmed xhigh. Current configured settings were reused unchanged. Surface a genuine instruction/config conflict rather than silently substituting or retargeting. Preserve the orchestration chat and its main model/effort; a default-worker request is not permission to retarget that chat. Do not silently substitute DeepSeek, another model, or lower reasoning when an admitted route fails; report the blocked route.

When the operator requests a model change in response to failed specialist calls, keep that change scoped to the implicated specialist profiles. Do not retarget the orchestration chat or root/default profile. For this operator, changing the main default requires an explicit request naming the **main default model**; a specialist model request is not that permission. Read current profile settings first and reuse an already-applied correction rather than rewriting them. Confirm the specialist's actual provider call and reasoning setting through its execution receipt. Never try to repair a specialist outage by changing the orchestrator's model or reasoning.

## Configuration-adaptive resource weights

The hard limits remain 10 worker processes per session and 25 worker processes
across all runner sessions for the current OS user. A second boundary limits the
same global pool to 25 weighted resource units. Every worker consumes at least
one unit, so weighting can reduce concurrency but can never weaken the 25-process
cap.

The runner derives each weight from the selected profile's current `config.yaml`
and binds the plan to that file's SHA-256 digest. It fails closed if the config
cannot be parsed or changes between planning and process start. Policy
`profile_config_weight_v1` assigns:

- base remote/lightweight profile: 1 unit;
- provider not in the explicit remote-provider set: +2 units;
- 3–5 enabled MCP servers: +1 unit, or 6+: +2 units;
- any enabled plugin: +1 unit;
- maximum weight: 4 units.

This is configuration-adaptive containment, not measured memory attribution.
Do not infer RAM consumption from a weight. Use `scripts/summarize_receipts.py`
for duration, admission-wait, outcome, and observed-weight distributions; require
synchronized process/cgroup sampling before changing weights based on memory.

## Profile routing

- `public` — public claims, README/docs, release notes, commits/pushes,
  deployment statements, portfolio/application material, benchmarks, talks,
  posts, and external communication.
- `job-scout` — job/career research: finding current roles that fit the
  operator's verified skills, discovery leads, posting verification, and
  application-target shortlists (read-only; never applies on the operator's
  behalf).
- `explorer` — competing designs, novel combinations, cheap falsification, and
  kill criteria.
- `longmemeval-bench` — source-of-truth, evidence lineage, schemas, receipts,
  and reproducibility.
- `statistician` — estimands, uncertainty, quantitative comparison, and
  evidence strength.
- `ml-evaluation-researcher` — benchmark design, controls, rubrics, and
  model/evaluation claims.
- `cognitive-scientist` — coordination, dissent, reconciliation, authority,
  and recovery constructs.
- `psychometrician` — measurement validity, assessment governance, and
  discriminant validity.
- `inbox-manager` — correspondence, follow-up, commitments, and communication
  evidence.

## Deterministic need routing

**Current-source reconciliation (2026-10-01):** runtime
`b9ba5ed9b793fd02468856fe4cc8b7e15e2b8df1` exposes
`decide_specialist_nonactivation` as a library-only, non-dispatch function. It
accepts existing validated descriptors and exact bindings, and returns
`no_specialist_needed` or `blocked`; it does not implement the historical
need/value request interface below or select/dispatch a profile. Explicit
specialist dispatch is separately owned by `ares_runtime.specialist_dispatch`
and requires Electron admission. Inspect the installed callable/schema before
using this procedure; never manufacture a routing receipt from a silent module
exit or treat a blocked/unindexed/disabled profile as a missing specialist.
The live roster may contain profiles absent from the source descriptor
manifest. New-specialist proposals are upstream draft artifacts, not admitted
`SpecialistDescriptorV1` records: the latter requires an existing profile and
semantic role. This reconciliation does not authorize profile creation or
credential/role admission.

Before invoking profiles, compile explicit uncovered evidence obligations. If
the installed `ares_runtime.specialist_routing` exposes the documented CLI, run
it with `terminal`:

```bash
python -m ares_runtime.specialist_routing --request /path/to/request.json
```

Some installed revisions provide this module as a library only (no CLI entry
point). In that case, record `ROUTER_CLI_UNAVAILABLE` and select from the
explicit obligation-to-profile map below; do not treat a silent zero-exit module
invocation as a routing decision. The request must name the uncovered question,
why direct deterministic evidence is insufficient, materiality, independence
need, expected information gain, cost, latency, and a specialist cap.
Obligation classes map to profiles:

- `public_claim` → `public`
- `competing_design` → `explorer`
- `source_lineage` → `longmemeval-bench`
- `quantitative_inference` → `statistician`
- `model_evaluation` → `ml-evaluation-researcher`
- `coordination_recovery` → `cognitive-scientist`
- `measurement_validity` → `psychometrician`
- `communication_commitment` → `inbox-manager`
- `career_search` → `job-scout`

A career-search obligation is selected only when the operator requests current
role discovery and supplies or authorizes the verified preference set. Fictional
career fixtures do not activate a real search or justify retaining personal
preferences.

The router preserves one-executor default behavior, suppresses ceremonial
fan-out when direct evidence is sufficient or expected value is non-positive,
and emits `selected`, `no_specialist_needed`, `degraded`, or `blocked`. Advisory
`SpecialistBidV1` artifacts are shadow-only and cannot grant authority or alter
selection. Selecting every configured profile requires `full_panel_explicit: true`.

## Operating procedure

1. Resolve the current source/runtime identity, run deterministic need routing,
   and write one self-contained
   brief containing the goal, constraints, evidence cutoff, allowed scope,
   and explicit read-only boundary when applicable.
2. Select the smallest sufficient set and use the reusable runner. The default and hard per-session ceiling are 10 workers; the runner coordinates both a hard 25-process cap and a 25-unit configuration-adaptive resource budget across all profile-collaboration runs for the current OS user. Each executed lane also receives a digest-bound `AresProfileInputManifestV1` declaring only the controller brief and assigned workspace as first-pass inputs, excluding peer reports/transcripts until the `panel_complete` review barrier:
   ```bash
   python3 "$HOME/.ares/skills/productivity/profile-collaboration/scripts/run_panel.py" \
     --workspace /absolute/workspace \
     --profiles public,explorer \
     --brief 'The same complete brief for every selected profile.' \
     --max-workers 10
   ```
   For a bounded text review (report is wording/consistency over reproduced
   material, not workspace discovery), add `--review-mode text_only` and
   reproduce the material verbatim in the brief; optionally tighten lane tool
   surface with `--lane-toolsets file,web` (default for all lanes is
   `terminal,file,web,search`). The workspace gate (default 2 GiB,
   `--workspace-gate-bytes 0` to disable) refuses dispatch on oversized
   workspaces. Receipt records `review_mode`, `lane_toolsets`, and
   `workspace_size_bytes`.
   Use `--full-panel` only as an explicit escalation. The runner refuses an
   implicit all-profile invocation, isolates each selected `HERMES_HOME`, uses
   bounded concurrency and process-group cleanup, requests automatic archival
   of automation-owned oneshot sessions, and writes a machine-readable receipt
   under `~/.ares/profile-collaboration/receipts/`. Independent runs may launch
   separate instances of the same named profile; profile identity is not a
   singleton lock.
3. If any useful controller work is independent of the reports, start the runner
   with `terminal(background=true, notify=true)` and immediately continue that
   work. Do not poll while useful work remains. Synchronize with the panel only
   before the first decision, mutation, closeout, or public gate that depends on
   its reports. Foreground execution is acceptable only when no independent work
   remains. Background execution does not weaken the requirement to review every
   selected outcome before relying on it.
4. Run the dry-run first for a new routing shape. It must enumerate exactly the
   explicitly selected profiles, in canonical order, with no duplicates. A
   full-panel dry-run must enumerate every configured profile. **Use distinct output
   directories for dry-run and execution** (for example `--out /packet/panel-dry-run`
   and `--out /packet/panel-run`): dry-run also creates its receipt directory, and
   reusing it for execution fails with `FileExistsError` before any profile runs.
   A dry-run plan is not an executed consultation. The current `verify_receipt.py`
   intentionally rejects dry-run receipts (`terminal_state=planned`, empty
   results); inspect `panel.json` for the exact selected profiles, order, and
   resource plan instead. Run `verify_receipt.py` on the separate **executed**
   receipt only. Preserve failed receipts and retry execution under a new path,
   never delete/reuse the old directory.
   **One-shot identity must be unique per run, not merely per output-directory leaf.** The panel runner's
   `HERMES_ONESHOT_SESSION_ID` must bind to its unique panel `run_id`; a repeated
   `--out` basename such as `panel` must not reuse an old archived session ID.
   An `archive` success does not prove a session was fresh: an old archived
   session can otherwise receive new messages and retain its prior context.
   The current runner records `session_identity_policy=panel_run_id_bound_v1`
   and fails closed if that ID already exists in the target profile DB. Before
   relying on a run, check its session ID is run-bound, newly allocated, and
   archived; preserve contaminated old sessions and receipts rather than
   deleting or editing their transcript.
   Do not put credentials, tokens, passwords, or connection strings in the brief or receipt.

### Per-profile timeout floors

A profile may set this runner-owned, profile-local extension in its `config.yaml`:

```yaml
profile_collaboration:
  timeout_seconds: 600
```

The runner accepts only finite positive values up to 3600 seconds. It treats the
value as a **floor** for that selected profile: an explicit longer `--timeout`
remains longer. It raises the panel deadline only when needed to honor a
configured floor, preserving an explicitly short `--panel-timeout` for profiles
without one. The receipt records requested and effective per-profile timeouts,
requested/effective panel deadlines, timeout floors, and per-profile effective-
brief hashes. `verify_receipt.py` verifies those bindings. This extension is
consumed by the runner; it is not an authority grant or a core Hermes
configuration contract.

### Profile-owned reasoning and child environment

The runner reads each selected profile's validated `agent.reasoning_effort`
(`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`) and
passes it to the actual `hermes_cli.main` process. It does not unconditionally
override every specialist to `low`. The plan, child input manifest, result, and
verifier bind the selected reasoning effort.

Panel children start from a minimal host allowlist (`HOME`, `PATH`, locale, and
timezone) and receive an explicit target `HERMES_HOME`; the controller's ambient
profile credentials, routing hints, and secret scope do not cross that boundary.
The target profile must own its credentials or explicitly opt into the root
credential underlay. This is a local application of the profile-provenance rule:
a missing target value removes that authority; it does not inherit a sibling or
controller value by accident.

5. Join every selected profile before relying on the consultation. A running child is not a completed
   consultation. A nonzero exit, timeout, empty report, controller error, or
   failed session archival is a failed or blocked selected lane, not implicit
   approval. Automation-owned transcripts remain durable but should be archived
   from the normal Desktop Sessions projection.
6. Verify the receipt without changing it:
   ```bash
   python3 "$HOME/.ares/skills/productivity/profile-collaboration/scripts/verify_receipt.py" \
     --receipt /absolute/path/to/receipt-directory
   ```
   Pass the **directory containing `panel.json`**, not the `panel.json` path itself;
   the verifier writes its separate `verification.json` projection inside that
   directory. If `run_panel.py` used an explicit alternate `--runtime`, pass that
   exact same path to the verifier with `--runtime`; otherwise verification compares
   against the default active runtime and reports a false runtime-identity mismatch.
   If you invoke the standalone verifier with Python `-P`, explicitly include the
   trusted runner `scripts/` directory alongside the pinned runtime in `PYTHONPATH`.
   Safe-path mode otherwise excludes the verifier's sibling `run_panel.py`, producing
   `ModuleNotFoundError: run_panel` before verification. Preserve that failed attempt
   and rerun only the read-only verifier with the corrected import context; do not
   replay a successful publication, activation, or Desktop cutover to fix this error.
   This checks profile identity/order, runtime revision, artifact existence, byte
   counts, and SHA-256 digests. It deliberately leaves semantic review to the
   controller.
7. Read each returned report, extract evidence/uncertainty/dissent/next gate,
   preserve disagreements, and independently verify decisive claims against
   current files and live commands. Only then may the controller mark the
   panel closeout verified. For exact-candidate reviews, keep repository source
   unchanged until EVERY selected lane has finished. A first returned lane can
   identify a repair but does not release the shared review barrier. If the
   controller edits while another lane is running, preserve the drifted review
   as superseded/HOLD and freeze/review the new bytes; successful artifact/hash
   verification cannot approve a candidate that changed during review.
8. For anything public or externally visible, obtain the `public` review
   before the effect and again at closeout. Publication/release authority is a
   separate operator decision.

## Contained read-only review and verifier admission

For a review that must not write the source/index, pass `--read-only-review`
to `scripts/run_panel.py`. This opt-in Linux mode must pass a real `bwrap`
namespace probe; unavailable containment is blocked, never silently downgraded.
The host root and canonical profile inputs are read-only. Writable scratch and
session accounting are run-owned under the receipt directory; canonical Bot
Chats and profile session stores are not replaced. The input manifest and result
bind the actual containment policy and run-owned state path. Read back archival
in that exact isolated state.db, not the canonical profile database.

Pass `--require-read-only-review` to the matching verifier when
containment is an acceptance requirement. This boundary provides filesystem
write containment, NOT network or secret isolation: configured provider access
remains runtime-owned and the receipt states those limits. Evidence-only inputs
and temporary indexes are useful hygiene but are not equivalent to the namespace.

Before dispatch the runner exercises the standalone verifier's real `-P` import
and CLI context. A failed preflight stops worker creation and preserves a failed
receipt. Zero-exit empty/whitespace or truncated reports are not completion.
Normal-return descendants also undergo bounded process-group cleanup.

Verification attempts are retained under `verifications/`; `verification.json`
is a rebuildable latest projection. `execution_artifacts_verified` does not mean
semantic approval: `semantic_review_complete` remains false until the controller
actually reviews the outcome. Do not promote a skipped/failed required gate or
an unresolved exact-target readback. After an ambiguous post-effect failure,
inspect the target before retry; an observed effect authorizes only retrying its
failed verification, not repeating the mutation.

## Evidence states

Use these distinctions in receipts and responses:

- `policy_present` — SOUL/skill text exists and is loaded or observed on disk;
- `invoked` — a profile process was started;
- `returned` — the profile exited with a report;
- `execution_artifacts_verified` — the controller verified the receipt files,
  identities, and hashes;
- `controller_verified` — the controller reviewed the reports and current
  source/runtime evidence;
- `not-applicable`, `unavailable`, `failed`, or `blocked/unknown` — explicit
  lane outcomes that must remain visible.

A runner receipt can prove execution mechanics. It cannot by itself prove that
all reports are correct, that a desktop is healthy, or that a public claim is
safe.

## Support files

- [scripts/run_panel.py](scripts/run_panel.py) — bounded profile invocation with
  a 10-worker per-session ceiling, a 25-process cross-session cap, a 25-unit
  weighted resource budget, per-profile stdout/stderr artifacts, and
  `panel.json`.
- [scripts/summarize_receipts.py](scripts/summarize_receipts.py) — aggregate
  receipt-only p50/p95 duration and admission waits, outcomes, and observed
  resource weights without reading specialist report contents.
- [scripts/verify_receipt.py](scripts/verify_receipt.py) — mechanical receipt
  and hash verification; writes a separate `verification.json` projection.

## Common pitfalls

- Launching a specialist when its report is neither decision-changing nor
  required for an externally consequential gate. Strict relevance gating is the
  default; omission of an irrelevant profile is not degraded execution.
- Running the panel in the foreground while source inspection, local tests,
  drafting, or other report-independent work remains. Start it in the background,
  continue useful work, and join only at the first dependent gate.
- Bypassing the 10-worker per-session ceiling, the shared 25-process cap, or the
  25-unit weighted resource budget. Global capacity waiting is bounded by the
  panel deadline and must remain visible through `resource_weight`,
  `admission_slots`, and `admission_wait_seconds` in returned lane receipts.
- Treating configuration-derived weight as measured RAM, CPU, GPU, or provider
  cost. It is containment based on active provider/MCP/plugin configuration;
  synchronized runtime measurements are a separate evidence gate.
- Treating a named profile as a singleton. Separate sessions may run independent
  instances of the same specialist; do not deduplicate them across unrelated
  briefs or make one session wait merely because the profile name matches.
- Treating a memory entry, SOUL paragraph, or skill file as proof that the
  panel actually ran.
- Treating a generic approval or empty response as a domain result.
- Allowing one profile's credentials, configuration, workspace, or authority
  to leak into another profile.
- Replacing a failed panel with a generic delegated subagent without recording
  the unavailable-profile exception.
- Sending a profile a huge staged diff as a publication brief. First validate the
  profile with a bounded one-shot; then ask for a narrow claim-boundary review
  against the staged stat, selected high-risk paths, and named validation receipts.
  A panel timeout caused by unbounded review scope is not a profile-health failure.
- Treating empty stdout from a timed-out `-z` process as proof of provider inactivity. Inspect the exact receipt session ID in the profile's state.db read-only (messages role/content/tool_calls/tool_name/timestamp; do not expose reasoning columns). On 2026-09-05, the packet cognitive-scientist lane continued tool work until about seven seconds before its 300-second kill, after broad skill/source intake and compaction. A public lane similarly spent its budget on workspace discovery and nested delegation. Bind the absolute workspace and narrow lane obligation in the brief; distinguish busy scope exhaustion from idle provider failure.
- Model choice silently changes lane token budgets. On 2026-10-20, `state.db` billing columns showed the `opencode-go`/`glm-5.3-flash` era produced lanes at 100k–704k input tokens versus 20–50k in the prior `openai-codex`/`gpt-6.1-sol` era, with the timeout rate concentrating in the big-token era. When timeouts rise, compare `sessions.billing_provider`, `input_tokens`, and `output_tokens` across eras before blaming providers. A same-profile control lane that succeeds minutes after a timeout is stronger evidence about scope than any aggregate statistic. Header-claimed model/effort is not enough: only the model actually billed each lane is current truth.
- `auth.json` `last_auth_error` is per-provider historical residue, not necessarily a live failure. A stored `refresh_token_reused` on a provider no longer routed-to does not block lanes billed through the current provider. Verify which provider actually served recent lanes (billing columns) before re-authing anything.
- The 2026-10-20 runner additions (clarified after a full-pass execution and 46/46 test run): `--review-mode text_only` binds the lane to a reproduced-text review with no workspace enumeration; the workspace-size gate (`--workspace-gate-bytes`, default 2 GiB) refuses dispatch on oversized workspaces (a 7.46 GiB workspace was refused as designed); `--lane-toolsets` (default `terminal,file,web,search`) passes a minimal toolset to each `hermes -z` lane so lanes stop paying ~46-tool schema load. For text review lanes, prefer `--lane-toolsets file,web` and reproduce the material verbatim in the brief.
- Assuming process-leader exit proves descendant cleanup. The repaired runner now checks process-group existence independently of leader liveness, escalates surviving descendants, bounds post-termination pipe draining, and records `cleanup_state`, `cleanup`, and `pipe_drain_completed` per lane. Regression coverage includes a TERM-exiting leader with a TERM-ignoring descendant and inherited-pipe drain; the installed repair passed 19 runner tests on 2026-09-05. Preserve the cleanup fields and treat `cleanup_state=incomplete` as failed containment, never as a returned report. The canonical runner also emits `cleanup_state=not_required` when the group was already gone without TERM/KILL; do not reject that state solely because its label is not `clean`. For a qualified normal return require the actual conjunction `leader_reaped=true`, `group_gone=true`, and `pipe_drain_completed=true`, plus the expected returned outcome and archived session. A receipt/checker correction must preserve the failed audit and supersede it explicitly; do not rerun a successful consultation or weaken unresolved cleanup facts.
- Mutating profile homes during a read-only consultation.
- A contained read-only review can fail before agent initialization when the provider runtime needs its mounted `auth.lock` or an authentication refresh write. An observed `Read-only file system: .../auth.lock` with empty stdout is an unavailable selected lane, not a completed review or a reason to weaken containment. Preserve the failed panel and standalone verifier output; inspect only the exact run-owned session ID to distinguish `not_created` from an unarchived session. Do not make credential files writable, change profile credentials/config, drop `--read-only-review`, retarget the orchestration model, or repeat under another credential owner implicitly. Use the explicitly allowed generic read-only subagent fallback, labeling it degraded and not the named profile, or ask for owner-supported runtime repair if that fallback cannot satisfy the gate. Retain the fallback's completed delegation manifest and full report with hashes; read-only instructions alone are not the original bwrap containment proof. For generic fallback reviews, require the complete report to be written to an explicit run-owned file and return its exact path; verify that artifact before closing the gate. Automatic final-message delivery, an absent summary-cache file, and a clipped live-transcript excerpt are not durable full-report capture. Preserve failed retrieval evidence and never mark semantic review complete from the completion status or verdict fragment alone.
- A profile can exit zero yet be operationally blocked when every tool call fails before execution (observed runtime error: `DaemonThreadPoolExecutor object has no attribute _initializer`). The runner recognizes that specific exit-zero failure report, records `outcome=blocked`, `termination_reason=operational_blocked_report`, and `operational_block=daemon_pool_initializer_failure`, and fails the panel instead of manufacturing a successful return. Preserve the receipt. This can happen even when the recorded runtime contains the Python 3.14 fix: Python normally prepends the child working directory to `sys.path`, so an assigned source workspace can shadow `hermes_cli`, `tools`, and `hermes_state` ahead of the receipt-bound runtime despite `PYTHONPATH`. The runner must launch both the profile process and its archive helper with Python safe-path mode (`-P`) and an explicit runtime `PYTHONPATH`; `child_environment_policy=target-profile-clean-env-runtime-isolated-v2` records that boundary. Retry only after that boundary is present or with another verified runtime. A successful post-fix probe must exercise a real inspection tool against the assigned workspace and pass receipt verification; merely importing `tools.daemon_pool` from the runtime directory does not test workspace-shadow resistance. Other semantically blocked reports still require controller review; this detector is a narrow witness for the known incident, not a general semantic judge.
- A profile one-shot can exit zero with the standalone stdout `HTTP 401: Encountered invalidated oauth token for user, failing request`. That is an authentication-blocked lane, not a report. New runner executions classify this exact standalone message as `outcome=blocked`, `termination_reason=operational_blocked_report`, and `operational_block=oauth_token_invalidated`; a quoted historical 401 inside an otherwise valid review is not classified as an operational failure. Earlier receipts are immutable and are not retroactively repaired: quarantine any previously returned lane with this text even when its artifact/hash verifier passed. Keep target-profile OAuth ownership explicit; do not guess credentials, paste an API key, or silently retry under a different profile/provider. A fresh authorized credential repair and model smoke are separate gates.
- A standalone `API call failed after ... retries: HTTP 429: You used all included Pro usage` is a provider-quota-blocked lane, not a specialist report. The runner recognizes the evidenced standalone message (including its billing/ref suffix) as `provider_quota_exhausted`; quoted historical errors inside a report are not classified by that signature. Preserve prior receipts that labeled this `returned`: mechanical hash/archival checks were not semantic approval, and new code does not rewrite those histories. Record blocked lanes separately; do not change provider, profile credentials or billing implicitly. A selected lane needs a real report before its decision gate can pass.
- A reviewer can test a base-to-candidate patch in the wrong direction against an already-patched tree. Provide an executed disposable-index witness (`read-tree <exact-base>`, `apply --cached <full-patch>`, `write-tree`) and require its complete tree to equal the staged candidate; use reverse-check on the already-patched worktree. Freeze current source, title/body/reply, tests and lint receipts together; name obsolete exports as historical so stale artifacts cannot veto or qualify the current snapshot.
- A custom outer `bwrap --bind / /` around an implementation runner needs an explicit usable device mount (`--dev /dev`). A missing device setup was observed to pass runner dry-run/verifier preflight but fail the real oneshot before session creation with `PermissionError: '/dev/null'`. Before dispatch, exercise a real write to `os.devnull` and confirm it is a character device in the exact wrapper. Preserve the failed receipt/session-not-created outcome; fix only the device mount, retain all original source read-only mounts and credential ownership, and execute under fresh dry/run output directories. This is distinct from the read-only `auth.lock` failure; never make credentials writable to solve a device error.
- Overwriting an old receipt or deleting failed evidence. Receipts are
  append-only run artifacts; new attempts get new directories.

## Verification checklist

- [ ] The current runtime and source identity are recorded.
- [ ] The brief is identical across all required profiles and contains no
      secrets.
- [ ] Every executed first-pass lane has a verified input manifest bound to the shared brief/workspace, with no peer-output edge before the declared review barrier.
- [ ] The specialist set is directly decision-changing or externally consequential, is the smallest sufficient set, and has a recorded routing reason.
- [ ] Report-independent controller work continued while a background panel ran.
- [ ] Requested concurrency is at most 10; global admission declares both the 25-process cap and 25-unit resource budget.
- [ ] Every selected profile has a digest-bound resource plan, and every returned lane records matching weight, slots, and wait.
- [ ] Telemetry summaries distinguish scheduling observations from memory attribution.
- [ ] Concurrent uses of the same profile have distinct run/session identities.
- [ ] Dry-run profiles match the explicit selection in canonical order.
- [ ] Every selected profile has an explicit outcome and exit status.
- [ ] Receipt paths remain inside the run directory and their hashes verify.
- [ ] No orphaned panel process remains after completion or timeout.
- [ ] Automation-owned oneshot sessions are archived and absent from the normal Sessions projection.
- [ ] Reports are reviewed for evidence, uncertainty, dissent, and next gates.
- [ ] `public` is consulted before any external-facing effect.
- [ ] Desktop/runtime recovery is tested separately from collaboration.
- [ ] Remaining proof debt and rollback/quarantine are visible.

## Closeout

Report the receipt path, runtime revision, profiles consulted, each profile's
outcome, preserved dissent, controller verification commands, unresolved
blockers, and whether the public gate passed, failed, or was not applicable.
Do not call the feature complete merely because the runner returned zero: the
mechanical verification and semantic controller review must both pass.
