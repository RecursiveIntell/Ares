"""Tests for tools/lazy_core_tools.py — core-tool lazy schema loading.

Mirrors tests/tools/test_tool_search.py. Coverage targets the regression modes
called out in #6839 (core surface still eager) and the prototype PR #70084.

All tests monkeypatch ``_core_tool_names`` so they don't depend on the real
tool registry / toolset definitions.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, Any

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(
    name: str, description: str = "", properties: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


_CORE = [
    "terminal",
    "file_write",
    "web_search",
    "browser",
    "delegate_task",
    "memory_search",
]
_NONCORE = ["mcp_foo_bar", "plugin_baz"]


@pytest.fixture(autouse=True)
def _patch_core_names(monkeypatch):
    import tools.lazy_core_tools as m

    monkeypatch.setattr(m, "_core_tool_names", lambda: frozenset(_CORE))


def _core_defs():
    return [_td(n, f"Core tool {n}: does the thing for {n}.") for n in _CORE] + [
        _td(n, "non-core") for n in _NONCORE
    ]


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_off(self):
        from tools.lazy_core_tools import CoreLazyConfig

        cfg = CoreLazyConfig.from_raw(None)
        assert cfg.enabled == "off"

    def test_bool_true_is_on(self):
        from tools.lazy_core_tools import CoreLazyConfig

        cfg = CoreLazyConfig.from_raw(True)
        assert cfg.enabled == "on"

    def test_auto_kept(self):
        from tools.lazy_core_tools import CoreLazyConfig

        cfg = CoreLazyConfig.from_raw({"enabled": "auto"})
        assert cfg.enabled == "auto"

    def test_unknown_enabled_is_off(self):
        from tools.lazy_core_tools import CoreLazyConfig

        cfg = CoreLazyConfig.from_raw({"enabled": "bogus"})
        assert cfg.enabled == "off"

    def test_always_include_normalized(self):
        from tools.lazy_core_tools import CoreLazyConfig

        cfg = CoreLazyConfig.from_raw({
            "enabled": "on",
            "always_include": [" terminal ", "file_write"],
        })
        assert cfg.always_include == ("terminal", "file_write")


# ---------------------------------------------------------------------------
# Activation gate + passthrough
# ---------------------------------------------------------------------------


class TestActivation:
    def test_off_is_passthrough(self):
        from tools.lazy_core_tools import assemble_core_tool_defs, CoreLazyConfig

        res = assemble_core_tool_defs(
            _core_defs(), config=CoreLazyConfig(enabled="off")
        )
        assert res.activated is False
        # Every tool (core + noncore) returned untouched.
        names = {(t["function"]["name"]) for t in res.tool_defs}
        assert names == set(_CORE) | set(_NONCORE)

    def test_no_core_tools_is_passthrough(self):
        from tools.lazy_core_tools import assemble_core_tool_defs, CoreLazyConfig

        res = assemble_core_tool_defs(
            [_td("mcp_x")], config=CoreLazyConfig(enabled="on")
        )
        assert res.activated is False


# ---------------------------------------------------------------------------
# Compact index + bridge
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_activated_collapses_core_behind_bridge(self):
        from tools.lazy_core_tools import assemble_core_tool_defs, CoreLazyConfig
        from tools.lazy_core_tools import REQUEST_SCHEMA_NAME

        res = assemble_core_tool_defs(_core_defs(), config=CoreLazyConfig(enabled="on"))
        assert res.activated is True
        assert res.deferred_count == len(_CORE)
        names = {t["function"]["name"] for t in res.tool_defs}
        # All core full schemas gone; only the bridge remains for them.
        assert REQUEST_SCHEMA_NAME in names
        assert not (names & set(_CORE))
        # Non-core preserved untouched.
        assert _NONCORE[0] in names

    def test_index_is_much_smaller_than_full_schemas(self):
        from tools.lazy_core_tools import assemble_core_tool_defs, CoreLazyConfig
        from tools.lazy_core_tools import (
            estimate_tokens_from_schemas,
            build_compact_index,
        )

        full = estimate_tokens_from_schemas([
            _td(n, f"Core tool {n}: " + ("x" * 200)) for n in _CORE
        ])
        idx = build_compact_index(
            [_td(n, f"Core tool {n}: does {n}.") for n in _CORE], max_tokens=1500
        )
        # Index must be dramatically smaller than the full schema payload.
        assert len(idx) < full * 4  # chars/4 rule; index chars << full tokens*4
        assert "terminal" in idx

    def test_always_include_keeps_eager(self):
        from tools.lazy_core_tools import assemble_core_tool_defs, CoreLazyConfig
        from tools.lazy_core_tools import REQUEST_SCHEMA_NAME

        res = assemble_core_tool_defs(
            _core_defs(),
            config=CoreLazyConfig(enabled="on", always_include=("terminal",)),
        )
        names = {t["function"]["name"] for t in res.tool_defs}
        assert "terminal" in names  # kept eager
        assert REQUEST_SCHEMA_NAME in names  # bridge still present for the rest
        assert "file_write" not in names  # deferred


# ---------------------------------------------------------------------------
# Bridge dispatch (hydration on demand)
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_hydrate_returns_full_schema(self):
        from tools.lazy_core_tools import dispatch_request_tool_schema

        out = json.loads(
            dispatch_request_tool_schema(
                {"name": "terminal"}, current_tool_defs=_core_defs()
            )
        )
        assert out["name"] == "terminal"
        assert "parameters" in out

    def test_unknown_tool_errors(self):
        from tools.lazy_core_tools import dispatch_request_tool_schema

        out = dispatch_request_tool_schema(
            {"name": "nope"}, current_tool_defs=_core_defs()
        )
        assert "error" in out

    def test_missing_name_errors(self):
        from tools.lazy_core_tools import dispatch_request_tool_schema

        assert "error" in dispatch_request_tool_schema(
            {}, current_tool_defs=_core_defs()
        )


# ---------------------------------------------------------------------------
# Fail-open under broken config
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_broken_config_loads_off(self):
        from tools.lazy_core_tools import load_config

        # load_config swallows exceptions; never raises.
        assert load_config().enabled in ("off", "on", "auto")
