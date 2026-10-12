# Canonical full installer hardening — 2026-10-12

## Source and scope

Ares base: `be0088f84dc1c8c6a66cad230815b31ebf85220c`.
Original root installer blob: `66f77b71bf20f5cc1738adf442cf3af2a744228f`.
Final installer blob: `43e5e48c0e8b1c2744ed85c3bdebbcc2c0d6e104`.
Final installer SHA256: `8c59ab74c4ba5fe47524c471bf79c3f259a8fdf947fc0a228a5d0168ac95ef4b`.

This is the root full-distribution `install.sh`, not the inherited
`scripts/install.sh` used by the historical release-tag installer matrix.
Existing PR #137's sandbox TLS/npm/dependency-tree work remains separate.
No claim that this patch fixes that entire matrix follows from these tests.

The runtime controller, release authority, qualification manifests, dependency
lockfiles, and provider-selection interfaces are unchanged. The full default
component set and existing opt-outs remain. Recipe version advances from 3 to 4;
existing immutable releases with different inputs are preserved, not rewritten.

## Repairs

- Inherited shell error trapping and explicit errors share recovery; substitutions
  recover once in the top-level shell. Sourcing refuses without exiting the caller.
- Validate valued arguments, branches, and physical paths before provisioning;
  preserve space/Unicode paths and side-effect-free help/plan. Reject unsafe
  symlink aliases as well as directly unsafe paths.
- Serialize cooperating full installers per home without stealing locks. Preserve
  the independent runtime owner's activation lock and rollback authority.
- Pin managed uv 0.12.19, verify its installed version, and bound HTTPS bootstrap
  downloads with certificate verification and HTTPS-only redirects. This dated
  release satisfies the inherited reviewed <0.13 policy; uv 0.13 is not qualified
  here. Official source: https://github.com/astral-sh/uv/releases/tag/0.12.19 .
- Sanitize inherited uv/Python environment overrides, own the Python directory,
  and keep installer-owned uv/Cargo ahead of ambient executables. The internal
  uv pin remains outside the UV_* namespace removed by sanitization.
- Refuse dirty, symlinked, wrong-origin, unpublished-ahead, or diverged checkouts;
  preserve legitimate branch switching and published fast-forward updates.
- Install native wheels without dependency re-resolution, check dependency closure
  with uv pip check before sealing, and verify executable native tools without
  depending on Python assertions being enabled.
- Preserve customized generated plugins and symlinked configuration/skills.
  Refuse an explicit False configuration-save result. Back up and recover the
  installer-owned skill namespaces, skill manifest, stack plugin, config and env.
- Refuse old-configuration restoration when canonical runtime selection changed;
  retain the backup for the existing runtime owner's recovery procedure.
- Stage Recursive Agent payload copies and publish only a complete declared
  payload. Selected payload/gateway failures block completion, even with provider
  setup deferred. Existing complete payloads are preserved; completeness does
  not prove authenticity, freshness, behavior, or a running daemon.
- Store attempts separately from the last successful receipt. Require complete
  doctor evidence and matching selected revision before publishing success.
  Flush and atomically replace each receipt file. No atomic cross-file or
  power-loss durability guarantee is claimed.
- Preserve failing subprocess status across exported variable assignments,
  including gateway-unit identity calculation and toolchain path discovery.

## Validation

Initial development reproduced false-success and preservation defects with a
19-method baseline harness (21 assertion failures including subcases). A first
repaired candidate passed an expanded 28-method harness. The original website
21-test installer suite passed on baseline and its adapted suite passed on that
candidate. These are historical gates, not extra independent final test totals.

The execution environment was replaced before final publication; the exact
candidate source was recovered and checked against its Git blob
`f996735a6f11509e02499287e31aea043ee22387`. A restored/reviewed 28-method harness
passed again. Four final tests added two real failing counterexamples and two
positive controls: gateway identity errors hidden by export, and path aliases
bypassing separator validation. The two negative cases failed before the final
fix and pass afterward. An earlier fixture quoting mistake was corrected before
those counterexamples were evaluated; it was not treated as a product defect.

Final executed commands in a disposable Linux workspace:

```sh
python3 tests/test_ares_full_installer.py
# 32 methods passed; zero failures/errors/skips.
python3 -m pytest -q tests/test_ares_full_installer.py
# 32 passed; 17 subtests passed. Same tests, not additive evidence.
bash -n install.sh
# Passed. The suite also compiles all six actual embedded Python programs.
```

The tests use real Bash, disposable files/symlinks and real Git histories.
Native build/runtime, service, provider and transport boundaries are fixtures.
No network, host installation, provider call or live service change is performed.
The final source/mirror bytes match exactly. The earlier website suite was not
rerun on the final two-defect successor; its final exact-source run remains due.

## Remaining release gates

Keep the change a draft until exact-head CI, independent source review, the
canonical repository runner and clean-machine qualification pass. Local direct
stdlib/pytest execution is not full-repository dependency or CI parity.
ShellCheck/Ruff were unavailable locally. Actual uv download/install, native
builds, current SDK/wheel closure, real gateway/provider flows, Desktop packaging,
macOS/WSL, disk-full/power-loss, upgrades and installed rollback were not executed.
The website production build and rendered-HTML suite were not executed.

Required clean-machine cases: fresh full/minimal homes; repeat installation;
immutable-input mismatch refusal; customized-home preservation; full-to-minimal
refusal; provider setup/defer; disabled/unavailable/requested gateway; managed
update/backout; interrupted configuration; failed required dependencies. Retain
source identities, logs, tool versions, checks and failure evidence. A new
pip-check failure is a real gate, not something to waive as a fixture mismatch.

Website mirror: update `recursiveintell-web/public/ares/install.sh` to identical
bytes and adapt its receipt/build fixtures together. Website base was
`e939f81811b04553bf6ec3aa6ab1831702a22318`; its original test blob was
`80852a9daacae284add60a83b0d63509c093d763`. Do not deploy one copy independently.
The website's publication boundary requires separately authorized publication;
its local mirror artifact does not mean the served endpoint has changed.

The source recipe still builds current enhancement checkouts and deliberately
refuses same-Ares-revision/different-input immutable reuse. This change does not
silently repurpose source qualification pins as installer release authority.
The per-home lock assumes cooperating installers and operator-controlled paths;
it is not hostile same-UID isolation. Receipt selection is an observation, not
an activation lease. Pre-activation recovery covers named files, not toolchains
or OS packages; post-activation failures do not trigger an independent rollback.

## Rollback

Before merge, close the draft to decline the source change. After authorized
integration, revert the actual integration commit through review, restoring
canonical and mirror bytes together. No storage migration is introduced.
Source reversal is not installed binary/database rollback. Do not reset dirty
work, steal locks, delete recovery evidence, or rewrite protected release state.
