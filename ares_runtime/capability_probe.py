"""Offline capability checks run by ``ares doctor`` inside the selected runtime.

``MCP readiness`` proves configured servers answer. It cannot see whether the
model can actually *call* a capability: a plugin tool whose schema reaches
the model without parameters, a real-profile browser that refuses to resolve
the desktop's default browser, or a web backend that resolves to nothing all
pass a reachability probe. These checks inspect local state and do not
intentionally make provider requests or launch a browser. Provider availability
methods are extension points that are expected to make no network calls; this
probe does not enforce that contract against nonconforming plugins.

Run as ``python -m ares_runtime.capability_probe``; prints one JSON object
mapping check label to ``{"ok": bool, "detail": str}``.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _tool_schema_check() -> dict[str, Any]:
    import model_tools  # noqa: F401  (registers built-in tools on import)
    from hermes_cli.plugins import discover_plugins
    from tools.registry import registry

    discover_plugins()
    offenders = []
    for entry in registry._snapshot_entries():
        schema = entry.schema if isinstance(entry.schema, dict) else {}
        parameters = schema.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            offenders.append(entry.name)
    if offenders:
        return {
            "ok": False,
            "detail": "model sees no parameters for: " + ", ".join(sorted(offenders)),
        }
    return {"ok": True, "detail": "every registered tool exposes a parameters object"}


def _browser_check() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    browser = (load_config_readonly() or {}).get("browser") or {}
    if not browser.get("use_real_profile"):
        return {"ok": True, "detail": "real-profile browsing off"}
    from hermes_cli.browser_connect import UNSUPPORTED_CHANNEL, detect_default_chromium

    detected = detect_default_chromium()
    if detected is None:
        return {"ok": False, "detail": "use_real_profile is on but no supported default Chromium was detected"}
    if detected == UNSUPPORTED_CHANNEL:
        return {"ok": False, "detail": "use_real_profile is on but the default is a pre-release Chromium channel"}
    return {"ok": True, "detail": f"real profile resolves to {detected}"}


def _web_backend_check() -> dict[str, Any]:
    from tools.web_tools import _ensure_web_plugins_loaded

    _ensure_web_plugins_loaded()
    from agent.web_search_registry import (
        get_active_extract_provider,
        get_active_search_provider,
    )

    parts = []
    ok = True
    for capability, provider in (
        ("search", get_active_search_provider()),
        ("extract", get_active_extract_provider()),
    ):
        if provider is None:
            ok = False
            parts.append(f"{capability}: none")
            continue
        keyed = bool(provider.is_available())
        keyless = False
        if not keyed:
            check = getattr(provider, "is_keyless_available", None)
            keyless = bool(check()) if callable(check) else False
        if not (keyed or keyless):
            ok = False
        tier = "keyed" if keyed else "keyless" if keyless else "unavailable"
        parts.append(f"{capability}: {provider.name} ({tier})")
    return {"ok": ok, "detail": "; ".join(parts) + " (quota not probed)"}


CHECKS = (
    ("tool schemas", _tool_schema_check),
    ("real-profile browser", _browser_check),
    ("web research backend", _web_backend_check),
)


def run() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for label, check in CHECKS:
        try:
            results[label] = check()
        except Exception as exc:  # each check reports its own failure
            results[label] = {"ok": False, "detail": f"probe failed: {type(exc).__name__}: {exc}"}
    return results


def main() -> int:
    sys.stdout.write(json.dumps(run(), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
