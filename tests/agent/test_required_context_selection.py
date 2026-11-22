"""Optional versus required ContextEngine.select_context() behavior.

The first contract is fail-open for existing users. Required mode denies the
logical provider request before dispatch rather than silently selecting the
original transcript. No real providers or credentials are involved.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.conversation_loop import _apply_context_engine_selection


REQUEST = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
HISTORY = [{"role": "user", "content": "hello"}]


def _agent(engine, *, required=False):
    return SimpleNamespace(
        context_compressor=engine,
        _context_selection_required=required,
        session_id="synthetic-session",
    )


def _select(engine, *, required=False):
    return _apply_context_engine_selection(
        _agent(engine, required=required), REQUEST, HISTORY, HISTORY[-1],
        logger=Mock(),
    )


class _NoSelection:
    pass


class _Selected:
    context_length = 4096

    def select_context(self, request_messages, **kwargs):
        return [{"role": "user", "content": "governed"}]


class _NoResult:
    def select_context(self, request_messages, **kwargs):
        return None


class _Broken:
    def select_context(self, request_messages, **kwargs):
        raise OSError("owner unavailable")


class _Invalid:
    def select_context(self, request_messages, **kwargs):
        return []


def test_optional_engine_without_selection_is_original():
    assert _select(_NoSelection()) is REQUEST


def test_required_engine_without_selection_refuses():
    with pytest.raises(RuntimeError, match="hook is unavailable"):
        _select(_NoSelection(), required=True)


def test_required_engine_selects_provider_projection():
    assert _select(_Selected(), required=True) == [
        {"role": "user", "content": "governed"}
    ]


@pytest.mark.parametrize("engine, message", [
    (_NoResult(), "returned no provider context"),
    (_Broken(), "selection failed"),
    (_Invalid(), "invalid provider context"),
])
def test_required_selection_never_silently_falls_back(engine, message):
    with pytest.raises(RuntimeError, match=message):
        _select(engine, required=True)


@pytest.mark.parametrize("engine", [_NoResult(), _Broken(), _Invalid()])
def test_legacy_optional_selection_keeps_original_request(engine):
    assert _select(engine) is REQUEST
