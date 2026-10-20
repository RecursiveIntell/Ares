# Skill-Routing Eval — Results

**Run date:** 2026-10-10 · **Arms:** `pre-fix=e3e8a39d9427` (the earlier
"err on the side of loading" prose), `task-fit=be3df4e52121` (the current
task-fit prose) · **Reps:** 3 · **Tasks:** 8 · **Runs:** 144 (3 arms × 8 tasks ×
3 reps × 2 models)

Models: `deepseek-v4.1-flash:cloud` and `glm-5.3-flash:cloud`, both served
locally through an OpenAI-compatible proxy at `127.0.0.1:11436/v1`.

## Combined (both models)

| arm | family | n | score | mean loads |
|---|---|---|---|---|
| pre-fix | relevant | 18 | 0.917 | 1.278 |
| pre-fix | abstain | 30 | 0.667 | 0.467 |
| **task-fit** | **relevant** | 18 | **0.972** | **1.056** |
| **task-fit** | **abstain** | 30 | **1.000** | **0.000** |
| names-only | relevant | 18 | 0.861 | 1.278 |
| names-only | abstain | 30 | 0.833 | 0.167 |

`names-only` = the task-fit prose with **every** category rendered names-only
(descriptions stripped) — the strongest form of the index-trimming idea this
harness was built to test.

## Per model

| model | arm | relevant score / loads | abstain score / loads |
|---|---|---|---|
| deepseek-v4.1-flash | pre-fix | 0.889 / 1.444 | 0.600 / 0.600 |
| deepseek-v4.1-flash | **task-fit** | **1.000 / 1.000** | **1.000 / 0.000** |
| deepseek-v4.1-flash | names-only | 0.889 / 1.222 | 0.800 / 0.200 |
| glm-5.3-flash | pre-fix | 0.944 / 1.111 | 0.733 / 0.333 |
| glm-5.3-flash | **task-fit** | 0.944 / 1.111 | **1.000 / 0.000** |
| glm-5.3-flash | names-only | 0.833 / 1.333 | 0.867 / 0.133 |

## Findings

**F1 — The task-fit prose reduces skill over-loading on the two recorded models.**
On the partial-relevance abstain tasks — where the wording brushes a skill's
domain but the task needs none of its instructions — the task-fit arm abstained
3/3 on both models (`t7`), while the pre-fix arm loaded `changelog-format` 3/3 on
both (`t7`) and, on `t8`, loaded both `deploy-runbook` and the `rollback-runbook`
decoy (3/3 on deepseek, 1/3 on glm). Combined abstain loads: **0.467 → 0.000**.
On the directly-applicable tasks there is no regression: the improvement is
observed on one of the two models (deepseek 0.889 → 1.000); glm is unchanged at
0.944. This is the behavior the prose is written to cause, now measured.

**F2 — Stripping descriptions from the index lowers routing quality (names-only
arm).** The names-only arm scores below the task-fit arm on both families and
below the pre-fix baseline on the relevant family (0.861 vs 0.917 combined; glm
relevant 0.833 with loads 1.333, re-grabbing the `rollback-runbook` decoy on
`t1`). Removing the description text — leaving only a bare skill name — makes the
model load on partial keyword matches again (`t7`: names-only loaded
`changelog-format` 3/3 on deepseek). The description text is load-bearing for the
applicability decision, which matches the rationale already recorded in
`agent/coding_context.py` for not demoting categories under `auto`.

On the relevant family, deepseek's names-only score equals its pre-fix score
(0.889) rather than degrading; the degradation is concentrated on glm and on the
abstain family. Measured with the task-fit prose and maximal stripping; the
"bare names vs. interaction between prose and names" effects are not separated.

**Decision: do not extend index demotion.** Keep full descriptions in the
default index. The existing opt-in `focus` denylist is untouched and its narrow
scope is not what this experiment measured (this demoted *all* categories; the
`focus` denylist drops only clearly-off-domain ones).

## Caveats

- 8 tasks, 2 models, 3 reps (n=3 per cell, no significance test). This supports
  a routing-behavior observation for the recorded models only; it is not a
  population-level or quality claim.
- The names-only arm tests maximal description-stripping, not the narrow
  `focus` denylist. It refutes *broad* demotion, not the shipped opt-in mode.
- Both models were served by a local proxy; provider-side model drift over time
  is not controlled.

## Reproduce

```bash
cd <repo>
P=".venv/bin/python evals/skill_routing"
$P/runner.py --base e3e8a39d9427 --cand be3df4e52121 \
    --base-url http://127.0.0.1:11436/v1 \
    --model deepseek-v4.1-flash:cloud --reps 3 --label task-fit-compare --demote-extra-arm
$P/report.py --labels task-fit-compare
```
