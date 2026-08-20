"""Lazy core-tool schema loading for Hermes Agent.

When enabled, the full JSON schemas for CORE Hermes tools (terminal, file, web,
browser, delegate, memory, skills, cronjob, spotify, ...) are replaced in the
model-visible tools array by a single compact capability index plus one bridge
tool, ``request_tool_schema``. The model reads the index (name + one-line
purpose per core tool) every turn — a few hundred tokens instead of ~60K — and
hydrates a tool's full parameter schema on demand via ``request_tool_schema``.

Why a separate module from ``tools/tool_search.py``:
  Tool Search intentionally NEVER defers core tools (see tools/tool_search.py
  design notes). But the core surface is itself the dominant fixed cost: with
  zero MCP servers configured, a fresh session still exposes ~42 core tools and
  ~61K chars of schema (#6839, measurement by @0oAstro). This module closes that
  exact gap. It composes with Tool Search — Tool Search handles MCP/plugin
  bloat, this module handles the always-eager core surface — and the two never
  overlap because core tools are excluded from Tool Search's deferrable set.

Design invariants (mirror tools/tool_search.py):
  * OFF by default. ``tools.core_lazy.enabled: "off"`` → pure pass-through; no
    behavior change for any existing deployment.
  * Fail-open. Any error in assembly or dispatch returns the full eager list /
    a tool_error, never a broken request. Mirrors the ``except: logger.warning``
    guards in model_tools + tool_search.
  * The bridge routes through ``model_tools.handle_function_call`` like any
    direct call, so guardrails, plugin hooks, approvals all fire identically.
  * Session-scoped. ``request_tool_schema`` only returns a schema for a tool the
    current session's toolset scope actually grants (defense in depth against
    out-of-scope core-tool reads).
  * Per-session promoted state. Once a tool's schema is hydrated this turn, the
    caller keeps it hot (see assemble_core_tool_defs ``promoted`` argument) so a
    tool used once stays fully available for the rest of the turn without a
    second round-trip.

Wire-format hook: the compact index is built by ``build_compact_index`` so a
future compressed wire-format encoder can be slotted in there without touching
assembly or dispatch. Phase 1 ships the readable compact index.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.registry import tool_error

logger = logging.getLogger("tools.lazy_core_tools")

# Reserved bridge tool name. Must not collide with Tool Search bridge names
# (tool_search / tool_describe / tool_call) or any real core tool.
REQUEST_SCHEMA_NAME = "request_tool_schema"

BRIDGE_TOOL_NAMES = frozenset({REQUEST_SCHEMA_NAME})

# Cheap, provider-stable token estimate (chars/4). See tools/tool_search.py.
CHARS_PER_TOKEN = 4.0

# Compact index target. The whole point is to keep this tiny; default 1500
# tokens is ~6K chars, enough for ~42 core tools at ~140 chars each.
DEFAULT_INDEX_MAX_TOKENS = 1500


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreLazyConfig:
    """Resolved, validated core-lazy configuration for one assembly."""

    enabled: str  # "off" | "on" | "auto"
    # Core tool names that must ALWAYS stay eager (safety-critical / hot path).
    # Empty = none. Resolved from config; unknown names are dropped, not fatal.
    always_include: Tuple[str, ...] = ()
    # Hard cap on the compact index size in tokens (chars/4 estimate).
    index_max_tokens: int = DEFAULT_INDEX_MAX_TOKENS

    @classmethod
    def from_raw(cls, raw: Any) -> "CoreLazyConfig":
        if raw is False:
            return cls(enabled="off")
        if raw is True:
            return cls(enabled="on")
        if not isinstance(raw, dict):
            # None / anything else → off (safe default; opt-in only).
            return cls(enabled="off")
        enabled_raw = str(raw.get("enabled", "off")).strip().lower()
        if enabled_raw in ("true", "1", "yes"):
            enabled = "on"
        elif enabled_raw in ("false", "0", "no"):
            enabled = "off"
        elif enabled_raw in ("auto", "on", "off"):
            enabled = enabled_raw
        else:
            enabled = "off"
        always = raw.get("always_include") or []
        if isinstance(always, str):
            always = [always]
        always_include = (
            tuple(str(a).strip() for a in always if str(a).strip())
            if isinstance(always, (list, tuple))
            else ()
        )
        index_max = max(
            200,
            min(
                60000, _safe_int(raw.get("index_max_tokens"), DEFAULT_INDEX_MAX_TOKENS)
            ),
        )
        return cls(
            enabled=enabled, always_include=always_include, index_max_tokens=index_max
        )


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> CoreLazyConfig:
    """Load core-lazy config from the user config file (tools.core_lazy)."""
    try:
        from hermes_cli.config import load_config as _load

        cfg = _load() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        if not isinstance(tools_cfg, dict):
            tools_cfg = {}
        return CoreLazyConfig.from_raw(tools_cfg.get("core_lazy"))
    except Exception as e:  # pragma: no cover — never break tool loading
        logger.debug("Failed to load core_lazy config: %s", e)
        return CoreLazyConfig.from_raw(None)


# ---------------------------------------------------------------------------
# Core-tool classification
# ---------------------------------------------------------------------------


def _core_tool_names() -> frozenset[str]:
    """Return the set of core tool names (NEVER deferred by Tool Search)."""
    try:
        from toolsets import _HERMES_CORE_TOOLS

        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


def classify_core(
    tool_defs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split tool-defs into (non_core, core) for lazy-core assembly.

    non_core is passed through untouched (composes with Tool Search). core is
    the candidate set for compact-index replacement.
    """
    non_core: List[Dict[str, Any]] = []
    core: List[Dict[str, Any]] = []
    core_set = _core_tool_names()
    for td in tool_defs:
        name = (td.get("function") or {}).get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            continue  # defensive: never re-classify our own bridge
        if name in core_set:
            core.append(td)
        else:
            non_core.append(td)
    return non_core, core


