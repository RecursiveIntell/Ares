"""Effective lifetime for a delegated child, bounded by its grant window (Task 2.2).

The canonical Ares time scope is ``time.not_after`` (ares_runtime/authority.py). A child
runs until the EARLIER of its own timeout and its window end. A child whose window has
already closed is denied with a typed error and never starts. Naive datetimes are
refused: comparing them to aware instants would be silently wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


class ChildWindowClosed(Exception):
    """The child's grant window has already ended; the child must not start."""


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def child_effective_deadline(
    *,
    now: datetime,
    own_timeout_seconds: Optional[float],
    window_not_after: Optional[datetime],
) -> datetime:
    """Return the instant after which the child must not run.

    ``own_timeout_seconds`` of None means the child has no own timeout; then the window
    end alone bounds it. With neither bound there is no deadline, so the caller must
    supply at least one of the two.
    """
    _require_aware(now, "now")
    if window_not_after is not None:
        _require_aware(window_not_after, "window_not_after")
        if window_not_after <= now:
            raise ChildWindowClosed("child grant window has already closed")

    own_deadline = (
        now + timedelta(seconds=own_timeout_seconds) if own_timeout_seconds is not None else None
    )
    if own_deadline is None and window_not_after is None:
        raise ValueError("a child needs an own timeout or a grant window to be bounded")
    if own_deadline is None:
        return window_not_after  # type: ignore[return-value]
    if window_not_after is None:
        return own_deadline
    return min(own_deadline, window_not_after)


__all__ = ["child_effective_deadline", "ChildWindowClosed"]
