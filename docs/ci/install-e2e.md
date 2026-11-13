# Install / upgrade E2E canary

`.github/workflows/install-e2e.yml` ("Hermes cross-grade canary (advisory)") is
an advisory signal: it never gates a merge. A red scheduled run can be picked up
by `ares-scheduled-failure-watch.yml`, which can open a tracked issue for a
failing workflow.

## What runs

Two routes, each a reusable `install-e2e-run.yml` leg with its own sandbox,
its own install (uv, managed Python, Node, venv) and one update path:

| Route | Starts from | What the leg asserts |
|---|---|---|
| `installer` | every sampled release tag | re-running `install.sh` over an existing checkout reaches the tested commit, `hermes --version` runs, and the post-update Node health gate passes |
| `update` | release tags whose Git creator date is on/after `UPDATE_FROM_SINCE` | `hermes update` from that release reaches the tested commit, `hermes --version` runs, and the post-update Node health gate passes |

The installer route samples from the unfiltered release tags. The update route
filters tags by Git creator date and selects up to the requested count from
that filtered set; this date filter is a proxy for release eligibility and
does not establish that a qualifying release contains a given fix
(`scripts/sandbox/pick-release-tags.sh`).

## Observed failures that motivate the date floor

Hosted run `40598973759` (2026-10-27, `main` at `d672831a`), leg `update from
v2026.6.19`:

- `hermes update` printed `✓ Code updated!` and `✓ Update complete!`;
- the leg landed on the target commit and `hermes --version` ran;
- the leg then failed the post-update Node health gate:
  `required Node dependencies are missing or invalid (root manifest check)`.

The `installer from v2026.6.19` leg in the same run failed earlier, at installer
policy preflight:
`Installer policy preflight failed: Unsupported uv version; reviewed range is
0.9.28 through 0.12.x`.

These two observations are from one release in one run. They do not establish
that every earlier release fails the same way. The update route is filtered to
tags whose Git creator date is on or after 2026-10-29; this is a proxy for
release eligibility and does not establish that a qualifying tag contains the
reviewed-uv pin. The installer route's matrix remains unfiltered.

The installer route measures whether rerunning the installer works from each
sampled release; this run's installer leg failed at policy preflight.

## Re-enabling or moving the update floor

1. Use a release published on or after the reviewed-uv pin's landing, and verify
   that the pin is present in that release before using it as the update floor.
2. If that release should be the new floor, set `UPDATE_FROM_SINCE` in
   `.github/workflows/install-e2e.yml` to its tag date (or later).
3. `gh workflow run install-e2e.yml -f route=both -f tag-count=1`. Read the
   `pick-releases` log for the installer matrix and the filtered update
   matrix; if that matrix is empty, the log prints a notice that no tag meets
   the Git creator-date cutoff. Check the dispatch inputs as well, since the
   update job can also be skipped when the selected route is `installer`.
4. Confirm the update leg starts from the intended release before treating its
   result as coverage.

## Evidence boundaries

- An `installer` green leg is evidence for the installer path from that release,
  not for `hermes update` from it.
- When the `update` job is skipped, it indicates that no update leg ran;
  check the `pick-releases` output and dispatch inputs to determine why.
- A green canary is not a release gate and does not certify Desktop, gateway or
  provider behaviour.
- The canonical Ares installer (`install.sh --ares-source`) and
  `ares setup → doctor → rollback` are not covered by this workflow.