# ---------------------------------------------------------------------------
# Token estimation + activation gate
# ---------------------------------------------------------------------------


def estimate_tokens_from_schemas(tool_defs: Iterable[Dict[str, Any]]) -> int:
    """chars/4 estimate of the schema cost of a tool-defs list."""
    total = 0
    for td in tool_defs:
        try:
            total += len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total += len(str(td))
    return int(__import__("math").ceil(total / CHARS_PER_TOKEN))


def should_activate(config: CoreLazyConfig, core_tokens: int) -> bool:
    """Activate only when enabled and at least one core tool is present."""
    if config.enabled == "off":
        return False
    if core_tokens <= 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Compact index builder
# ---------------------------------------------------------------------------


def _one_line_purpose(description: str, limit: int = 90) -> str:
    """First sentence / clause of a tool description, trimmed."""
    if not description:
        return ""
    # Prefer the first sentence; fall back to first clause on a comma/semicolon.
    first_sentence = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0]
    cut = first_sentence
    for sep in (",", ";", " —"):
        if sep in cut:
            cut = cut.split(sep, 1)[0]
    cut = cut.strip()
    if len(cut) > limit:
        cut = cut[: limit - 1].rstrip() + "\u2026"
    return cut


def build_compact_index(
    core_tools: List[Dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_INDEX_MAX_TOKENS,
    always_include: Tuple[str, ...] = (),
) -> str:
    """Build the embedded capability index: one compact line per core tool.

    Format: ``name — <one-line purpose>``. Sorted by name for determinism.
    Truncated to ``max_tokens`` (chars/4). Tools forced eager via
    ``always_include`` are noted but the bridge still lists everything so the
    model can hydrate any of them.
    """
    entries: List[str] = []
    for td in core_tools:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        purpose = _one_line_purpose(fn.get("description", ""))
        line = f"- {name} — {purpose}" if purpose else f"- {name}"
        entries.append(line)
    entries.sort()
    budget_chars = max_tokens * int(CHARS_PER_TOKEN)
    out: List[str] = []
    used = 0
    for line in entries:
        line_chars = len(line) + 1
        if used + line_chars > budget_chars and out:
            out.append(
                f"- \u2026 ({len(entries) - len(out)} more core tools — "
                f"load any with request_tool_schema)"
            )
            break
        out.append(line)
        used += line_chars
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Bridge tool schema
# ---------------------------------------------------------------------------


def bridge_tool_schema(compact_index: str, core_count: int) -> List[Dict[str, Any]]:
    """The single bridge tool the model sees in place of full core schemas."""
    desc = (
        f"{core_count} core Hermes tools are available on demand. Each line "
        "below is a tool you can call directly after loading its schema.\n\n"
        "To use a core tool, first call `request_tool_schema` with its exact "
        "name to load its full parameter schema, then call the tool normally. "
        "Tools already promoted this turn need no reload.\n\n"
        "Available core tools:\n" + compact_index
    )
    return [
        {
            "type": "function",
            "function": {
                "name": REQUEST_SCHEMA_NAME,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact core tool name from the index above.",
                        },
                    },
                    "required": ["name"],
                },
            },
        }
    ]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass
