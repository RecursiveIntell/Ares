"""Task 2.2 behavior: a grant window bounds a real delegated child run.

The child's work is ``child.run_conversation``. The fake child here blocks forever, so
only the window deadline can end the run. These tests drive ``_run_single_child`` itself,
so they check the wiring as well as the rule in ``agent.delegated_expiry``.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool as d


class _BlockingChild:
    """Child whose run never returns on its own; only an external stop can end it."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self._interrupt_requested = False
        self.model = "m"
        self.provider = "p"
        self._delegate_depth = 1
        self.session_id = "child"
        self.iteration_budget = None

    def run_conversation(self, *a, **k):
        self.started.set()
        self.release.wait(timeout=20)  # hard ceiling so a broken test cannot hang the suite
        return {"final_response": "late", "completed": True, "api_calls": 1}

    def interrupt(self, *a, **k):
        self.release.set()

    def get_activity_summary(self):
        return {}


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.iteration_budget = None
    return parent


def test_window_end_bounds_a_blocking_child_run(monkeypatch):
    """A window ending in ~0.5s must end the child well before its 20s ceiling."""
    monkeypatch.setattr(d, "_get_child_timeout", lambda: None)
    child = _BlockingChild()
    # Window long enough for setup and start (setup is not the behavior under test), but
    # far shorter than the 20s ceiling, so only the window can end the blocking run.
    window_end = datetime.now(timezone.utc) + timedelta(seconds=3)

    t0 = time.monotonic()
    result = d._run_single_child(0, "goal", child, _parent(), window_not_after=window_end)
    elapsed = time.monotonic() - t0

    assert child.started.is_set(), "the child must have started inside the window"
    assert elapsed < 10, f"window did not bound the child: ran {elapsed:.1f}s"
    assert result.get("status") in {"timeout", "error", "failed", "interrupted"}, result


def test_closed_window_refuses_child_before_it_starts(monkeypatch):
    """A window already in the past must be refused; the child must never start."""
    monkeypatch.setattr(d, "_get_child_timeout", lambda: None)
    child = _BlockingChild()
    closed = datetime.now(timezone.utc) - timedelta(seconds=5)

    result = d._run_single_child(0, "goal", child, _parent(), window_not_after=closed)

    assert not child.started.is_set(), "a closed window must not let the child run"
    assert result.get("status") not in {"completed"}, result


def test_no_window_leaves_existing_behavior_unchanged(monkeypatch):
    """Without a window the child's own timeout still governs, as before."""
    monkeypatch.setattr(d, "_get_child_timeout", lambda: 0.5)
    child = _BlockingChild()

    t0 = time.monotonic()
    d._run_single_child(0, "goal", child, _parent(), window_not_after=None)
    elapsed = time.monotonic() - t0

    assert elapsed < 10, f"own timeout did not apply without a window: {elapsed:.1f}s"


def test_unbounded_default_falls_back_to_a_bounded_child(monkeypatch):
    """Regression: ``child_timeout_seconds: 0`` (the shipped default) means
    "no operator cap", and top-level delegation supplies no grant window — so
    both bounds were None and agent.delegated_expiry.child_effective_deadline
    refused with ``ValueError: a child needs an own timeout or a grant window
    to be bounded``. Every top-level delegation on a fresh install died in
    ~10 ms. The delegate now derives a 24 h fallback cap when neither bound is
    present, so the child starts and the heartbeat staleness monitor remains
    the real stuck-child protection."""
    monkeypatch.setattr(d, "_get_child_timeout", lambda: None)
    started = threading.Event()
    finished = threading.Event()
    released = threading.Event()

    class _FinishingChild:
        _interrupt_requested = False
        model = "m"
        provider = "p"
        _delegate_depth = 1
        session_id = "child"
        iteration_budget = None

        def run_conversation(self, **kwargs):
            started.set()
            # Return quickly: proves the child runs to completion under the
            # fallback bound rather than being refused or killed early.
            finished.set()
            released.set()
            return {"final_response": "ok", "completed": True, "interrupted": False,
                    "api_calls": 1, "messages": []}

    child = _FinishingChild()
    result = d._run_single_child(0, "goal", child, _parent(), window_not_after=None)

    assert started.is_set(), "fallback bound must let the child start"
    assert finished.is_set(), "child must be able to finish inside the fallback"
    assert result.get("status") == "completed", result
