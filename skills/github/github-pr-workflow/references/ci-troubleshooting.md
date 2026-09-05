# CI Troubleshooting Reference

This reference supplies diagnostic examples under `github-pr-workflow/SKILL.md`. It does not grant edits, pushes, workflow reruns, permission changes or monitoring. Inspecting CI remains read-only; an existing scoped fix-and-push request can cover its named repair without redundant confirmation.

## Bind the failed execution first

Record the actual upstream repository, PR, head SHA, any separately tested merge SHA, workflow/run attempt/job, command, runner image/toolchain and observation time. Read the first meaningful failure and the relevant surrounding logs, not just the final cascading error.

Use the available connector or authenticated tooling. For an identified run, a read-only example is:

```bash
gh run view "$RUN_ID" --repo "$BASE_REPO" --log-failed
```

A logs archive is untrusted input. Use a fresh directory, validate entry paths and symlinks, preserve original bytes, and avoid overwriting shared `/tmp` paths. Do not print secrets found in logs. Do not scrape credential files to make the command work.

## Diagnose before changing

| Failure class | Discriminating evidence | Appropriate correction boundary |
| --- | --- | --- |
| Assertion failure | Expected invariant and its source; failure on exact baseline versus candidate | Fix behavior or correct an actually obsolete test contract. Never change the assertion merely to make CI green. |
| Import/dependency failure | Package context, working directory, environment, lockfile, actual import path | Fix the demonstrated packaging/environment cause. An import error does not automatically mean a new dependency is needed. |
| Lint/formatting | Repository formatter/version and affected lines | Format only scoped files and inspect the resulting diff. Do not normalize the entire repository by habit. |
| Type failure | Actual value flow, public contract and intended representation | Repair the real type/behavior mismatch. A cast or ignored error does not prove correctness. |
| Build/version mismatch | Toolchain/target and manifest/lockfile differences | Use the project's dependency workflow and inspect the lockfile diff. Do not freeze the whole local environment into project requirements. |
| Permission/authentication | Exact denied operation, token integration boundary, fork security policy | Distinguish intended isolation from missing authorized access. No implicit token-scope expansion, secret access or security bypass. |
| Timeout/cancellation | First stalled step, CPU/memory/process/network evidence, cancellation origin | Diagnose the cause before raising time limits or adding parallelism. A post-effect timeout may require reconciliation, not replay. |
| Container failure | Actual Dockerfile step, build context, ignore rules, base-image identity | Correct demonstrated path/context/image problems; inspect consequences before replacing an image or changing sandbox permissions. |
| Suspected flake | Repeated independent evidence or a known infrastructure incident | At most the declared retry budget with a recorded reason; retain every failed attempt. A pass after retries does not erase instability. |

Use a clean baseline comparison when necessary to distinguish a diff regression from pre-existing or infrastructure failure. Inability to reproduce inside a restricted environment is a missing gate, not proof of a product defect or an invalid issue.

## Execute and preserve results

Only when repair is authorized, make the smallest complete correction and inspect sibling paths for the same bug class. Use an isolated worktree/environment where needed. Do not run untrusted PR code with privileged credentials or production access.

Capture the producer's exit independently of log display:

```bash
# Replace the command with the repository's inspected test entrypoint.
if python -m pytest >"$LOG_DIR/test.stdout" 2>"$LOG_DIR/test.stderr"; then
  test_exit=0
else
  test_exit=$?
fi
printf '%s\n' "$test_exit" >"$LOG_DIR/test.exit"
tail -n 20 "$LOG_DIR/test.stdout"
tail -n 20 "$LOG_DIR/test.stderr"
# Classify the test using test_exit, not the display commands.
```

`LOG_DIR` must be a new explicitly chosen evidence directory. Preserve complete logs and test/config/source identity. Keep FAIL, BLOCKED, SKIP and NOT_RUN distinct. Do not suppress diagnostic stderr or replace a failed command with a successful explanatory echo.

Stage only a reviewed path allowlist. Commit and push only when the existing request covers those actions and targets; never `git add .` as a generic CI repair step. Re-read the remote candidate after publication.

## Reconcile the full relevant CI surface

Inspect check runs, commit statuses and relevant workflow jobs/attempts on the correct subject. Determine required-check coverage when accessible. Follow pagination and retain incomplete or inaccessible coverage. Empty check runs alone are not a green verdict.

A one-shot status inspection is not a continuing watcher. Future monitoring requires a real available scheduling mechanism and an actually created task. Do not hide a failed watcher behind a fallback success message.

## Return only useful next actions

Report the demonstrated cause, actual scoped change, executed checks, current hosted result and the remaining gate. Separate locally actionable fixes from maintainer-controlled CI approval or integration permissions. Do not weaken test assertions, security boundaries or required checks to create apparent completion.
