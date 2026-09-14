"""Canonical validation helpers for session-scoped model options.

The gateway/compute host owns application and persistence; this module only
normalizes the wire request so every transport validates the same shape.
"""

from __future__ import annotations

from typing import Any

_VALID_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def normalize_runtime_configure(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("runtime options must be an object")

    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id required")

    intent_id = str(params.get("intent_id") or "").strip()
    if not intent_id:
        raise ValueError("intent_id required")

    result: dict[str, Any] = {"session_id": session_id, "intent_id": intent_id}

    model = params.get("model")
    if model is not None:
        if not isinstance(model, dict):
            raise ValueError("model must be an object")
        model_id = str(model.get("id") or "").strip()
        provider = str(model.get("provider") or "").strip()
        if not model_id or not provider:
            raise ValueError("model.id and model.provider are required")
        result["model"] = {
            "id": model_id,
            "provider": provider,
            "persist_profile_default": bool(model.get("persist_profile_default", False)),
            "confirm_expensive_model": bool(model.get("confirm_expensive_model", False)),
        }

    reasoning = params.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ValueError("reasoning must be an object")
        mode = str(reasoning.get("mode") or "").strip().lower()
        if mode == "inherit":
            result["reasoning"] = {"mode": mode}
        elif mode == "off":
            result["reasoning"] = {"mode": mode}
        elif mode == "effort":
            effort = str(reasoning.get("effort") or "").strip().lower()
            if effort not in _VALID_EFFORTS:
                raise ValueError(f"unknown reasoning effort: {effort}")
            result["reasoning"] = {"mode": mode, "effort": effort}
        else:
            raise ValueError(f"unknown reasoning mode: {mode}")

    fast = params.get("fast")
    if fast is not None:
        value = str(fast).strip().lower()
        if value not in {"inherit", "normal", "fast"}:
            raise ValueError(f"unknown fast mode: {fast}")
        result["fast"] = value

    if len(result) == 2:
        raise ValueError("at least one runtime option is required")
    return result


def model_switch_value(model: dict[str, Any]) -> str:
    scope = "--global" if model.get("persist_profile_default") else "--session"
    return f"{model['id']} --provider {model['provider']} {scope}"
