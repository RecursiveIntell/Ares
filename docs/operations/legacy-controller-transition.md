# Explicit legacy local-controller transition

This procedure concerns `ares_runtime.local_runtime` releases whose original
`release.json` contains `revision`, `source`, and `installed_at`, but no final
`runtime_binding`. It does not migrate another installed-runtime owner, import
credentials, certify native-provider behavior, or authorize a live rollout.

## Why bootstrap is explicit

The old managed `ares` launcher executes the selected old controller. That
builder cannot emit the new final-binding contract. Do not use it to install a
new controller and then manufacture success by stamping metadata on old releases.
Run the reviewed new controller from its own source-bound interpreter instead.

The new controller refuses ordinary setup/update from an unqualified current
release. The explicit option takes the **full expected current commit**, not an
abbreviation or an implicit "whatever is selected":

```bash
# SOURCE must be a clean, reviewed, exact merged downstream main checkout.
# ARES_HOME must be the intended installation; preserve its existing owners.
# Provision the bootstrap interpreter in SOURCE, never in the selected release.
UV_PROJECT_ENVIRONMENT="$SOURCE/.venv" uv sync --project "$SOURCE" --locked --extra all --no-dev

ARES_HOME="$ARES_HOME" HERMES_HOME="$ARES_HOME" ARES_BIN_DIR="$ARES_HOME/bin" \
  ARES_GATEWAY_UNIT_PATH="$GATEWAY_UNIT" \
  "$SOURCE/.venv/bin/python" -m ares_runtime.local_runtime setup \
  --source "$SOURCE" --transition-from-legacy "$EXACT_CURRENT_SHA"
```

`SOURCE`, `ARES_HOME`, `GATEWAY_UNIT`, and `EXACT_CURRENT_SHA` are operator-resolved
identities, not defaults to guess. Inspect the managed Node/npm engine versions,
obtain a consistent authority backup and private protected preimages, and rehearse
in a disposable installation first. The normal setup defaults build Desktop and
manage the gateway; `--no-gateway` is appropriate for an isolated fixture, not a
substitute for live service verification. An existing Desktop process requires its
own managed coherence check and approved replacement; setup does not certify it.

## Binding and refusal rules

- The contract-1 completeness gate remains unchanged. Missing/wrong bindings on
  selected releases are not repaired or rebuilt in place.
- Legacy identity is probed read-only using its own interpreter, isolated Python
  imports, neutral working directory, and bounded child time/output. The source,
  Git head/tree, clean state, original descriptor digest, interpreter digest,
  virtualenv configuration, final prefix, and controller/CLI import paths must
  agree. A new-controller source with missing metadata is **not** legacy.
- New candidate finalization atomically records both its normal
  `AresLocalRuntimeBindingV1` and a distinct `AresLegacyRollbackBindingV1` in the
  candidate's existing `release.json`. No legacy descriptor is rewritten, no old
  source is claimed to implement contract 1, and no second selection registry is
  introduced. The legacy binding is correlation evidence, not a signature or a
  same-UID security boundary.
- Selected pointer identity and legacy bytes are rechecked under the controller
  lock before selection. Drift, incomplete output, invalid JSON, wrong producer,
  unsupported descriptor shape, or ambiguous imports refuse the transition.
- Direct activation cannot bypass the backout admission. A reused candidate must
  still supply the exact binding and a source-owned transition-capable controller.

## Backout

After the explicit transition, ordinary `ares rollback` may select the bound
legacy `previous` target **only after fresh identity revalidation**. Absent,
malformed, wrong-target, changed, or forged binding fields refuse before pointer
or service effects. A present but invalid normal binding never falls through to
legacy admission. The existing durable pointer-pair journal owns interruption
recovery; there is no manual-symlink recovery recipe.

A successful backout restores the actual old controller and unchanged legacy
release bytes. The selected old controller retains its original behavior; a
subsequent ordinary rollback is expected to select the new candidate again.
Confirm that old-controller behavior in the managed rehearsal rather than
inferring it from new-controller fixtures. This expected selection symmetry is
not a migration of the old controller. To upgrade again, use the explicit new
source-bound bootstrap, not the legacy updater. A failed or interrupted cutover
requires fresh pointer, journal, service, and protected-state inspection before
retrying; do not assume the old baseline is still current.

## Proof boundary

The regression suite lives in
`tests/ares_runtime/test_legacy_controller_transition.py`, alongside the existing
local-runtime and lifecycle-recovery suites. It covers explicit admission,
normal legacy backout, preserved legacy descriptors, old-builder/new-controller
mismatch, post-backout forward selection, atomic binding publication, drift,
invalid/duplicate JSON, selected-release preservation, and bounded probe failures.

Fixture imports and green unit tests are not installed-package, Desktop E2E,
provider, gateway, or live-activation certificates. Bind the actual staged build,
editable-path move, launcher readbacks, backout, failure/recovery controls and
protected-state preservation to an exact source tree before making those claims.
