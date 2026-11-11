"""Regression (audit L5): a failed reconciliation marker must not be silently discarded.

_mark_reconciliation_required previously swallowed every exception. When the
durable marker write failed, nothing recorded that the rebase needed
reconciliation beyond the in-memory return value.
"""

import pytest

from ares_runtime.continuity import runtime


class _FailingDb:
    def mark_context_rebase_reconciliation_required(self, transition_id):
        raise RuntimeError("disk full")


class _OkDb:
    def __init__(self):
        self.marked = []

    def mark_context_rebase_reconciliation_required(self, transition_id):
        self.marked.append(transition_id)


def test_marker_success_is_recorded():
    db = _OkDb()
    runtime._mark_reconciliation_required(db, "t-1")
    assert db.marked == ["t-1"]


def test_marker_failure_is_reported_not_swallowed():
    with pytest.raises(runtime.ReconciliationMarkerError) as info:
        runtime._mark_reconciliation_required(_FailingDb(), "t-2")
    assert "t-2" in str(info.value)
    assert isinstance(info.value.__cause__, RuntimeError)


def test_missing_marker_api_is_a_noop():
    # A db without the marker API cannot record it; that is not an error.
    runtime._mark_reconciliation_required(object(), "t-3")
