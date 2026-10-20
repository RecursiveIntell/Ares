# Skill-Routing Eval

An A/B harness measuring whether the **task-fit routing prose** in
`agent/prompt_builder.py` changes an agent's *skill-loading behavior* — not
whether the wording is present. Motivated by the 2026-10-09 task-fit fix
(`cb9e29bd`, `be3df4e5`), whose only prior proof was that the prompt text had
changed. This harness tests the behavior the text is supposed to cause.

## What it measures

Each run drives a minimal agent loop against a small, fixed skill catalog. The
**only variable between arms** is the `<available_skills>` block produced by
`agent/prompt_builder.py` extracted from two git refs — exactly the
`evals/session_search_schema` pattern, applied to the skills block instead of a
tool schema. The model, temperature, task prompts, catalog, and the
`skill_view` tool are held constant across arms.

Grading is programmatic (no LLM judge), fractional, and reads *which skills the
agent chose to load*:

| family | task shape | correct behavior |
|---|---|---|
| `relevant` | one skill is directly applicable | load exactly that skill |
| `abstain` | no skill is directly applicable (incl. partial-relevance) | load none |

| task | family | stress |
|---|---|---|
| `t1_deploy` | relevant | direct hit; a decoy (`rollback-runbook`) shares "deploy" |
| `t2_changelog` | relevant | direct hit on `writing/changelog-format` |
| `t3_csv` | relevant | direct hit on `data/csv-parser` |
| `t4_abstain_math` | abstain | trivial arithmetic — nothing applies |
| `t5_abstain_smalltalk` | abstain | social close — nothing applies |
| `t6_abstain_unrelated` | abstain | off-domain creative ask |
| `t7_partial_changelog` | abstain | brushes "changelog" but asks for an opinion |
| `t8_partial_deploy` | abstain | "deploy" vocabulary, but a discussion question |

Score per run: `1.0` correct · `0.5` right skill **plus** an unnecessary one
(over-load) · `0.0` missed the right skill, or loaded anything on an abstain.

Metrics per run recorded in the JSONL: `score`, `loaded`, `n_loads`,
`first_prompt_tokens`, `total_tokens`, `wall_s`.

## Running

```bash
# Baseline (pre-fix prose) vs the task-fit candidate.
# The base ref predates the fix; the cand ref is the fixed HEAD.
python3 evals/skill_routing/runner.py \
    --base e3e8a39d9427 --cand be3df4e52121 \
    --base-url http://127.0.0.1:11436/v1 \
    --model deepseek-v4.1-flash:cloud --reps 3 --label task-fit-compare

# Any OpenAI-compatible endpoint works. OpenRouter:
#   --base-url https://openrouter.ai/api/v1 --model <model>   (needs OPENROUTER_API_KEY)
# Local Ollama:
#   --base-url http://127.0.0.1:11434/v1 --model granite4.2:3b

# One task, or summarize:
python3 evals/skill_routing/runner.py ... --tasks t7_partial_changelog
python3 evals/skill_routing/report.py --labels task-fit-compare
```

Results append to `results/<label>/<model-slug>.jsonl`; completed
`(task, arm, rep)` cells are skipped on re-run, so an interrupted run resumes.

Rules of engagement:

- **3 reps minimum.** Single-run deltas within ±3% are noise.
- **Two model families.** The effect is expected to be model-dependent; one
  model is not evidence of a general effect.
- The runner imports the **live** `agent/prompt_builder.py` from the working
  tree via `git show`; do not edit that file while a run is in flight.

## Self-tests

```bash
python3 evals/skill_routing/tasks.py            # oracle both-polarity check
python3 evals/skill_routing/prompt_size_probe.py --self-test
```

## Context-cost probe

`prompt_size_probe.py` records how much of the system prompt the skills index
occupies, via the canonical `hermes prompt-size` accounting:

```bash
python3 evals/skill_routing/prompt_size_probe.py --label baseline --json
```

## Results

See [`RESULTS.md`](RESULTS.md) for the measured arm-by-arm numbers, findings,
and caveats. Raw per-run output under `results/` is gitignored (generated); the
findings live in `RESULTS.md`.

This harness measures **routing behavior only**. It is not a quality, safety,
or population-level claim; the corpus is 8 tasks and the finding is scoped to
the models and reps recorded.
