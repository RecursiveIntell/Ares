"""A registry-wiring fixture must not execute its real browser fallback."""
import importlib.util
from pathlib import Path

import pytest

from tools.registry import registry

_spec = importlib.util.spec_from_file_location(
    'browser_wiring_fixture_support',
    Path(__file__).with_name('test_browser_extension_router_wiring.py'),
)
_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_support)
_route_spy = _support._route_spy


def test_browser_wiring_fixture_never_enters_browser_runtime(_route_spy, monkeypatch):
    from tools import browser_tool

    runtime_calls = []

    def forbidden_runtime(*args, **kwargs):
        runtime_calls.append((args, kwargs))
        pytest.fail('registry-wiring fixture crossed the real browser command boundary')

    monkeypatch.setattr(browser_tool, '_run_browser_command', forbidden_runtime)
    for name in _support.BROWSER_ACTIONS:
        _route_spy.clear()
        args = {'url': 'https://example.test', 'ref': '@e1', 'text': 'hi'}
        result = registry.get_entry(name).handler(
            dict(args), task_id='task-fixture', session_id='session-fixture')
        assert result is not None
        assert len(_route_spy) == 1
        assert _route_spy[0]['action'] == name
        assert _route_spy[0]['args'] == args
        assert _route_spy[0]['task_id'] == 'task-fixture'
        assert _route_spy[0]['session_id'] == 'session-fixture'
    assert runtime_calls == []
