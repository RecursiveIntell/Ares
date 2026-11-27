"""Pure helpers for the Context Governor adapter.

Moved verbatim from ContextGovernorEngine static methods. Functions here read only
their arguments and the standard library: no engine state, no subprocess, no I/O.
The engine re-exports each one as a static method of the same name.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
import copy
import json
import re
import shutil


def _default_binary() -> str:
    """The governor command to run when none is configured.

    `shutil.which` covers the usual installs. The bare name is the fallback so the caller resolves it the
    same way it resolves anything else; this module must not name one machine's home directory.
    """
    return shutil.which("context-governor") or "context-governor"


def _context_lineage_root_with_evidence(
    session_db: Any, session_id: str
) -> tuple[str, bool]:
    """Resolve a root only through complete, owner-backed ancestry evidence."""
    current = str(session_id or "default")
    getter = getattr(type(session_db), "get_session", None)
    if session_db is None or not callable(getter):
        return current, False
    seen: set[str] = set()
    for _ in range(1000):
        if not current or current in seen:
            return current, False
        seen.add(current)
        try:
            row = getter(session_db, current)
        except Exception:
            return current, False
        if not isinstance(row, dict) or "parent_session_id" not in row:
            return current, False
        raw_parent_id = row.get("parent_session_id")
        if raw_parent_id is None:
            return current, True
        # SessionDB's nullable TEXT column yields either None or a string.
        # Do not let falsey malformed values (False, 0, [], "") masquerade
        # as an owner-confirmed logical root and replace a known veto key.
        if not isinstance(raw_parent_id, str) or not raw_parent_id:
            return current, False
        parent_id = raw_parent_id
        if row.get("source") == "tool":
            return current, True
        if "model_config" not in row:
            return current, False
        raw_config = row.get("model_config")
        if raw_config is None:
            # SessionDB stores a missing optional configuration as SQL
            # NULL. Only that exact owner representation means an empty
            # config; other falsey values are malformed, not defaults.
            config = {}
        elif isinstance(raw_config, str):
            if not raw_config.strip():
                return current, False
            try:
                config = json.loads(raw_config)
            except (TypeError, ValueError):
                return current, False
        else:
            config = raw_config
        if not isinstance(config, dict):
            return current, False
        # Validate owner markers before comparing them. SessionDB's fork
        # owner uses OR semantics: either marker naming the immediate
        # parent establishes a fork; another valid marker may be inherited
        # provenance on that row and does not contradict the owner result.
        marker_values = []
        for marker_key in ("_branched_from", "_delegate_from"):
            marker_value = config.get(marker_key)
            if marker_value is None:
                continue
            if not isinstance(marker_value, str) or not marker_value:
                return current, False
            marker_values.append(marker_value)
        if parent_id in marker_values:
            return current, True
        reset_from = config.get("_reset_from")
        if reset_from is not None and (
            not isinstance(reset_from, str) or not reset_from
        ):
            return current, False
        try:
            parent = getter(session_db, parent_id)
        except Exception:
            return current, False
        if not isinstance(parent, dict) or "end_reason" not in parent:
            return current, False
        reason = parent.get("end_reason")
        from hermes_state_common import _RESET_END_REASONS

        if reset_from == parent_id:
            # SessionDB persists this owner marker before a parent may be
            # reopened, so the marker—not mutable end_reason—is the durable
            # reset-boundary evidence for this immediate edge.
            return current, True
        if reason in _RESET_END_REASONS:
            child_key = row.get("session_key")
            parent_key = parent.get("session_key")
            if (
                isinstance(child_key, str)
                and child_key
                and child_key == parent_key
            ):
                # Match SessionDB's legacy reset-child fallback exactly:
                # same non-empty route key plus a recognized reset end.
                return current, True
        if reason == "compression":
            current = parent_id
            continue
        if reason == "context_rebase":
            transition_lookup = getattr(
                type(session_db), "context_rebase_transition_for_session", None
            )
            if not callable(transition_lookup):
                return current, False
            try:
                transition = transition_lookup(session_db, current)
            except Exception:
                return current, False
            if (
                transition is None
                or getattr(transition, "child_session_id", None) != current
                or getattr(transition, "parent_session_id", None) != parent_id
            ):
                return current, False
            current = parent_id
            continue
        return current, False
    return current, False


def _strip_image_parts(content: list) -> str | None:
    """Strip image parts from multimodal content, return text summary."""
    text_parts = []
    had_image = False
    for part in content:
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif part.get("type") in {"image", "image_url", "input_image"}:
                had_image = True
    if had_image:
        return "[image removed] " + " ".join(text_parts)[:200]
    return None


def _summarize_tool_result(tool_name: str, tool_args: str, content: str) -> str:
    """Generate an informative 1-line summary of a tool result."""
    lines = content.strip().split("\n")
    line_count = len(lines)

    # Try to extract exit code
    exit_code = None
    for line in lines[-5:]:
        if "exit_code" in line.lower():
            try:
                import re

                m = re.search(r'exit_code["\']?\s*[:=]\s*["\']?(\d+)', line)
                if m:
                    exit_code = m.group(1)
                    break
            except Exception:
                pass

    # Try to parse tool args for context
    args_preview = ""
    if tool_args:
        try:
            parsed = json.loads(tool_args)
            if isinstance(parsed, dict):
                for key in ("command", "path", "file_path", "query"):
                    if key in parsed:
                        args_preview = (
                            f" `{parsed[key]}`"
                            if len(str(parsed[key])) < 80
                            else ""
                        )
                        break
        except Exception:
            pass

    parts = [f"[{tool_name}]{args_preview}"]
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    parts.append(f"{line_count} lines output")
    return ", ".join(parts)


def _without_host_todo_snapshots(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return a copy without host-refreshed todo snapshot suffixes."""
    from tools.todo_tool import TODO_INJECTION_HEADER

    normalized = copy.deepcopy(messages)
    for message in normalized:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            marker = content.find(TODO_INJECTION_HEADER)
            if marker >= 0:
                prefix = content[:marker]
                # The host appends the todo block with exactly two newline
                # separator bytes. Remove only those owned bytes: ``rstrip``
                # also erased receipt-bound whitespace from the real user
                # message (notably the trailing newline from --query-file),
                # so pending validation rejected an otherwise exact live
                # continuation projection.
                if prefix.endswith("\n\n"):
                    prefix = prefix[:-2]
                message["content"] = prefix
        elif isinstance(content, list):
            message["content"] = [
                part
                for part in content
                if not (
                    isinstance(part, dict)
                    and str(part.get("text") or "")
                    .lstrip()
                    .startswith(TODO_INJECTION_HEADER)
                )
            ]
    return normalized


