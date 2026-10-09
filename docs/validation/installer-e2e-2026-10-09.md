# Installer E2E investigation — October 8–9, 2026

Evidence state: a sandbox-specific Node trust defect is reproduced; the original
installer exit 217 remains unresolved. This candidate is not an installation,
packaging, release, or live-runtime qualification.

## Source and scope

- Repository: `RecursiveIntell/Ares`.
- Inspected main and original workflow source:
  `e3e8a39d9427354eeba014c21c9c916078fb0cdb`.
- [October 8 run 37794185539](https://github.com/RecursiveIntell/Ares/actions/runs/37794185539):
  five installer failures and five updater successes.
- [October 7 run 37637898257](https://github.com/RecursiveIntell/Ares/actions/runs/37637898257):
  the same installer failure split.
- Complete logs for every October 8 installer and updater job, and every October
  7 installer job, were inspected. All ten October 8 preserved artifact ZIPs
  were downloaded and verified against GitHub's SHA256 metadata.
- [PR #136](https://github.com/RecursiveIntell/Ares/pull/136) remains an unmerged
  draft. Its diagnostic v2 helper and tests are reused byte-for-byte from
  `3f52dac495e0506fc783af2704afab5c99405b0e`; no other composition changes are
  included. Its original qualification excluded installation and packaging.
- [PR #135](https://github.com/RecursiveIntell/Ares/pull/135) concerns dependency
  advisory pins. This candidate does not change dependency pins or lockfiles.

## What the preserved evidence establishes

All five historical installations reached their completion message, but also
warned that npm installation failed. Each current installer rerun completed
the reviewed, locked Python installation before failing at root npm; none
reached the TUI dependency stage.

| Starting release | Installer job | Artifact | Root npm status | Elapsed | Proxy SSL EOF events |
| --- | ---: | ---: | ---: | ---: | ---: |
| v2026.3.12 | 113371337916 | 11558114298 | 1 | 73s | 44 |
| v2026.3.30 | 113371337329 | 11558566429 | 1 | 72s | 44 |
| v2026.4.23 | 113371337209 | 11558965507 | 1 | 73s | 44 |
| v2026.5.28 | 113375363104 | 11559790322 | 1 | 72s | 44 |
| v2026.6.19 | 113376556137 | 11559336431 | 217 | 1s | 0 |

Each v1 record had empty `npm_codes` and `safe_causes`, no timeout, and no
reported output truncation. These records cannot distinguish empty output from
an omitted, unrecognized error. Raw npm output was deliberately discarded at
the diagnostic privacy boundary, rather than retained in the artifacts.

The green updater jobs are not evidence of healthy Node dependencies:

| Starting release | Updater job | Preserved update-stage evidence |
| --- | ---: | --- |
| v2026.3.12 | 113371337559 | npm stage announced; no usable dependency result |
| v2026.3.30 | 113371337642 | npm stage announced; no usable dependency result |
| v2026.4.23 | 113371337450 | root, TUI, and web npm failure warnings |
| v2026.5.28 | 113371337608 | two npm exit-handler errors; ENOTEMPTY rename with errno -39; root, TUI, and web failure warnings |
| v2026.6.19 | 113371337350 | two npm exit-handler errors; root and web failure warnings |

The old harness checked target HEAD and `hermes --version`; neither requires
working Node dependencies. Updater artifacts retained only the initial install
transcript, although complete job logs exposed the update-stage failures.

## Reproduced defect and bounded correction

The sandbox configures an HTTPS MITM proxy that signs certificates with its
throwaway `ca.pem`. Curl, Python, and Git receive that CA, but the original
`NODE_EXTRA_CA_CERTS` pointed only at `real-ca.pem`. Node therefore lacks the
issuer of proxied registry certificates.

A disposable loopback fixture executed the actual sandbox proxy and real npm.
With the original Node trust projection, npm rejected the certificate with
`UNABLE_TO_VERIFY_LEAF_SIGNATURE`, and the proxy logged the same
`SSLEOFError`/`UNEXPECTED_EOF_WHILE_READING` shape as CI. Including the sandbox
CA made the registry request succeed with TLS verification enabled. The
regression also runs the actual stage-2 shell through a capture-only bwrap
double, then uses its emitted CA configuration for a real npm package install.
This exercises trust configuration and package download without requiring
privileged namespaces or external registry traffic.

The correction extends Node's extra trust with both the sandbox and existing
real CA bundles, inside the disposable sandbox. The proxy's upstream connection
continues verifying exclusively against the real CA bundle. No production
installer trust setting is changed, and no TLS or package-integrity check is
disabled.

This reproduced defect explains the four proxy-associated failure signatures.
Because their original raw npm errors were discarded, it does not prove every
failure in those four jobs has been eliminated. A fresh matrix remains required.

## Exit 217: unresolved, not inferred from arithmetic

An npm errno of -39 can map to shell status 217. The updater v2026.5.28 log
independently shows ENOTEMPTY, but it is a different invocation and does not
establish the cause of the v2026.6.19 installer failure.

An offline test created a stale npm retirement directory and changed a local
package version. Available npm 11.9.0 repaired it successfully; `npm ci` also
passed. This falsifies the proposed blanket stale-directory explanation in
that environment. No destructive module cleanup or install algorithm change
is justified by that test.

Diagnostic v2 now recognizes ENOTEMPTY and additional issuer errors, describes
the bounded output shape, and reconstructs collected records strictly. Unknown
text, URLs, headers, auth data, npm cache/debug logs, and user npmrc contents
remain excluded. It deliberately does not derive a cause from exit status.

## E2E evidence and acceptance changes

After either route reaches the target revision, the harness now checks the
required root, TUI, and web dependency trees with real npm. Missing dependencies
fail even when the Python CLI version smoke passes. Optional desktop
dependencies are excluded from this gate; install defaults are preserved.

The updater transcript is retained with `tee` and `pipefail`, matching the
installer's transcript handling. Raw npm output from the dependency probe is
suppressed; the gate emits only a fixed failure message. Sandbox logs are
collected on dependency-gate failure.

## Validation and limits

The untouched baseline's diagnostic and installer-stage suites passed 25
tests. Reusing diagnostic v2 increased that focused selection to 45 passing
tests. The initial six-file selection passed 65 tests with no failures or file
retries. Independent review then reproduced a missing-transitive-dependency
counterexample to the depth-zero health gate. The gate now checks the full
selected dependency trees, with a regression for each required surface. The
final six-file selection passed 68 tests with no failures or retries. It includes
15 dependency-health tests, three real proxy/TLS/integrity
tests, and nearby Node discovery/global-prefix regressions. TLS tests first
ran on the original projection: two failed and the untrusted-CA negative
control passed; all three passed after the correction. Shell syntax, scoped
Ruff checks, and `git diff --check` also passed.

Local runtime: Linux, Python 3.12.14, Node 24.19.0, npm 11.9.0. These are not
the complete GitHub runner toolchain. The controlled npm fixture tests the
actual TLS and dependency-probe boundaries, not the repository's entire
dependency installation, package lifecycle, supply-chain age policy, or desktop
build.

The full local bubblewrap replay is blocked: a namespace capability probe
exited 1 with `Failed to create NETLINK_ROUTE socket: Operation not permitted`.
No permission setting or namespace isolation was bypassed. The full Ares test
suite, macOS/Windows installers, packaging, providers, and live services are
outside this bounded correction.

Reproduce the focused tests from a clean checkout with a Python environment
containing pytest, pytest-asyncio, pytest-timeout, and PyYAML; real node, npm,
bash, and openssl must be on PATH:

```bash
HERMES_PYTHON=/absolute/path/to/test-venv/bin/python scripts/run_tests.sh -j 2 --file-retries 0 tests/test_npm_failure_diagnostics.py tests/test_install_sh_node_deps_failure.py tests/test_sandbox_node_tls.py tests/test_install_e2e_node_health.py tests/test_install_sh_node_npm_check.py tests/test_install_sh_node_global_prefix.py
bash -n scripts/sandbox/stage2-run.sh scripts/sandbox/check-node-deps.sh tests/install/install-update-e2e.sh
git diff --check
```

On a Linux host that supports the required unprivileged namespace topology,
run both routes from all five sampled releases, with logs outside the checkout:

```bash
HERMES_E2E_LOG_DIR=/absolute/path/outside/checkout/installer-logs tests/install/install-update-e2e.sh --route installer --install-ref v2026.6.19
HERMES_E2E_LOG_DIR=/absolute/path/outside/checkout/update-logs tests/install/install-update-e2e.sh --route update --install-ref v2026.6.19
```

Repeat for v2026.3.12, v2026.3.30, v2026.4.23, and v2026.5.28, or manually
dispatch `install-e2e.yml` on the candidate branch with `route=both` and
`tag-count=5`. The supported GitHub connector exposes reruns but no branch
workflow-dispatch action; rerunning old main would not test this candidate.

The acceptance gate is a fresh matrix with target HEAD, CLI smoke, required
Node dependency checks, and current-run diagnostics. Any remaining exit 217
must be classified from its new diagnostic, not guessed. Missing or failed
qualification keeps this PR unmerged and does not authorize activation.

Rollback: close the unmerged PR; if subsequently reviewed and merged, revert
this isolated change set. No profile, provider credential, service, installed
runtime, dependency version, or installation-default migration is part of it.
