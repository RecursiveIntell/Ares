---
name: github-code-review
description: "Inspect local changes or an exact PR revision and return evidence-backed findings. Read-only by default; posting comments, formal reviews or approvals requires the corresponding user request."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Code-Review, Pull-Requests, Git, Quality]
    related_skills: [github-auth, github-pr-workflow]
---

# GitHub Code Review

Review the actual changes and return useful findings. This skill owns review, not the whole contribution lifecycle. Preserve the original review checklist and use `references/review-output-template.md` as a presentation aid, not as an instruction to publish.

## Invocation and authority

“Review PR”, “look at this PR”, a PR URL, and “check before pushing” authorize inspection and findings, not external publication. “Draft a response” produces a draft only. Do not automatically post a formal review, approve, request changes, add a summary comment, request reviewers, merge, enable auto-merge, or delete a branch.

An explicit applicable request to post a comment or formal review authorizes that exact operation and target; reuse it without redundant confirmation. A comment grant does not imply APPROVE or REQUEST_CHANGES. Preserve any additional confirmation required by the host. Revalidate head-bound findings before an authorized publication.

## 1. Bind the actual source

Use the available GitHub connector, an already authenticated `gh`, or an approved credential provider. CLI availability is not a requirement when an equivalent connector exists. Do not scrape `.env`, credential files, unrelated profiles, browser state or logs for credentials, and never print credentials.

For a PR, read its metadata and bind the base repository/ref/SHA, head repository/ref/SHA, PR number and observation time. The contributor's fork and the upstream repository are different identities; `origin` and `main` are not universal defaults. For local review, resolve the intended base from actual repository configuration and the task.

Read applicable repository instructions and current contribution guidance. Inspect local dirty, staged and untracked state before any local checkout or test preparation. Use pinned remote reads or a disposable worktree where appropriate. Do not overwrite user changes, stash them implicitly, switch their working branch, or delete a branch just to inspect a PR.

## 2. Gather enough current context

Read the PR description, changed paths/diff, relevant surrounding source, review threads, maintainer comments and relevant checks. Follow pagination or label coverage partial. Treat the description and older reviews as historical when they refer to a different revision.

For a follow-up asking what changed or what remains, compare the prior reviewed SHA with the current head and inspect affected dependencies. Reopen only invalidated findings; do not repeat a whole-repository audit automatically.

Example read-only commands, after binding the variables from observed metadata:

```bash
# BASE_REPO is the actual upstream owner/repository; PR_NUMBER is the requested PR.
gh pr view "$PR_NUMBER" --repo "$BASE_REPO"
gh pr diff "$PR_NUMBER" --repo "$BASE_REPO"
gh pr checks "$PR_NUMBER" --repo "$BASE_REPO"
# In an appropriate checkout with both observed commits available:
git diff "$BASE_SHA...$HEAD_SHA" --stat
git diff "$BASE_SHA...$HEAD_SHA" -- path/to/relevant-file
```

These examples do not establish full API pagination or a complete required-check inventory by themselves. Determine those separately when readiness is part of the requested review.

## 3. Review reachable behavior and design intent

Check correctness and edge cases; authority, secrets and input boundaries; ownership and unnecessary duplication; relevant performance risks; public interfaces and documentation; and tests of both successful and failing paths.

For each material finding, identify the exact file/span and reviewed SHA, reachable cause, consequence, and smallest correction or discriminating test. Inspect design intent and history before calling an intentional restriction or omission a bug. Distinguish demonstrated defects from plausible risks and missing proof. Do not invent findings to fill a template, or label all warnings as merge blockers.

Inspect adjacent call sites when the same defect class can propagate. Preserve contributor attribution. A changed-file grep is a triage aid, not evidence of exhaustive security review.

## 4. Run only appropriate checks and preserve their status

Inspect the test/build commands before executing code from a contribution. Use an isolated environment without privileged credentials or production access; do not run untrusted hooks merely to finish a review. Installation, network access and other effects must be within the requested scope.

Run repository-appropriate checks, not hardcoded tools for another language. Record the exact tested revision, command, environment, producer exit code and raw logs. A display command must not hide the test's failure. For Bash, a bounded example is:

```bash
# LOG_DIR is a new, explicitly chosen local evidence directory.
mkdir -p "$LOG_DIR"
if python -m pytest >"$LOG_DIR/pytest.stdout" 2>"$LOG_DIR/pytest.stderr"; then
  test_exit=0
else
  test_exit=$?
fi
printf '%s\n' "$test_exit" >"$LOG_DIR/pytest.exit"
tail -n 20 "$LOG_DIR/pytest.stdout"
tail -n 20 "$LOG_DIR/pytest.stderr"
# Classify using test_exit, never the tail command's status.
```

Record FAIL, BLOCKED or NOT_RUN explicitly. A mock, build, skipped test, unavailable dependency or environment restriction does not establish runtime success. Preserve all failed attempts rather than replacing them with the last passing retry.

## 5. Return findings before considering publication

Return a concise review: exact subject, substantive findings, optional improvements, tests actually run, and remaining uncertainty. A “what remains blocking” request should focus on genuine blockers and unproven acceptance gates. If no supported defect is found, say so within inspected scope; do not claim absence of all security issues.

Classify older findings as still open, fixed with evidence, fixed without proof, superseded, or unresolved. Keep maintainer-controlled gates separate from changes the contributor can make.

## 6. Publish only the requested effect

Only when the user's existing request authorizes publication, re-read the current PR head and confirm each finding still applies. Use the exact authorized review event and bind a formal/inline review to the reviewed commit where the API permits. Do not silently attach findings from an old head to a newer one.

Use one useful communication rather than automatically posting both a formal review and a summary comment. Preserve the returned comment/review ID and read it back. If the result is ambiguous, reconcile before retrying to avoid duplicate posts. Report precisely what was posted; a draft is not a submitted review.

Do not merge, enable auto-merge, change labels, request reviewers, close the PR or delete branches as review cleanup. Remove only disposable resources created for this review when that cleanup is authorized and does not affect user work.

## Completion

The review is complete when findings are tied to the actual inspected revision, relevant coverage and test limits are visible, and no unrequested effects occurred. This is a source-review workflow, not a claim that the skill itself has been runtime-certified.