def _empty_tip_shape_is_valid(tip: dict) -> bool:
    """Check the empty Rust tip shape; omitted epoch alone defaults to zero."""
    epoch = tip.get("lineage_epoch", 0)
    return (
        tip.get("compacted_messages") == []
        and tip.get("receipt_id") is None
        and tip.get("generation") is None
        and type(epoch) is int
        and epoch == 0
    )


def _message_from_governor(msg: Dict[str, Any]) -> Dict[str, Any]:
    metadata = msg.get("metadata") or {}
    content = msg.get("content") or ""
    if isinstance(metadata, dict) and isinstance(
        metadata.get("hermes_content"), list
    ):
        content = copy.deepcopy(metadata["hermes_content"])
    out = {"role": msg.get("role") or "assistant", "content": content}
    if msg.get("name"):
        # ``name`` is the provider-facing shape; ``tool_name`` is Hermes'
        # durable SessionDB column. Carry both so the in-memory transcript
        # and a resumed transcript normalize to the same receipt message.
        out["name"] = msg.get("name")
        out["tool_name"] = msg.get("name")
    if msg.get("id") and msg.get("role") == "tool":
        out["id"] = msg.get("id")
    # Restore OpenAI-specific fields from metadata
    if isinstance(metadata, dict):
        governor_projection = bool(
            metadata.get("compressed_summary") is True
            and msg.get("name") == "context_governor"
            and msg.get("role") in {"assistant", "user"}
        )
        if isinstance(metadata.get("hermes_metadata"), dict):
            out["metadata"] = copy.deepcopy(metadata["hermes_metadata"])
        if metadata.get("tool_calls"):
            out["tool_calls"] = metadata["tool_calls"]
        if metadata.get("tool_call_id"):
            out["tool_call_id"] = metadata["tool_call_id"]
        if governor_projection:
            out["_compressed_summary"] = True
    return out


def _expected_summary_message_id(response: dict[str, Any]) -> str | None:
    """Return the one Rust allocation-plan summary identity, if valid."""
    plan = response.get("allocation_plan") or {}
    receipt = response.get("receipt") or {}
    if not isinstance(plan, dict) or not isinstance(receipt, dict):
        return None
    plan_id = plan.get("plan_id")
    if (
        not isinstance(plan_id, str)
        or not re.fullmatch(r"ctxp_[A-Za-z0-9_-]+", plan_id)
        or receipt.get("allocation_plan_id") != plan_id
    ):
        return None
    return f"summary_{plan_id}"


def _is_exact_summary_message(
    message: dict[str, Any], expected_summary_id: str | None
) -> bool:
    """Match every core-owned summary discriminator, never user markers."""
    metadata = message.get("metadata")
    return bool(
        expected_summary_id
        and message.get("id") == expected_summary_id
        and message.get("name") == "context_governor"
        and message.get("role") in {"assistant", "user"}
        and isinstance(metadata, dict)
        and metadata.get("compressed_summary") is True
    )


