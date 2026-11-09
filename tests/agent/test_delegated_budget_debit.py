"""Task 2.2 RED: a delegated child's iterations debit the parent's remaining budget.

Operator-chosen model: "debit from parent". A child's effective spend is bounded by,
and charged against, the parent's budget, so total parent+child iterations cannot
exceed the parent's cap. The child keeps its own per-child ceiling as well.
"""

import pytest

from agent.iteration_budget import IterationBudget
from agent.delegated_budget import DebitingIterationBudget


def test_child_consume_debits_parent_remaining():
    parent = IterationBudget(10)
    child = DebitingIterationBudget(max_total=50, parent=parent)
    assert child.consume() is True
    assert parent.used == 1, "a child iteration must debit the parent"


def test_child_cannot_exceed_parent_remaining_even_with_larger_own_cap():
    parent = IterationBudget(3)
    child = DebitingIterationBudget(max_total=50, parent=parent)
    results = [child.consume() for _ in range(10)]
    assert results.count(True) == 3, "total spend must stop at the parent's remaining budget"
    assert parent.used == 3
    assert parent.remaining == 0


def test_child_respects_its_own_cap_when_parent_has_more():
    parent = IterationBudget(100)
    child = DebitingIterationBudget(max_total=2, parent=parent)
    results = [child.consume() for _ in range(5)]
    assert results.count(True) == 2
    assert parent.used == 2


def test_refund_returns_iteration_to_parent_and_child():
    parent = IterationBudget(5)
    child = DebitingIterationBudget(max_total=5, parent=parent)
    assert child.consume()
    child.refund()
    assert child.used == 0
    assert parent.used == 0, "a refunded child iteration must release the parent debit"


def test_parent_exhaustion_blocks_child_without_partial_child_debit():
    parent = IterationBudget(1)
    parent.consume()  # parent already exhausted
    child = DebitingIterationBudget(max_total=5, parent=parent)
    assert child.consume() is False
    assert child.used == 0, "a denied consume must not leave a child-side debit behind"




def test_build_child_agent_call_site_debits_parent_budget():
    """The child build call site must pass the parent-debiting helper, never None.

    Full construction needs a provider SDK and a configured context engine that this
    sandbox does not provide, so the wiring is asserted structurally: the
    ``iteration_budget`` keyword in ``_build_child_agent`` must be the debiting helper.
    Mutation-checked: reverting it to None fails this test.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "tools" / "delegate_tool.py").read_text())
    build = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_child_agent"
    )
    values = [
        ast.unparse(kw.value)
        for call in ast.walk(build) if isinstance(call, ast.Call)
        for kw in call.keywords if kw.arg == "iteration_budget"
    ]
    assert values, "_build_child_agent must pass iteration_budget to the child"
    assert all(v.startswith("_child_debiting_budget(") for v in values), (
        f"child budget must be parent-debiting, got {values}"
    )
