# Ares 2.0 — pre-port host-readiness contracts

**Status: implementation in progress; no readiness gate has passed.**

All requirements describe semantics to PROVE against the separately pinned stock-Hermes tree. PR #94878 is mandatory, but its historical local test results are not current-lab results.

| ID | Contract / effect owner | Relevant source of truth | Acceptance evidence | State |
| --- | --- | --- | --- | --- |
| P0-01 | Child inherits no foreign profile-owned environment values | #94878: `agent/secret_scope.py`, `tools/environments/local.py` | A→B absent/different values at actual child spawn | First synthetic contract tests committed; implementation not ported, tests NOT RUN |
| P0-02 | Revocation does not reclassify previously owned launch values | #94878: monotone owned-name history | delete source, re-spawn, prove no stale value | Not yet ported |
| P0-03 | Unreadable/malformed/stale secret source refuses explicit cross-profile child boundary | #94878: env loader and profile scope | failure before child creation | Not yet ported |
| P0-04 | Wrapped/forwarded variables cannot reintroduce source credentials | #94878: direct/Apptainer/Singularity carrier normalization | nested wrapper and force-alias permutations | Not yet ported |
| P0-05 | Persistent shell snapshots cannot replay revoked/source-owned credentials | #94878: `tools/environments/base.py`, `test_profile_secret_isolation.py` | real shell replay/restore after ownership growth | Not yet ported |
| P0-06 | Scope parity across foreground, background, PTY, SSH, Docker, Kanban and compute host | #94878 affected execution owners | real child construction, deny/allow with exact target | Not yet ported |
| P0-07 | Cron reload retains or refreshes target installed authority correctly | #94878: `tests/cron/test_routed_fire_reload_child_authority.py` | unchanged and rotating external source | Not yet ported |
| P0-08 | Target config grants survive without ambient/grant bypass | #94878: Docker config target-grant tests | collision + target-only + internal-secret exclusion | Not yet ported |
| P0-09 | Clarify preview and interrupted historical-call lifecycle parity | #94878 desktop Clarify follow-up | component composition and headed E2E (not asserted) | Not yet ported |
| P0-10 | Contributor provenance and release attribution parity | #94878 author resolver/checker | casefold collision and raw %ae tests | Not yet ported |
| P0-11 | Singularity lifecycle and overlay identity (only if backend enabled) | official plugin draft #362 | cross-process restart + retry + native backend when available | Conditional, unqualified |
| H1-01 | Required named context engine cannot silently fall back | `agent/agent_init.py` + opt-in config | missing, clone fail, wrong name, safe worker, legacy fallback | Code committed, tests not executed |
| H1-02 | Critical context selection errors cannot silently fall back | `agent/conversation_loop.py` | fail closed when explicitly required; no-op/legacy preserved | Code committed on 01b branch, tests NOT RUN |
| H2-01 | Exact post-rewrite tool action denied before effect | #32719/#95247, `tools/registry.py`, `agent/tool_executor.py` | main/direct/MCP/nested/parallel dispatch once-only | Not implemented |
| H3-01 | Receipt activation follows exact durable transcript commit | `persistence.conversation`, Ares context governor | prepare/validate/commit/abort/crash/recovery tests | Not implemented |
| H4-01 | Exact physical model send needs valid permit | Ares `ares_runtime/physical_dispatch.py` and upstream transport | denied sends exactly zero bytes; streams/retries settle | Not implemented |
| H5-01 | Delegation does not create second authority owner | `agent/delegation_provider.py` and AgentGraph | bound parent/child lifecycle and cancellations | Design spike only |

## Mandatory execution discipline

- Run all tests with upstream `scripts/run_tests.sh` in a private disposable `HERMES_HOME`, not a production profile.
- Establish stock-Hermes red witnesses before accepting a host API change; pass exact scenarios and sibling routes after.
- Verify optional defaults: no behavior changes, no prompt/tool-surface drift, no extra network send.
- Track authentic call counts, persisted row IDs and scoped ownership. Logs or mock returns alone do not prove effects were blocked.
- Do not count missing CI workflows, action-required status, skipped suites, or static code inspection as a pass.
- Do not port actual Ares services until the P0 + relevant H1–H4 conformance gates have evidence.
- Preserve the original authors and contributors of #94878/#91293 when reconstructing code.

## Recorded staging work

- `lab/ares2-stock-hermes-2026-11-21`: pristine upstream Hermes source.
- `lab/ares2-01-required-context-2026-11-21`: commit `b6a28ef2482f0557a8184f97a99b5429f3fe3773`, four-file implementation and synthetic regression tests, **NOT RUN**.
- `lab/ares2-00a-pr94878-contracts-2026-11-21`: exact first #94878 behavioral acceptance scenarios committed, not run.\n- `lab/ares2-01b-required-selection-2026-11-21`: commit `f7dbce98b34973948113a11ef6b4deec1e68eb90`, opt-in per-request context selection, not run.\n- This branch: manifests and coverage ledger; **no execution claims**.

## Rollback

Discard any one staging branch, preserving its SHA and evidence. Existing Ares `main` and #94878 remain unchanged. No existing profile or data store is part of this laboratory.
