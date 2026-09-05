---
name: github-pr-workflow
description: "Operate the requested part of a GitHub PR lifecycle with exact fork/base/head identity, scoped writes, and honest CI evidence. Inspection does not authorize pushing, posting, merging, or branch deletion."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git, Automation, Merge]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request Workflow

Own PR mechanics, not code-review judgment or a second end-to-end implementation workflow. `github-issue-to-pr` owns an explicitly requested issue-to-PR task; `github-code-review` owns review. Execute only the requested lifecycle stages, preserving existing authorization rather than asking again for the same approved action.

## Scope and effect boundaries

Read/status/draft requests produce observations or drafts. Explicit requests to implement, commit, push, open a PR, edit a description, post a comment, submit a formal review, request reviewers, merge, enable auto-merge, or delete a branch are distinct grants. A request can explicitly cover several stages; do those stages without redundant permission questions. Capability and credentials do not create a grant. Preserve host confirmation requirements.

Opening a PR does not authorize merge, approval, unsolicited issue comments, or branch deletion. Green checks do not create merge authority. A failed or ambiguous write does not authorize blind retries.

## 1. Resolve identity and the actual environment

Read repository instructions and the live contribution target. Record separately:

- Base repository, branch and current commit; its actual default branch when relevant.
- Head repository, branch and exact commit; the fork is not necessarily the upstream.
- PR/issue number, current review subject and observation time.
- Local checkout/worktree, remote mapping, staged/unstaged/untracked state when using local Git.
- Requested effects, target paths/refs and applicable restrictions.

Do not assume `origin`, `main`, a particular merge method, or that the checked-out branch is the requested PR. Resolve defaults from repository metadata. Preserve contributor authorship and accepted upstream history; do not replace another contributor's work or squash away credit by habit.

Prefer an available GitHub connector or already authenticated `gh`. An approved Git credential provider may supply authentication without revealing secrets. Do not scrape `.env`, credential stores, browser state, unrelated profiles or process logs. Missing CLI does not imply missing connector access. Equivalent transport must preserve identity, scope, effect type and read-back checks; otherwise report the missing capability.

## 2. Prepare only the requested change

For authorized implementation, inspect the current base before creating an isolated branch/worktree. Leave unrelated user changes untouched. Follow current naming and commit conventions; `references/conventional-commits.md` is an aid, not authority over repository policy.

Stage an explicit reviewed file allowlist, never the whole working tree as an auto-fix shortcut. Inspect the staged diff before committing. Inspect build/test commands and hooks before running contribution code; use a disposable environment without privileged credentials when code is untrusted.

```bash
# Set these paths from the reviewed change; they are examples, not mandatory files.
git add -- path/to/changed-source path/to/regression-test
git diff --cached --check
git diff --cached --stat
git diff --cached
# Commit only after verifying that the staged tree matches the authorized scope.
```

Preserve raw command exits and logs. Do not infer a test passed from the exit of `head`, `tail` or another display process. A failed, skipped or unavailable check is not a passing gate.

## 3. Publish an authorized branch or PR

Before pushing, re-read the relevant remote target and check that the intended update still applies. Use a normal non-force push unless history rewriting was specifically authorized. Where the operation supports an expected-old-ref or expected head, bind it; a non-force update prevents non-fast-forward changes but is not a universal compare-and-swap guarantee.

For a PR, bind the actual upstream base and fork-qualified head. Use the repository's real templates and contribution policy. `templates/pr-body-bugfix.md` and `templates/pr-body-feature.md` are optional starting points. State problem, implementation, tests actually executed, risk, exclusions, and the exact candidate revision. Do not convert unchecked test boxes into green claims.

Read back the created/updated object and verify its ID, base, head, files, title/body and requested state. If publication succeeded but the response was lost, search/reconcile the exact intended object before retrying. Preserve partial completion.

Record the actual operation: local commit, branch push/ref update, PR creation/edit, comment, formal review, merge or deploy. A direct branch update is not a PR merge. A source push is not deployment or browser validation.

## 4. Reconcile current CI and reviews

Use current PR metadata to identify the correct head and any separately tested merge candidate. Inspect relevant check runs, commit statuses, workflow runs/jobs, required checks when visible, reviews and unresolved requests. Follow pagination. Distinguish inaccessible, partial, empty and complete results. An empty check-runs response alone does not prove green CI.

Bind each result to its tested SHA/run ID and observation time. Keep historical tests and reviews, but do not let them certify a newer revision without justified applicability. Re-evaluate only affected proof after a change; preserve unaffected evidence with its basis.

Classify gates separately: introduced code failure, baseline failure, infrastructure failure, pending execution, maintainer-controlled approval, merge conflict, stale evidence, or unknown coverage. Permission to run CI is not code approval; passing CI is not a complete security or product correctness certificate.

`references/ci-troubleshooting.md` can guide diagnosis after verifying it matches the actual runner and failure. Do not alter tests to hide failures or bypass required checks.

## 5. Fix within the existing request and close the current turn

For an authorized CI repair, read the first meaningful failure, reproduce where appropriate, make the smallest complete correction and test the original failure plus affected siblings. Apply a predeclared retry/compute budget. Retry a suspected flake only with evidence and retain the failed run; repeated retries are not proof of correctness.

A request to inspect failures does not authorize a push. An existing request to fix and push does, within its original scope. Stop at new destructive effects, changed targets, missing privileges, or exhausted budget; report completed actions and the exact remaining gate rather than restarting planning or asking an already-answered question.

Do not promise future monitoring from a one-shot conversation. A brief bounded check during this execution is distinct from a scheduled watcher; future monitoring requires an available scheduling tool and an actual configured task.

## 6. Merge and cleanup only when requested

Re-read head, current gates and repository policy immediately before an authorized merge. Use the explicitly selected or policy-required merge strategy and preserve attribution. Bind expected head when supported and stop on a mismatch. Do not bypass protection or fabricate approval.

Auto-merge is its own future effect and must be requested. Local/remote branch deletion is separate from merge and must be within the grant; never delete the user's working branch as an automatic epilogue. Cleanup of disposable resources is limited to resources created for this task and must not remove user work.

## Output and completion

Return the exact contribution identity, operations completed with returned IDs/commits, current check/review coverage, genuine remaining gates, and the smallest next action. For a delta question, report only changed or invalidated evidence plus necessary context. Do not claim merged, released, deployed, fully green, or runtime-certified unless the corresponding evidence actually exists.
