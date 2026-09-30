# SessionDB dispatch task basis

## Supported boundary

`SessionDBContextDispatchV2` captures a `task_binding_digest` in the existing
dispatch history record. The source is the same SQLite snapshot used by local
admission: canonical profile/root, sorted current custody heads (including
released heads), their generations/checkpoints, and validated V2 native-input
task bindings. No task display label, optional goal, environment override,
callback result, custody token, or new mutable authority store supplies it.

The settled-response tool gate and native-call signing gate compare this digest
inside their existing SessionDB write transaction. Assistant and completed tool
rows do not change this basis. A custody claim, refresh, release, checkpoint or
source update, recovery-file binding, transfer or recovery generation change
invalidates old tool proposals and requires fresh provider admission. Released
task obligations remain in the inventory; release does not erase history.

V2 bindings are checked against authentic input provenance using the same
connection. Compacted input and released ancestor sessions retain their native
provenance semantics. V1 historical-goal custody is not promoted into V2 task
authority. Its existing canonical head/checkpoint remains in the inventory.

This protects the whole-session inventory. It does **not** select or authorize
one task from `mission_ref`/`task_id`, authenticate a remote owner, establish a
cross-owner fence, or prevent an owner changing after the local check. No native
RPC is performed inside SQLite transactions. Recovery's `action_control_digest`
is unchanged: recovery intentionally advances custody under its own reservation.

## Canonical owners and mutation surfaces

- `hermes_state_runs.py`: immutable custody generations and CAS heads;
  `claim_run_custody[_checked]`, `claim_run_task_custody_checked`,
  `publish_run_checkpoint`, `transition_run_source`, `transfer_run_session`,
  `refresh_run_custody`, `renew_run_custody_for_context_recovery`,
  `bind_run_recovery_files_checked`, `release_run_custody`, and continuity
  transfer methods converge on `_commit_run_on_conn`. Snapshot reads enumerate
  canonical heads, not an adapter-maintained task index
- `hermes_state.py`, `hermes_state_inbox.py`, `hermes_state_input_turns.py`:
  session/profile lineage, authentic message/input provenance, accepted inputs,
  projection, compaction and rewind. The task binding reader validates the
  underlying rows each time; it does not assume a cached digest survives edits
- `hermes_state_continuity.py`: Stop (`record_context_stop`), local admission,
  settlement and signing. Existing input/Stop checks remain mandatory
- `hermes_cli/goals.py:save_goal` and continuity resume own goal changes. Goals
  remain controls/history, not a substitute for canonical run identity
- `hermes_cli/profiles.py`: create/import/rename/delete profile, profile metadata,
  role and specialist references, active-profile selection;
  `hermes_cli/config.py:save_config`, `write_platform_config_field`, and
  `set_config_value` own filesystem configuration. These external files are
  **not guarded by this SQLite change**

## Compatibility and recovery

Earlier dispatch receipts remain execution history but lack the new basis and
cannot authorize additional tools/signatures. They must not be backfilled from
today's state. Unknown/spent attempts remain unknown/spent; no replay, budget
reset, or renewal of prior authority is implied. Added snapshot provenance can
also make an older materialization stale. Stop and reconcile rather than force
its admission. Reverting to a reader that ignores this binding is not a safe
rollback for an enrolled governed cohort; retain the corrected reader or remain
stopped. No live migration, enrollment, key change or activation is part of this
source patch.

## W05 prerequisites still open

1. A real selected-task bootstrap must derive its run from canonical custody,
   tie it to the active controller/lease, and reject ambiguity instead of taking
   caller-supplied task identity as authority
2. Profile/config owners need current authenticated acquisition and a mutation
   protocol covering every supersession/revocation writer above
3. Pinned `semantic-memory-mcp` needs authenticated-principal derivation and the
   governed V2 endpoint; fixture echoes are not authentication
4. Native participating-owner guard changes remain a separately unpublished
   owner patch. Adoption requires the real protocol plus authenticated server
   identity/incarnation, not socket path or UID alone
5. SessionDB participation requires an audited durable prepare/recovery protocol
   coordinating input, Stop, task and profile changes without RPC under SQLite
6. Final serialized provider bytes, retries, sinks and client-side HTTP paths
   still require their own route closure and admission proof

## Validation

`tests/ares_runtime/test_dispatch_task_binding.py` exercises real disposable
SessionDB admission/settlement, independent-connection mutations, missing legacy
basis, malformed task provenance, restart, recovery-control stability, native
prepare-before-sign mutation, and the actual tool-registry refusal boundary.
The native peer in the signing regression is a recording test peer, not evidence
of authenticated production bootstrap. Existing custody/cold recovery,
continuation, native-dispatch and live-candidate suites remain required.