def _validated_checkpoint_receipt(
    response: dict[str, Any],
) -> dict[str, Any] | None:
    """Return receipt evidence suitable for deterministic scheduling."""
    receipt = response.get("receipt") or {}
    if not isinstance(receipt, dict):
        return None
    hashes = (
        receipt.get("original_transcript_blake3"),
        receipt.get("compacted_transcript_blake3"),
    )
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in hashes
    ):
        return None
    generation = receipt.get("generation")
    original_tokens = receipt.get("original_approx_tokens")
    compacted_tokens = receipt.get("compacted_approx_tokens")
    savings = receipt.get("token_savings_estimate")
    numeric = (generation, original_tokens, compacted_tokens, savings)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in numeric
    ):
        return None
    if (
        generation < 1
        or original_tokens <= 0
        or compacted_tokens < 0
        or savings < 0
        or compacted_tokens > original_tokens
        or savings != original_tokens - compacted_tokens
    ):
        return None
    return receipt


def _clarification_block(clarifications: List[Dict[str, Any]]) -> str:
    """Render bounded-summary body text for resolved clarify answers."""
    encoded = json.dumps(
        clarifications,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="backslashreplace").decode("utf-8")
    return (
        "\n\nResolved prior user clarifications (historical Q/A; do not ask "
        "again unless scope changes):\n"
        f"{encoded}"
    )


def _summary_message_index(messages: List[Dict[str, Any]]) -> int | None:
    """Locate only the summary tagged from this compact-v2 response."""
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        marker = message.get("_context_governor_summary_id")
        if isinstance(marker, str) and marker.startswith("summary_ctxp_"):
            return index
    return None


def _reserved_carrier_syntax_outside_fallback(summary: str) -> str | None:
    """Return the first host-reserved syntax found outside its owned section.

    The model may echo or propose fallback references inside
    ``EXACT FALLBACK REFS`` because that entire body is replaced by the
    authenticated host carrier. Everywhere else, receipt-looking syntax is
    rejected so a generated section cannot impersonate provenance or
    inflate the durable checkpoint counter.
    """
    exact_marker = "=== EXACT FALLBACK REFS ==="
    losses_marker = "=== SUMMARY LOSSES ==="
    try:
        exact_start = summary.index(exact_marker) + len(exact_marker)
        exact_end = summary.index(losses_marker, exact_start)
    except ValueError:
        return "section_contract"
    outside = summary[:exact_start] + "\n" + summary[exact_end:]
    patterns = (
        (
            "llm_checkpoint_marker",
            r"(?i)\bllm_checkpoint_(?:receipt|session_sha256)\s*=",
        ),
        ("receipt_id", r"(?i)(?<![\w])receipt_id\s*="),
        ("generation", r"(?i)(?<![\w])generation\s*="),
        (
            "lineage_hash",
            r"(?i)(?<![\w])lineage_(?:blake3|sha256)\s*=",
        ),
        (
            "original_transcript_hash",
            r"(?i)(?<![\w])original_transcript_(?:blake3|sha256)\s*=",
        ),
        (
            "ctxs_hash_line",
            r"(?i)\bctxs_[a-z0-9_-]+\s*\|\s*blake3\s*:[^\n|]*"
            r"\|\s*sha256\s*:",
        ),
    )
    for name, pattern in patterns:
        if re.search(pattern, outside):
            return name
    return None


def _normalize_llm_summary_output(content: str) -> str | None:
    """Accept only the specialized prompt's structured output.

    Reasoning-capable models sometimes prepend analysis despite the output
    contract. Strip a leading preamble only when the structured response
    that follows is complete; otherwise retain the extractive summary
    rather than feeding an unstable free-form summary into the next
    compaction cycle.

    The render-prompt system prompt declares that anything before
    ``=== ACTIVE TASK ===`` causes rejection. We honor this by stripping
    preamble (rather than discarding the whole output) only when the
    complete structured response is present — ``=== ACTIVE TASK ===``
    through ``=== PRIOR CONTEXT SUMMARY ===``. A free-form response with
    no structural markers is rejected outright.
    """
    markers = (
        "=== ACTIVE TASK ===",
        "=== ACCEPTANCE GATES ===",
        "=== EXACT FALLBACK REFS ===",
        "=== SUMMARY LOSSES ===",
        "=== PRIOR CONTEXT SUMMARY ===",
    )
    text = str(content or "").strip()
    if any(text.count(marker) != 1 for marker in markers):
        return None
    positions = [text.index(marker) for marker in markers]
    if positions != sorted(positions):
        return None
    start = positions[0]
    # Strip preamble (text before the first structural marker).
    if start > 0:
        text = text[start:]
    return text


def _extract_error_class(content: str) -> str | None:
    """Extract a rough error class from tool output content."""
    content_lower = content.lower()
    if "compile_error" in content_lower or "error[e" in content_lower:
        return "compile_error"
    if "timeout" in content_lower:
        return "timeout"
    if "not found" in content_lower or "no such file" in content_lower:
        return "not_found"
    if "permission" in content_lower:
        return "permission_denied"
    return "general_error"


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") in {"image", "image_url", "input_image"}:
                    parts.append("[image]")
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)
