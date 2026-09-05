# Review Output Template

This is a presentation aid under `github-code-review/SKILL.md`, not an instruction or authorization to publish. Return findings to the user by default. A draft, an internal recommendation, a posted comment and a submitted formal review are different states.

Use only useful sections; do not invent praise or findings to fill the template. For a blockers-only or delta request, omit unrelated observations.

```markdown
## Review of the inspected revision

Subject: [upstream repository / PR / head repository / exact reviewed SHA]
Observed: [timestamp]
Scope: [paths and behaviors inspected; important exclusions]

### Supported blockers or defects
- [ID] [path:line at reviewed SHA]: [reachable cause and consequence].
  Evidence: [source/test/observation and its origin].
  Required correction or proof: [smallest specific acceptance gate].
  Confidence: [supported / plausible risk / unproven].

### Optional improvements
- [Only material, nonblocking improvements relevant to the request.]

### Verification and remaining gates
- [Exact command/oracle, tested subject, raw result and scope.]
- [FAIL / BLOCKED / NOT_RUN / SKIP where applicable, with reason.]
- [Maintainer-controlled gate distinguished from contributor-actionable work.]

Publication: [not posted / draft only / exact authorized action and returned ID]
```

## Judgment is not a GitHub action

“No supported blocking defect found in the inspected scope” is an internal conclusion. It is not a submitted APPROVE review and not a guarantee that the code is secure.

“Changes are needed before the claimed behavior is proven” is a finding. It is not automatically a submitted REQUEST_CHANGES event.

Submit COMMENT, APPROVE or REQUEST_CHANGES only when the actual user request grants that specific action and target, subject to host requirements. Revalidate the current head before publishing; do not attach stale findings to new code silently. A generic comment grant is not an approval grant.

## Severity and blocker classification

Assess consequence and reachability separately from confidence and merge policy. A demonstrated authorization bypass, destructive data error or core regression may be blocking. A missing test may be a required acceptance gate or a nonblocking improvement depending on the requested claim. A style preference is not a security blocker.

Do not turn every warning into a request-changes event. Preserve reviewer/maintainer intent and distinguish a current unresolved requirement from a historical fixed finding.

## Inline findings

A useful inline finding states the invariant, where the current path violates it, why that matters, and the smallest correction or discriminating test. Bind the finding to the reviewed commit and valid file/line. Do not add speculative allegations or generic praise at arbitrary lines.

If authorized to publish, prefer one coherent communication rather than automatically posting a formal review and a redundant summary comment. Record returned IDs and read-back state. Reconcile ambiguous results before retrying.

## Local review and follow-ups

For local code, replace PR metadata with the worktree, base/head and dirty-state scope. Do not change branches or discard edits as a presentation step.

For follow-ups, bind the prior and current reviewed revisions. Mark earlier findings still open, fixed with evidence, changed but unproven, superseded or unresolved. Report only genuinely changed or invalidated evidence unless a new full review was requested.
