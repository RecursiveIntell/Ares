"""Task 2.2 RED: a delegated child's effective lifetime is bounded by its grant window.

The canonical Ares time scope is ``time.not_after`` (ares_runtime/authority.py). A child
given a window must be denied once that window has closed, and must never run past the
earlier of its own timeout and the window end.

Whether a live parent agent should itself carry a window is a separate product decision
and is NOT asserted here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.delegated_expiry import child_effective_deadline, ChildWindowClosed


NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_effective_deadline_is_the_earlier_of_own_timeout_and_window_end():
    window_end = NOW + timedelta(seconds=30)
    deadline = child_effective_deadline(now=NOW, own_timeout_seconds=600, window_not_after=window_end)
    assert deadline == window_end, "a child cannot outlive its grant window"


def test_own_timeout_wins_when_it_is_earlier_than_the_window():
    window_end = NOW + timedelta(hours=1)
    deadline = child_effective_deadline(now=NOW, own_timeout_seconds=60, window_not_after=window_end)
    assert deadline == NOW + timedelta(seconds=60)


def test_no_window_keeps_own_timeout_behavior():
    deadline = child_effective_deadline(now=NOW, own_timeout_seconds=60, window_not_after=None)
    assert deadline == NOW + timedelta(seconds=60)


def test_closed_window_is_a_typed_denial_not_a_silent_start():
    expired = NOW - timedelta(seconds=1)
    with pytest.raises(ChildWindowClosed):
        child_effective_deadline(now=NOW, own_timeout_seconds=60, window_not_after=expired)


def test_naive_datetimes_are_refused_rather_than_compared_silently():
    with pytest.raises(ValueError):
        child_effective_deadline(now=NOW.replace(tzinfo=None), own_timeout_seconds=60, window_not_after=None)
