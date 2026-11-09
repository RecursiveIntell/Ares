"""Delegated-child iteration budget that debits its parent (Task 2.2).

Operator-chosen model ("debit from parent"): a child's iterations are charged against
the parent's remaining budget, so total parent+child spend cannot exceed the parent's
cap. The child also keeps its own per-child ceiling. Both must allow an iteration.

Atomicity: the child's own counter is incremented only after the parent accepts. If
the parent refuses, the child records nothing, so a denied consume leaves no partial
debit on either side.
"""

from agent.iteration_budget import IterationBudget


class DebitingIterationBudget(IterationBudget):
    def __init__(self, max_total: int, parent: IterationBudget):
        super().__init__(max_total)
        self._parent = parent

    def consume(self) -> bool:
        # Charge the parent first. Parent refusal means no child-side debit at all.
        if not self._parent.consume():
            return False
        if super().consume():
            return True
        # Child's own cap refused: return the parent debit so the parent is unchanged.
        self._parent.refund()
        return False

    def refund(self) -> None:
        # Only release a parent debit that this child actually holds.
        if self.used > 0:
            self._parent.refund()
        super().refund()


__all__ = ["DebitingIterationBudget"]
