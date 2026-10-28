# CI lane policy

This document is the source of truth for how a CI lane moves between
**advisory**, **required**, and **quarantined** states, and how CI health is
measured. The measured rule is implemented by
`scripts/ci/ci_health_report.py` (`should_graduate`, `should_quarantine`);
this page must stay consistent with it.

## States

| State | Meaning | Enforcement |
|---|---|---|
| advisory | Lane runs and reports but cannot block a merge | Not in the required-check evaluator's applicable set |
| required candidate | Advisory lane being measured for promotion | Tracked by the CI health report streaks |
| required | Lane failure blocks the PR gate | Listed in `scripts/ci/evaluate_required_checks.py` |
| quarantined | Required lane temporarily excepted from the gate | Explicit evaluator exception with owner + expiry |

## Promotion: advisory → required

A lane is a required candidate after **10 consecutive green runs** with **p90
duration under the lane's budget** (default budget 20 minutes; lane-specific
budgets are recorded here). The maintainer then registers it in the required
check evaluator. No lane becomes required by streak alone — the registration
is a reviewed change.

## Demotion / quarantine: required → quarantined

A required lane with **3 or more `infra/network` or `unknown` failures inside
14 days** is quarantined: an explicit evaluator exception is added with a
named owner and a **14-day expiry**. Quarantine without an owner or expiry is
not valid and fails review.

## Budgets

| Lane | p90 budget |
|---|---|
| default | 20 min |
| (lane-specific budgets are appended here as lanes graduate) | |

## Reporting

The CI health report (`scripts/ci/ci_health_report.py`) samples the last N
runs per workflow and emits pass rate, rerun rate, p50/p90, green streaks,
failure classes (`test`, `infra/network`, `admission`, `cancelled`,
`unknown`), and a failure-signature table. Scheduled failures open or update
one tracked issue per workflow (`ci-scheduled-failure` label) and close it on
the next green run — no silent red.
