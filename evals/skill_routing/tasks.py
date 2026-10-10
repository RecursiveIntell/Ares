"""Task battery + programmatic oracles for the skill-routing eval.

Each task stresses one routing decision and grades the *agent's behavior* —
which skills it chose to load — not the wording of any prompt. Oracles are
pure functions over a run's observed skill loads, so there is no LLM judge.

The central claim under test: the task-fit routing prose should NOT change
whether a directly-applicable skill is loaded (those tasks must stay correct),
but SHOULD reduce loads on tasks where nothing is directly applicable. Two
task families encode that:

  * ``relevant``  — a specific skill is directly applicable; loading it is
                    correct and the arm must not regress here.
  * ``abstain``   — no skill is directly applicable; the correct behavior is
                    to load none (the old "err on the side of loading" prose
                    is expected to over-load here).

Grading is fractional so partial credit is visible:
  1.0  exactly the right behavior
  0.5  right skill plus an unnecessary one (over-load)
  0.0  missed the right skill, or loaded something when it should abstain
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fixtures import known_names


@dataclass
class RoutingTask:
    task_id: str
    family: str            # "relevant" | "abstain"
    prompt: str
    # oracle(loaded_names, final_text) -> float in 0.0..1.0
    oracle: Callable[[list[str], str], float]
    notes: str = ""


def _loaded_set(loaded: list[str]) -> set[str]:
    return {n for n in loaded if n in known_names()}


def _relevant(name: str) -> Callable[[list[str], str], float]:
    def _grade(loaded: list[str], _final: str) -> float:
        got = _loaded_set(loaded)
        if name not in got:
            return 0.0
        return 1.0 if len(got) == 1 else 0.5
    return _grade


def _abstain() -> Callable[[list[str], str], float]:
    def _grade(loaded: list[str], _final: str) -> float:
        got = _loaded_set(loaded)
        if not got:
            return 1.0
        # Any load on a no-applicable-skill task is the over-loading failure.
        return 0.0
    return _grade


TASKS: list[RoutingTask] = [
    RoutingTask(
        task_id="t1_deploy",
        family="relevant",
        prompt=(
            "We need to ship the payments service to production tonight behind "
            "the usual gated rollout. Walk me through exactly how we do that deploy."
        ),
        oracle=_relevant("deploy-runbook"),
        notes="Direct hit on devops/deploy-runbook; decoy rollback-runbook shares 'deploy'.",
    ),
    RoutingTask(
        task_id="t2_changelog",
        family="relevant",
        prompt=(
            "Draft the changelog entry for the 2.4.0 release so it matches our "
            "house formatting."
        ),
        oracle=_relevant("changelog-format"),
        notes="Direct hit on writing/changelog-format.",
    ),
    RoutingTask(
        task_id="t3_csv",
        family="relevant",
        prompt=(
            "I have a comma-separated export I need to turn into typed rows in "
            "code. What's our approach for parsing a delimited file?"
        ),
        oracle=_relevant("csv-parser"),
        notes="Direct hit on data/csv-parser.",
    ),
    RoutingTask(
        task_id="t4_abstain_math",
        family="abstain",
        prompt="What is 17 * 23? Reply with just the number.",
        oracle=_abstain(),
        notes="Trivial arithmetic — no skill is applicable; loading any is over-load.",
    ),
    RoutingTask(
        task_id="t5_abstain_smalltalk",
        family="abstain",
        prompt="Thanks, that worked great. Have a good one!",
        oracle=_abstain(),
        notes="Pure social close — no skill; loading any is over-load.",
    ),
    RoutingTask(
        task_id="t6_abstain_unrelated",
        family="abstain",
        prompt="Help me brainstorm a birthday gift for a friend who likes hiking.",
        oracle=_abstain(),
        notes="Off-domain creative ask; no skill; loading any is over-load.",
    ),
    # Partial-relevance tasks: the wording brushes a skill's domain, but the
    # task needs none of that skill's instructions. These discriminate the
    # actual hypothesis — the old "even partially relevant → MUST load" prose
    # over-loads here; the task-fit prose should not.
    RoutingTask(
        task_id="t7_partial_changelog",
        family="abstain",
        prompt=(
            "Do users actually read release changelogs? I'm unsure it's worth "
            "the effort to keep writing them every release."
        ),
        oracle=_abstain(),
        notes="Mentions changelogs but asks for an opinion, not the format skill.",
    ),
    RoutingTask(
        task_id="t8_partial_deploy",
        family="abstain",
        prompt=(
            "Our team keeps debating scheduled deploy windows versus continuous "
            "deployment. What's your take on the tradeoff?"
        ),
        oracle=_abstain(),
        notes="Deploy vocabulary, but a discussion question — no runbook needed.",
    ),
]

TASKS_BY_ID = {t.task_id: t for t in TASKS}

# A system prompt prefix shared by both arms — the eval varies ONLY the
# skills block (routing prose + index), never this preamble.
SYSTEM_PREAMBLE = (
    "You are a coding assistant working in a project. You have a skill library "
    "listed below and a `skill_view` tool to load a skill's full instructions. "
    "Load skills when the guidance applies to the task, then answer the user."
)


def _self_test() -> int:
    """Both polarities: oracles must accept correct behavior and reject failures."""
    # relevant: correct single load passes; over-load halves; miss zeroes.
    g = _relevant("deploy-runbook")
    assert g(["deploy-runbook"], "") == 1.0
    assert g(["deploy-runbook", "csv-parser"], "") == 0.5
    assert g(["csv-parser"], "") == 0.0
    assert g([], "") == 0.0
    # abstain: no load passes; any load fails.
    a = _abstain()
    assert a([], "") == 1.0
    assert a(["hue-scenes"], "") == 0.0
    # Unknown names never count as a real load, so a hallucinated tool call
    # cannot masquerade as a correct load.
    assert a(["not-a-real-skill"], "") == 1.0
    # Every task id is unique and every oracle is callable.
    assert len(TASKS_BY_ID) == len(TASKS)
    for t in TASKS:
        assert 0.0 <= t.oracle([], "") <= 1.0
    print(f"self-test: OK ({len(TASKS)} tasks, oracles discriminate both polarities)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