class CoreLazyResult:
    tool_defs: List[Dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    index_tokens: int = 0
    promoted: Tuple[str, ...] = ()


def assemble_core_tool_defs(
    tool_defs: List[Dict[str, Any]],
    *,
    context_length: Optional[int] = None,
    config: Optional[CoreLazyConfig] = None,
    promoted: Optional[Tuple[str, ...]] = None,
) -> CoreLazyResult:
    """Return the tool-defs list the model should see for CORE tools.

    non_core tools pass through untouched. Core tools are either kept eager
    (always_include + already-promoted this turn) or collapsed into the compact
    index + bridge. Idempotent: a second call with the bridge already present
    is a no-op.
    """
    if config is None:
        config = load_config()
    promoted = promoted or ()

    incoming = [
        td
        for td in tool_defs
        if (td.get("function") or {}).get("name") not in BRIDGE_TOOL_NAMES
    ]
    non_core, core = classify_core(incoming)
    if not core:
        return CoreLazyResult(tool_defs=incoming, activated=False)

    core_tokens = estimate_tokens_from_schemas(core)
    if not should_activate(config, core_tokens):
        return CoreLazyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=len(core),
            deferred_tokens=core_tokens,
        )

    # Keep safety-critical / promoted tools eager.
    always = set(config.always_include) | set(promoted)
    keep_eager: List[Dict[str, Any]] = []
    deferrable: List[Dict[str, Any]] = []
    for td in core:
        name = (td.get("function") or {}).get("name", "")
        if name in always:
            keep_eager.append(td)
        else:
            deferrable.append(td)
    if not deferrable:
        # Everything forced eager (e.g. always_include covers the whole core).
        return CoreLazyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=0,
            deferred_tokens=core_tokens,
        )

    compact_index = build_compact_index(
        deferrable,
        max_tokens=config.index_max_tokens,
        always_include=config.always_include,
    )
    bridge = bridge_tool_schema(compact_index, len(deferrable))
    result = non_core + keep_eager + bridge
    index_tokens = int(
        __import__("math").ceil(
            len(json.dumps(bridge, ensure_ascii=False, separators=(",", ":")))
            / CHARS_PER_TOKEN
        )
    )
    logger.info(
        "core_lazy activated: %d core tools eager (always_include/promoted), "
        "%d deferred (~%d tokens) behind request_tool_schema; index ~%d tokens",
        len(keep_eager),
        len(deferrable),
        core_tokens,
        index_tokens,
    )
    return CoreLazyResult(
        tool_defs=result,
        activated=True,
        deferred_count=len(deferrable),
        deferred_tokens=core_tokens,
        index_tokens=index_tokens,
        promoted=tuple(
            n for n in (td.get("function") or {}).get("name", "") for td in keep_eager
        ),
    )


# ---------------------------------------------------------------------------
# Bridge dispatch
# ---------------------------------------------------------------------------


def dispatch_request_tool_schema(
    args: Dict[str, Any],
    *,
    current_tool_defs: List[Dict[str, Any]],
) -> str:
    """Execute the ``request_tool_schema`` bridge tool. Returns JSON schema."""
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name is required")
    # Session-scoped: only return schemas for tools the session actually grants.
    for td in current_tool_defs:
        fn = td.get("function") or {}
        if fn.get("name") == name:
            return json.dumps(
                {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
                ensure_ascii=False,
            )
    # Not in the session's granted set.
    return tool_error(
        f"'{name}' is not an available core tool in this session. "
        "Check the spelling against the request_tool_schema index."
    )


def is_core_lazy_bridge(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES
