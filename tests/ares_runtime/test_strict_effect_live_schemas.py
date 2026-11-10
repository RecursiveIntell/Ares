"""Strict effect validation must accept every legitimate call shape that the
*live* model-facing tool schemas declare, not just the hand-curated defaults.

Defect found 2026-11-06 when ``ARES_STRICT_EFFECT_TOOL_ARGS_V1`` was enabled:
``terminal.notify`` is declared ``{"anyOf": [...]}`` with no top-level ``type``,
and ``validate_effect_args`` refused every value for it (``COERCION_REQUIRED``)
because it only read ``spec["type"]``. The curated corpus gate used
``_DEFAULT_EFFECT_SCHEMAS`` and so could not see it.

Exactness is preserved: a union branch must match its type *exactly* (no
coercion); a typeless spec with no union stays fail-closed.
"""

from __future__ import annotations

import pytest

from ares_runtime.collaboration import ContractError, validate_effect_args

SCHEMA = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": {"type": "string"},
        "notify": {"anyOf": [{"type": "boolean"}, {"type": "array", "items": {"type": "string"}}]},
        "timeout": {"type": "integer"},
    },
}


def test_anyof_accepts_each_declared_branch_exactly():
    validate_effect_args({"command": "ls", "notify": True}, SCHEMA)
    validate_effect_args({"command": "ls", "notify": ["done"]}, SCHEMA)


@pytest.mark.parametrize("bad", ["true", 1, 1.5, {"a": 1}, None])
def test_anyof_still_refuses_values_matching_no_branch(bad):
    with pytest.raises(ContractError) as caught:
        validate_effect_args({"command": "ls", "notify": bad}, SCHEMA)
    assert caught.value.code == "COERCION_REQUIRED"


def test_typeless_spec_without_union_stays_fail_closed():
    schema = {"type": "object", "properties": {"x": {"description": "no type"}}}
    with pytest.raises(ContractError) as caught:
        validate_effect_args({"x": 1}, schema)
    assert caught.value.code == "COERCION_REQUIRED"


def test_bool_is_not_an_integer_in_a_union():
    schema = {"type": "object", "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}
    validate_effect_args({"n": 3}, schema)
    validate_effect_args({"n": None}, schema)
    with pytest.raises(ContractError):
        validate_effect_args({"n": True}, schema)


def test_every_live_effectful_schema_property_is_decidable():
    """No property of a live effectful tool may be undecidable (typeless, non-union)."""
    from ares_runtime.collaboration import _EFFECTFUL_TOOLS
    import model_tools

    undecidable = []
    for definition in model_tools.get_tool_definitions(quiet_mode=True) or []:
        function = definition.get("function", {})
        if function.get("name") not in _EFFECTFUL_TOOLS:
            continue
        for key, spec in function.get("parameters", {}).get("properties", {}).items():
            if "type" not in spec and "anyOf" not in spec and "oneOf" not in spec:
                undecidable.append((function["name"], key))
    assert undecidable == []
