#!/usr/bin/env python3
"""Deterministic PR35 reconciliation repairs applied after merging pinned Ares main.

This file is temporary CI scaffolding. The reconciliation workflow removes it
in the same durable merge commit once all focused witnesses pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PINNED_MAIN = "0451a66cbb765a3ede359660e37b1b2fbe857525"


def between(text: str, start: str, end: str) -> str:
    if text.count(start) != 1:
        raise SystemExit(f"unexpected ownership start anchor: {start!r}")
    a = text.index(start)
    try:
        b = text.index(end, a + len(start))
    except ValueError as exc:
        raise SystemExit(
            f"missing ownership end anchor after {start!r}: {end!r}"
        ) from exc
    return text[a:b]


def replace_between(text: str, source: str, start: str, end: str) -> str:
    return text.replace(between(text, start, end), between(source, start, end), 1)


def reconcile_hermes_state() -> None:
    path = Path("hermes_state.py")
    text = path.read_text(encoding="utf-8")
    sanitize_import = "from agent.memory_manager import sanitize_context\n"
    activity_anchor = "from agent.message_sanitization import _sanitize_surrogates\n"
    activity_owner = "from agent.session_activity import ActivityProvenance\n"
    activity_expected = activity_anchor + activity_owner
    marker_import = (
        "from agent.context_compressor import (\n"
        "    _DB_PERSISTED_MARKER as _DB_PERSISTED_MARKER_KEY,\n"
        ")\n"
    )
    common_import = (
        "from hermes_state_common import escape_like as _escape_like, "
        "stat_db_file_identity as _stat_db_file_identity\n"
    )
    common_expected = (
        "from hermes_state_common import (\n"
        "    _RECOVERABLE_END_REASONS,\n"
        "    escape_like as _escape_like,\n"
        "    stat_db_file_identity as _stat_db_file_identity,\n"
        ")\n"
    )

    if sanitize_import not in text:
        if text.count(activity_anchor) != 1:
            raise SystemExit("unexpected message-sanitization import shape")
        text = text.replace(activity_anchor, sanitize_import + activity_anchor, 1)
    if activity_expected not in text:
        if text.count(activity_anchor) != 1:
            raise SystemExit("unexpected ActivityProvenance import shape")
        text = text.replace(activity_anchor, activity_expected, 1)
    if marker_import not in text:
        if text.count(activity_expected) != 1:
            raise SystemExit("unexpected top-level activity import block")
        text = text.replace(activity_expected, activity_expected + marker_import, 1)
    if common_expected not in text:
        if text.count(common_import) != 1:
            raise SystemExit("unexpected hermes_state_common import shape")
        text = text.replace(common_import, common_expected, 1)

    path.write_text(text, encoding="utf-8")
    checked = path.read_text(encoding="utf-8")
    for owner in (sanitize_import, activity_expected, marker_import, common_expected):
        if owner not in checked:
            raise SystemExit(f"canonical hermes_state owner missing: {owner!r}")


def reconcile_goals() -> None:
    path = Path("hermes_cli/goals.py")
    merged = path.read_text(encoding="utf-8")
    branch_shape = merged
    main = subprocess.check_output(
        ["git", "show", f"{PINNED_MAIN}:hermes_cli/goals.py"], text=True
    )

    set_start = (
        "    def set(self, goal: str, *, max_turns: Optional[int] = None, "
        "contract: Optional[GoalContract] = None) -> GoalState:\n"
    )
    set_end = "    def add_collaboration_contract_ref(self, contract_ref: str) -> GoalState:\n"
    merged = replace_between(merged, main, set_start, set_end)

    subgoal_start = "    # --- /subgoal user controls ---------------------------------------\n"
    wait_start = "    # --- /goal wait barrier -------------------------------------------\n"
    merged = replace_between(merged, main, subgoal_start, wait_start)

    checkpoint_start = (
        "    def _checkpoint(self, outcome: str, reason: str, "
        "next_action: Optional[str], *, continuation: bool, "
        "metadata: Optional[Dict[str, Any]] = None) -> bool:\n"
    )
    branch_is_waiting = between(
        branch_shape, "    def is_waiting(self) -> bool:\n", checkpoint_start
    )
    main_wait = between(main, wait_start, checkpoint_start)
    is_waiting_start = "    def is_waiting(self) -> bool:\n"
    if main_wait.count(is_waiting_start) != 1:
        raise SystemExit("unexpected main is_waiting owner")
    main_is_waiting = main_wait[main_wait.index(is_waiting_start) :]
    wait = main_wait.replace(main_is_waiting, branch_is_waiting, 1)
    wait = wait.replace(
        "    def wait_for_seconds(self, seconds: int, reason: str = \"\") -> GoalState:\n",
        "    def wait_for_seconds(self, seconds: int, reason: str = \"\", *, on_delegations: int = 0) -> GoalState:\n",
        1,
    )
    wait = wait.replace(
        "        self._state.waiting_until = time.time() + seconds\n"
        "        self._state.waiting_reason = (reason or \"\").strip() or None\n",
        "        self._state.waiting_until = time.time() + seconds\n"
        "        self._state.waiting_on_delegations = max(0, int(on_delegations))\n"
        "        self._state.waiting_reason = (reason or \"\").strip() or None\n",
        1,
    )
    wait = wait.replace(
        "        self._state.waiting_until = 0.0\n"
        "        self._state.waiting_reason = (reason or \"\").strip() or None\n",
        "        self._state.waiting_until = 0.0\n"
        "        self._state.waiting_on_delegations = 0\n"
        "        self._state.waiting_reason = (reason or \"\").strip() or None\n",
        2,
    )
    wait = wait.replace(
        "        self._state.waiting_until = 0.0\n"
        "        self._state.waiting_reason = None\n",
        "        self._state.waiting_until = 0.0\n"
        "        self._state.waiting_on_delegations = 0\n"
        "        self._state.waiting_reason = None\n",
        1,
    )
    merged = merged.replace(between(merged, wait_start, checkpoint_start), wait, 1)

    eval_start = "    def evaluate_after_turn(\n"
    eval_doc = "        \"\"\"Run gates + judge and update state."
    eval_header = between(merged, eval_start, eval_doc)
    if "active_delegations:" not in eval_header:
        needle = "        background_processes: Optional[List[Dict[str, Any]]] = None,\n"
        if eval_header.count(needle) != 1:
            raise SystemExit("unexpected evaluate_after_turn signature")
        merged = merged.replace(
            eval_header,
            eval_header.replace(
                needle, needle + "        active_delegations: int = 0,\n", 1
            ),
            1,
        )

    budget_start = (
        "    def _budget_pause(self, state: GoalState, verdict: str, reason: str, "
        "note: str = \"\") -> Dict[str, Any]:\n"
    )
    budget_old = between(merged, budget_start, eval_start)
    budget_new = (
        "    def _budget_pause(self, state: GoalState, verdict: str, reason: str, note: str = \"\") -> Dict[str, Any]:\n"
        "        state.status = \"paused\"\n"
        "        state.outcome = TURN_BUDGET_EXHAUSTED\n"
        "        state.paused_reason = f\"turn budget exhausted ({state.turns_used}/{state.max_turns})\"\n"
        "        if not self._checkpoint(\n"
        "            TURN_BUDGET_EXHAUSTED,\n"
        "            state.paused_reason,\n"
        "            \"explicit /goal resume after inspecting cumulative budget\",\n"
        "            continuation=False,\n"
        "        ):\n"
        "            return self._persistence_failure(\"turn budget stop could not be saved\")\n"
        "        return _decision(\n"
        "            \"paused\", False, None, verdict, reason,\n"
        "            f\"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns used{note}. \"\n"
        "            \"Use /goal resume to keep going, or /goal clear to stop.\",\n"
        "        )\n\n"
    )
    merged = merged.replace(budget_old, budget_new, 1)

    # Restrict BLOCKED replacement to GoalManager.evaluate_after_turn so the
    # kanban worker loop's separate blocked semantics are never touched.
    goal_class_end = "\n\n# ── Kanban worker goal loop"
    eval_block = between(merged, eval_start, goal_class_end)
    blocked_if = "        if verdict == \"blocked\":\n"
    done_if = "        if verdict == \"done\":\n"
    blocked_pos = eval_block.find(blocked_if)
    if blocked_pos < 0:
        raise SystemExit("GoalManager evaluator missing blocked verdict branch")
    done_pos = eval_block.find(done_if, blocked_pos + len(blocked_if))
    if done_pos < 0:
        raise SystemExit("GoalManager evaluator missing done verdict after blocked branch")
    # Include comments immediately preceding the blocked branch when present,
    # but never search outside the evaluator block.
    comment_marker = "        # BLOCKED is NOT done:"
    comment_pos = eval_block.rfind(comment_marker, 0, blocked_pos)
    replace_pos = comment_pos if comment_pos >= 0 else blocked_pos
    blocked_old = eval_block[replace_pos:done_pos]
    blocked_new = (
        "        # BLOCKED is not completion. Use the canonical lifecycle stop owner\n"
        "        # so pause, checkpoint, outcome, and persistence remain one transition.\n"
        "        if verdict == \"blocked\":\n"
        "            return self._execution_stop(GOAL_BLOCKED, f\"judged unachievable: {reason}\", metadata=turn_metadata)\n\n"
    )
    eval_block = eval_block[:replace_pos] + blocked_new + eval_block[done_pos:]
    merged = merged.replace(between(merged, eval_start, goal_class_end), eval_block, 1)

    failure_start = (
        "        # Auto-pause when the judge cannot reach the API at all N turns in a\n"
    )
    budget_check = "        if state.turns_used >= state.max_turns:\n"
    merged = merged.replace(
        between(merged, failure_start, budget_check),
        between(main, failure_start, budget_check),
        1,
    )

    path.write_text(merged, encoding="utf-8")
    checked = path.read_text(encoding="utf-8")
    goal_class = between(checked, "class GoalManager:\n", goal_class_end)
    for stale in (
        "self._save()",
        "self._require_goal()",
        "self._require_active()",
        "self._pause_decision(",
    ):
        if stale in goal_class:
            raise SystemExit(f"stale GoalManager helper survived: {stale}")
    eval_header = between(checked, eval_start, eval_doc)
    if "active_delegations: int = 0" not in eval_header:
        raise SystemExit("delegation-aware evaluator signature missing")
    if "waiting_on_delegations = max(0, int(on_delegations))" not in goal_class:
        raise SystemExit("delegation-aware timed wait missing")
    if "live = count_active_delegations(self.session_id)" not in goal_class:
        raise SystemExit("delegation early wake missing")
    if "malformed judge output threshold reached" not in goal_class:
        raise SystemExit("parse-failure pause owner missing")


def reconcile_goal_test() -> None:
    path = Path("tests/hermes_cli/test_goals.py")
    text = path.read_text(encoding="utf-8")
    old = "            mgr._save()\n"
    new = "            assert goals.save_goal(mgr.session_id, mgr.state)\n"
    if text.count(old) != 1:
        raise SystemExit("unexpected stale _save test shape")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    reconcile_hermes_state()
    reconcile_goals()
    reconcile_goal_test()


if __name__ == "__main__":
    main()
