# Reviewed UV policy during the POSIX installer

## Scope and compatibility

The non-Termux Python dependency stage of `scripts/install.sh` now uses
`scripts/install_uv_policy.py`. `UV_NO_CONFIG=1` still isolates user/system
configuration. The helper projects reviewed configuration from the original
`pyproject.toml` into an exclusively created, mode-0600 temporary file in that
same directory, and passes it explicitly to `uv sync --extra all --locked`.
The original project metadata and relative-path context remain authoritative.

A missing/malformed policy or lock, unsupported policy, or failed locked sync
now stops the install with its failure status. There is no unlocked pip fallback.
The stage preserves child exit statuses, including 124, and returns 128+signal
for a signal-terminated child. The earlier best-effort Termux pip/constraints
branch is unchanged and is a separate acceptance gate. PowerShell, updates,
and downstream bootstrap installers are outside this correction's scope.

Supported configuration is deliberately finite:

- `exclude-newer` must be `14 days`
- Every existing `exclude-newer-package` entry is preserved; this initial
  contract supports only the manifest's reviewed false-valued exceptions
- `find-links` supports local paths, kept verbatim in the same origin directory
- `no-index` supports a TOML boolean
- `override-dependencies` stays native; simple registry requirement strings only
- `sources` stays native; simple local-path entries only

Unknown keys, index declarations, URL/Git/index sources, other native metadata,
malformed types, and inline credential/query-bearing URLs fail before execution.
Errors do not echo input keys, configuration values, or environment values.
Future settings require an explicit reviewed contract extension, never omission.
The helper does not introduce or remove age exceptions, rewrite locks, or waive
aging. The reviewed manifest remains the policy owner.

Every inherited `UV_*` variable is rejected except:

- `UV_NO_CONFIG=1`
- `UV_PYTHON` and `UV_PROJECT_ENVIRONMENT` exactly matching the installer-owned
  interpreter and environment, which the helper also sets explicitly
- `UV_OFFLINE=1` and `UV_PYTHON_DOWNLOADS=never` (strictly restrictive controls)
- Absolute `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR` paths
  (storage/discovery locations; the interpreter is explicitly bound)

Existing users relying on other UV environment overrides must remove them
before this stage can proceed. No TLS, network trust, certificate, or auth
settings are changed. Supported version range is 0.9.28 through 0.12.x;
unknown newer versions fail closed pending review. Local execution proof is
limited to existing uv 0.9.28 and 0.12.19; accepting the range is not a claim
that every intervening release or hosted 0.12.21 was executed locally.

## Cleanup and validation

Normal success/failure and catchable signals terminate/reap the subprocess and
terminate remaining process-group descendants before deleting the temporary
configuration. SIGKILL or host loss cannot guarantee cleanup; residual files
have private permissions, no inline secrets, random names, and are never named
`uv.toml`, so uv does not automatically discover them. The helper checks that
manifest and lock bytes remain unchanged; it does not undo unexpected changes.

Focused behavioral tests execute the real installer stage using disposable
command fixtures. They cover policy refusal without secret echo, exact status,
no fallback, private same-origin creation, missing files, unsupported versions,
cleanup on failure/signals, exited-leader descendants, actual offline uv
relative wheel discovery, and native override/path-source preservation.

Canonical command:

```sh
HERMES_PYTHON=/path/to/test/python bash scripts/run_tests.sh tests/test_install_uv_policy.py -q
```

Exact current-manifest offline probes on both local versions reproduced failure
with only `UV_NO_CONFIG=1`, then passed helper lock checks and locked dry runs.
The source lock and manifest were byte-identical afterward. Dry runs described
packages that would be installed; no package was downloaded or installed.
A poisoned user configuration was ignored. These results do not certify a live
installation, all platforms, providers, or the original scheduled npm failure.

## Rollback

Revert only this scoped helper/integration/tests/documentation change. Reverting
restores the diagnosed policy-loss and unlocked-fallback behavior; it does not
repair that behavior safely. Never regenerate a lock, add age exceptions, change
trust settings, or delete retained failure evidence as a workaround.

## Hosted scanner correction

The first PR head failed the repository-wide Windows footgun gate because the
version probe lacked explicit text encoding and cancellation used unguarded
POSIX-only symbols. The correction decodes version output as strict UTF-8 and
validates POSIX identity plus every required signal/process-group capability
before executing uv or creating a temporary config. Missing capabilities fail
closed; this does not add Windows support or weaken descendant cleanup.
The scanner and its suppressions are unchanged. Regression coverage includes
missing capabilities, unsupported platform input, and valid/invalid UTF-8 under
an ASCII locale, alongside the existing cleanup and offline policy proofs.
