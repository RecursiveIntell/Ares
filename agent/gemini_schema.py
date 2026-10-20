"""Helpers for translating OpenAI-style tool schemas to Gemini's schema subset."""

from __future__ import annotations

import math
from typing import Any, Dict

from tools.schema_sanitizer import _normalize_type_array

# Gemini's ``FunctionDeclaration.parameters`` field accepts the ``Schema``
# object, which is only a subset of OpenAPI 3.0 / JSON Schema.  Strip fields
# outside that subset before sending Hermes tool schemas to Google.
_GEMINI_SCHEMA_ALLOWED_KEYS = {
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "maxItems",
    "minItems",
    "properties",
    "required",
    "minProperties",
    "maxProperties",
    "minLength",
    "maxLength",
    "pattern",
    "example",
    "anyOf",
    "propertyOrdering",
    "default",
    "items",
    "minimum",
    "maximum",
}


_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}
_GEMINI_STRUCTURAL_KEYS = {
    "array": ("items", "minItems", "maxItems"),
    "object": ("properties", "required", "minProperties", "maxProperties", "propertyOrdering"),
}


class GeminiSchemaProjectionError(ValueError):
    """An array-type tool schema cannot be projected without losing constraints."""

    def __init__(self, path: str, reason: str):
        # Only structural keys and ordinal positions enter path; never echo
        # producer-controlled property names, enum values or descriptions.
        super().__init__(f"Unsupported Gemini tool schema at {path[:160]}: {reason}")


def _array_enum_value_matches(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value))
        )
    return False


def _project_gemini_type_array(cleaned: Dict[str, Any], path: str) -> None:
    type_array = cleaned.pop("type")
    if not type_array or any(not isinstance(t, str) or t not in _JSON_TYPES for t in type_array):
        raise GeminiSchemaProjectionError(path, "type array needs recognized type names")
    types = list(dict.fromkeys(type_array))
    non_null = [t for t in types if t != "null"]
    if not non_null:
        raise GeminiSchemaProjectionError(path, "null-only type arrays are not supported")
    if "enum" in cleaned:
        enum = cleaned["enum"]
        if len(non_null) != 1:
            raise GeminiSchemaProjectionError(path, "multi-type array enums require an exact projection")
        if not isinstance(enum, list) or not enum or any(
            not _array_enum_value_matches(value, non_null[0]) for value in enum
        ):
            raise GeminiSchemaProjectionError(path, "enum must contain compatible finite scalar values")
        if non_null[0] != "string":
            try:
                cleaned["enum"] = [
                    str(value).lower() if isinstance(value, bool) else str(value) for value in enum
                ]
            except (ValueError, OverflowError):
                raise GeminiSchemaProjectionError(path, "enum cannot be serialized exactly") from None

    derived: Dict[str, Any] = {}
    _normalize_type_array(types, derived)
    if "anyOf" in derived:
        constraints = {"anyOf": cleaned["anyOf"]} if "anyOf" in cleaned else {}
        structural = {
            key: cleaned.pop(key)
            for keys in _GEMINI_STRUCTURAL_KEYS.values()
            for key in keys if key in cleaned
        }
        cleaned["anyOf"] = [
            sanitize_gemini_schema({
                **branch,
                **constraints,
                **{key: value for key, value in structural.items()
                   if key in _GEMINI_STRUCTURAL_KEYS.get(branch["type"], ())},
            }, _path=f"{path}.anyOf[{index}]")
            for index, branch in enumerate(derived["anyOf"])
        ]
    else:
        cleaned["type"] = derived["type"]
    if derived.get("nullable"):
        cleaned["nullable"] = True


def sanitize_gemini_schema(schema: Any, *, _path: str = "$") -> Dict[str, Any]:
    """Return a Gemini-compatible copy of a tool parameter schema.

    Hermes tool schemas are OpenAI-flavored JSON Schema and may contain keys
    such as ``$schema`` or ``additionalProperties`` that Google's Gemini
    ``Schema`` object rejects.  This helper preserves the documented Gemini
    subset and recursively sanitizes nested ``properties`` / ``items`` /
    ``anyOf`` definitions.
    """

    if not isinstance(schema, dict):
        return {}

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_ALLOWED_KEYS:
            continue
        if key == "properties":
            if not isinstance(value, dict):
                continue
            props: Dict[str, Any] = {}
            for index, (prop_name, prop_schema) in enumerate(value.items()):
                if not isinstance(prop_name, str):
                    continue
                props[prop_name] = sanitize_gemini_schema(prop_schema, _path=f"{_path}.properties[{index}]")
            cleaned[key] = props
            continue
        if key == "items":
            cleaned[key] = sanitize_gemini_schema(value, _path=f"{_path}.items")
            continue
        if key == "anyOf":
            if not isinstance(value, list):
                continue
            cleaned[key] = [
                sanitize_gemini_schema(item, _path=f"{_path}.anyOf[{index}]")
                for index, item in enumerate(value)
                if isinstance(item, dict)
            ]
            continue
        cleaned[key] = value

    if isinstance(cleaned.get("type"), list):
        _project_gemini_type_array(cleaned, _path)

    # Gemini's Schema validator requires every ``enum`` entry to be a string,
    # even when the parent ``type`` is ``integer`` / ``number`` / ``boolean``.
    # Preserve those constraints by stringifying scalar values while keeping
    # the declared type intact; Gemini uses the strings as schema metadata and
    # still emits typed tool arguments at runtime.
    enum_val = cleaned.get("enum")
    type_val = cleaned.get("type")
    if isinstance(enum_val, list) and type_val in {"integer", "number", "boolean"}:
        stringified = []
        for item in enum_val:
            if isinstance(item, str):
                value = item
            elif isinstance(item, bool):
                value = "true" if item else "false"
            elif (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(item)
            ):
                value = str(item)
            else:
                continue
            if value not in stringified:
                stringified.append(value)
        if stringified:
            cleaned["enum"] = stringified
        else:
            cleaned.pop("enum", None)

    # Gemini validates ``required`` strictly against the same node's
    # ``properties`` — GenerateContentRequest fails with HTTP 400
    # "...items.required[0]: property is not defined" when a required name
    # has no matching property in that node.  MCP servers routinely emit
    # this shape (e.g. the GitHub remote MCP's array item schemas carry
    # ``required`` without ``properties``), and one bad tool schema fails
    # the ENTIRE request before any model output.  Filter ``required`` to
    # names that exist in this node's ``properties`` and drop it when
    # nothing valid remains.  The tool handler still validates required
    # fields at execution time, so this only removes what Gemini couldn't
    # accept anyway.  (Port of Kilo-Org/kilocode#11955.)
    required_val = cleaned.get("required")
    if isinstance(required_val, list):
        props_val = cleaned.get("properties")
        prop_names = set(props_val.keys()) if isinstance(props_val, dict) else set()
        valid_required = [
            name for name in required_val
            if isinstance(name, str) and name in prop_names
        ]
        if not valid_required:
            cleaned.pop("required", None)
        elif len(valid_required) != len(required_val):
            cleaned["required"] = valid_required

    return cleaned


def sanitize_gemini_tool_parameters(parameters: Any) -> Dict[str, Any]:
    """Normalize tool parameters to a valid Gemini object schema."""

    cleaned = sanitize_gemini_schema(parameters)
    if not cleaned:
        return {"type": "object", "properties": {}}
    return cleaned
