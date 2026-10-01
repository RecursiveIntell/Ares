# Bounded npm installer diagnostics

Scheduled [run36789091929](https://github.com/RecursiveIntell/Ares/actions/runs/36789091929)
on source `ea414e66b17dca14486e5334fbe2d48790bd07bb` passed the CLI version check
for all five historical initial installs, then failed all five **installer reruns
over existing checkouts**. All five separate updater routes passed. This is not a
fresh-install failure claim or installed acceptance for current main.

Four reruns failed after roughly71–73seconds with sandbox TLS EOF messages; the
newest failed after roughly1second with an empty proxy log. npm's precise cause
was unavailable: `--silent` suppressed errors, the temporary captured output was
removed, and archived installer logs contained no npm debug records. These
observations do not prove a shared TLS cause or expiry of the600second deadline.

## Repair boundary

- Root and TUI npm dependency stages use error-level output, capture actual child
  status, and preserve it through the existing installer stage protocol, including
  status124. A124 record is a timeout-status observation, not proof that npm itself
  did not choose that exit code
- Only bounded structured facts leave the raw-output boundary: stage, exit status,
  elapsed seconds, known npm error codes, fixed safe descriptions and truncation
  state. Arbitrary text, package URLs, environment, `.npmrc`, auth configuration,
  unknown codes and raw debug logs are never emitted by this diagnostic helper
- The E2E collector reconstructs and validates records from the current installer
  transcript, rather than copying even purportedly sanitized free text. It keeps
  at most four records, refuses output collisions, and does not traverse caches.
  Each E2E invocation allocates a unique evidence subdirectory so reuse of the
  configured parent cannot relabel an old failure artifact as the current run
- Raw captured npm output remains in a private mktemp file during the command and
  is removed afterward. Missing/failed diagnostic helpers do not print raw output,
  mask npm failure, or convert a failure to success

No TLS verification, trust store, network configuration, package/dependency,
installer success policy, source identity or native guard change. No hosted test
was blindly rerun. A diagnostics fix does not certify that the original npm issue
is repaired or that all installer paths work.

Validation covers real installer stage invocation with injected npm success,
nonzero statuses, an actual timeout, empty output and credential-like content
(Authorization/Bearer, tokenized URLs, npmrc-style entries and multiline secrets).
Collector tests cover malicious extra fields, modified safe text, input/record
bounds, symlinks, FIFOs and output collisions. No live package installation is
needed for these tests. Rollback is a scoped commit revert.

## Separate UV configuration recommendation

The failing run also reported removal of global exclude-newer during locked sync,
then fell back successfully to unlocked resolution. `scripts/install.sh` globally
sets `UV_NO_CONFIG=1` to avoid inheriting another user's configuration, while its
locked tier invokes `uv sync --extra all --locked`. The repository's reviewed
`pyproject.toml` exclude-newer settings are recorded in `uv.lock`. This is a
separate configuration/lock-contract concern, not the npm failure. Reproduce with
the exact uv version and an isolated miniature project before changing it; preserve
inherited-config isolation and explicitly bind reviewed repository settings.
Do not solve it by globally enabling arbitrary inherited configuration. This patch
does not modify that policy or claim hash-verified installation acceptance.
