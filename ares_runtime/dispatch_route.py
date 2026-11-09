"""Nonauthorizing facts from the final immutable inference request.

Logical materialization may name the requested provider. This independent
transport-owner projection names the effective wire endpoint and model selector;
no credential, raw query secret or request content enters the projection.
"""
from __future__ import annotations

import json
import re
from typing import NoReturn
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from hermes_cli.middleware import EgressDispatchDenied


def _json_object(body: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate provider request member")
            result[key] = value
        return result
    value = json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(value) is not dict:
        raise ValueError("provider request is not an object")
    return value


def _wire_family(route: str) -> tuple[str, str, str]:
    path = urlsplit(route).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return "openai.chat_completions", "openai_compatible", "chat_completions"
    if path.endswith("/responses"):
        return "openai.responses", "openai_compatible", "codex_responses"
    if path.endswith("/messages"):
        return "anthropic.messages", "anthropic", "anthropic_messages"
    if path.endswith((":generateContent", ":streamGenerateContent")):
        return "gemini.generate_content", "gemini_native", "chat_completions"
    if path.endswith(("/converse", "/converse-stream")):
        return "bedrock.converse", "bedrock", "bedrock_converse"
    raise EgressDispatchDenied("UNSUPPORTED_TRANSPORT", "inference wire protocol is not qualified")


def request_output_ceiling(body: bytes, *, route: str) -> int:
    """Same exact-body validator for observation and enforcing lease admission."""
    try:
        payload = _json_object(body)
        family, _, _ = _wire_family(route)
        if family == "openai.chat_completions":
            values = [payload[k] for k in ("max_tokens", "max_completion_tokens") if k in payload]
            if len(values) != 1 or type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
                raise ValueError("ambiguous output reservation")
            ceiling = values[0]
        elif family == "openai.responses":
            ceiling = payload["max_output_tokens"]
        elif family == "anthropic.messages":
            ceiling = payload["max_tokens"]
        elif family == "gemini.generate_content":
            config = payload["generationConfig"]
            if type(config.get("candidateCount", 1)) is not int or config.get("candidateCount", 1) != 1:
                raise ValueError("multiple candidates unsupported")
            ceiling = config["maxOutputTokens"]
        else:
            ceiling = payload["inferenceConfig"]["maxTokens"]
        if type(ceiling) is not int or ceiling <= 0:
            raise ValueError("output ceiling is not a positive integer")
        return ceiling
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError) as error:
        raise EgressDispatchDenied("UNSUPPORTED_OUTPUT_BOUND", "explicit unambiguous output field required") from error


def _endpoint_identity(route: str) -> str:
    parts = urlsplit(route)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username is not None or parts.password is not None:
        raise EgressDispatchDenied("ROUTE_IDENTITY_UNAVAILABLE")
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True):
        if key.lower() in {"key", "api_key", "api-key", "access_token", "token"}:
            continue  # Authentication is intentionally not a route selector.
        if key == "api-version" and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
            safe_query.append((key, value))
        elif key == "alt" and value in {"json", "sse"}:
            safe_query.append((key, value))
        else:
            raise EgressDispatchDenied("ROUTE_IDENTITY_UNAVAILABLE", "unknown query selector is not a qualified route")
    if len({key for key, _ in safe_query}) != len(safe_query):
        raise EgressDispatchDenied("ROUTE_IDENTITY_UNAVAILABLE", "duplicate route selector")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(sorted(safe_query)), ""))


def resolve_dispatch_route(body: bytes, *, route: str) -> dict:
    try:
        family, provider, api_mode = _wire_family(route)
        endpoint = _endpoint_identity(route)
        payload = _json_object(body)
        if family in {"openai.chat_completions", "openai.responses", "anthropic.messages"}:
            model = payload["model"]
        elif family == "gemini.generate_content":
            match = re.search(r"/models/([^/]+):(?:generateContent|streamGenerateContent)$", urlsplit(route).path)
            if not match:
                raise ValueError("missing Gemini model selector")
            model = "models/" + unquote(match.group(1), errors="strict")
        else:
            match = re.search(r"/model/([^/]+)/(?:converse|converse-stream)/?$", urlsplit(route).path)
            if not match:
                raise ValueError("missing Bedrock model selector")
            model = unquote(match.group(1), errors="strict")
        if type(model) is not str or not model.strip() or len(model.encode("utf-8")) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in model):
            raise ValueError("invalid model selector")
        return {"schema":"ares.resolved-dispatch-route/v1", "wire_protocol":family,
                "provider_identity":provider + ":" + endpoint, "model_ref":model, "api_mode":api_mode}
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
        raise EgressDispatchDenied("MODEL_IDENTITY_UNAVAILABLE") from error


def request_controls(body: bytes, *, route: str) -> dict:
    """Redacted observations from serialized bytes, never configured defaults.

    This is not authority, a provider-internal effort attestation, or proof that
    hooks/memory were disabled. Context digests permit comparison with isolated
    controls without copying prompt content into an observer's receipt.
    """
    family, _, _ = _wire_family(route)
    if family not in {"openai.chat_completions", "openai.responses", "anthropic.messages"}:
        raise ValueError("native controls protocol not qualified")
    payload = _json_object(body)
    result: dict[str, object] = {"schema": "ares.native-request-controls/v1"}
    for name in ("reasoning", "thinking", "output_config"):
        if name not in payload:
            continue
        value = payload[name]
        if type(value) is not dict:
            raise ValueError("ambiguous native control object")
        fields = {}
        allowed = {
            "reasoning": ("effort", "enabled"),
            "thinking": ("type", "budget_tokens"),
            "output_config": ("effort",),
        }[name]
        for field in allowed:
            if field not in value:
                continue
            item = value[field]
            if field == "enabled":
                valid = type(item) is bool
            elif field == "budget_tokens":
                valid = type(item) is int and item > 0
            else:
                valid = type(item) is str and re.fullmatch(r"[a-z0-9_.-]{1,32}", item)
            if not valid:
                raise ValueError("unqualified native control value")
            fields[field] = item
        result[name] = fields
    if "reasoning_effort" in payload:
        effort = payload["reasoning_effort"]
        if type(effort) is not str or not re.fullmatch(r"[a-z0-9_.-]{1,32}", effort):
            raise ValueError("unqualified native effort")
        result["reasoning_effort"] = effort
    tools = payload.get("tools", [])
    if type(tools) is not list or any(type(tool) is not dict for tool in tools):
        raise ValueError("unqualified tools array")
    result["tools_count"] = len(tools)
    context = {key: payload[key] for key in ("messages", "system", "instructions", "input") if key in payload}
    # Explicit serializer, not an RFC8785/JCS or semantic-equivalence claim.
    import hashlib

    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    result["context_fields"] = sorted(context)
    result["context_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return result


def validate_explicit_reasoning(expected: dict, controls: dict | None, *, protocol: str | None) -> None:
    """Require the invocation's intent in actual serialized native controls.

    This is field equality, not provider-internal reasoning attestation. Legacy
    omitted/config-only calls do not enter this explicit-invocation contract.
    The adapter remains the sole owner of symbolic-effort to token-budget data.
    """
    def deny(code: str) -> NoReturn:
        raise EgressDispatchDenied(code, "explicit reasoning lacks matching qualified native controls")

    if type(expected) is not dict or type(expected.get("enabled")) is not bool or type(controls) is not dict:
        deny("EXPLICIT_REASONING_UNQUALIFIED")
    enabled = expected["enabled"]
    effort = expected.get("effort") if enabled else "none"
    if type(effort) is not str:
        deny("EXPLICIT_REASONING_UNQUALIFIED")
    thinking = controls.get("thinking", {})
    reasoning = controls.get("reasoning", {})
    output = controls.get("output_config", {})
    if any(type(value) is not dict for value in (thinking, reasoning, output)):
        deny("EXPLICIT_REASONING_UNQUALIFIED")

    if protocol == "anthropic.messages":
        if reasoning or "reasoning_effort" in controls:
            deny("EXPLICIT_REASONING_UNQUALIFIED")
        if not enabled:
            if thinking.get("type") != "disabled" or output.get("effort") is not None:
                deny("EXPLICIT_REASONING_MISMATCH")
            return
        if thinking.get("type") == "adaptive":
            if output.get("effort") != effort:
                deny("EXPLICIT_REASONING_MISMATCH")
            return
        if thinking.get("type") == "enabled":
            from agent.anthropic_adapter import THINKING_BUDGET

            budget = THINKING_BUDGET.get(effort)
            if budget is None:
                deny("EXPLICIT_REASONING_UNSUPPORTED")
            if type(thinking.get("budget_tokens")) is not int or thinking["budget_tokens"] != budget or output.get("effort") is not None:
                deny("EXPLICIT_REASONING_MISMATCH")
            return
        deny("EXPLICIT_REASONING_MISMATCH")

    if protocol not in {"openai.responses", "openai.chat_completions"} or thinking or output:
        deny("EXPLICIT_REASONING_UNQUALIFIED")
    if reasoning.get("enabled") is not None and reasoning["enabled"] is not enabled:
        deny("EXPLICIT_REASONING_MISMATCH")
    emitted = []
    if "effort" in reasoning:
        emitted.append(reasoning["effort"])
    if "reasoning_effort" in controls:
        if protocol == "openai.responses":
            deny("EXPLICIT_REASONING_UNQUALIFIED")
        emitted.append(controls["reasoning_effort"])
    # OpenRouter's native Chat disable form has no symbolic effort. Accept
    # only an explicit boolean false with no conflicting effort fields; this
    # does not introduce that representation into the Responses protocol.
    if (not emitted and not enabled and protocol == "openai.chat_completions"
            and reasoning.get("enabled") is False):
        return
    if not emitted or any(type(value) is not str or value != effort for value in emitted):
        deny("EXPLICIT_REASONING_MISMATCH")


def request_metadata(body: bytes, *, route: str) -> dict:
    result: dict[str, object] = {"resolved_route":None, "route_qualification_error":None,
              "output_token_ceiling":None, "output_bound_error":None,
              "native_controls":None, "controls_qualification_error":None}
    try:
        result["native_controls"] = request_controls(body, route=route)
    except (EgressDispatchDenied, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        result["controls_qualification_error"] = "REQUEST_CONTROLS_UNAVAILABLE"
    try:
        result["resolved_route"] = resolve_dispatch_route(body, route=route)
    except EgressDispatchDenied as error:
        result["route_qualification_error"] = error.code
    try:
        result["output_token_ceiling"] = request_output_ceiling(body, route=route)
    except EgressDispatchDenied as error:
        result["output_bound_error"] = error.code
    return result


def is_noncompletion_request(request) -> bool:
    """Positive API allowlist only. Unknown paths remain subject to governors."""
    path = urlsplit(str(request.url)).path.rstrip("/")
    method = str(getattr(request, "method", "POST")).upper()
    if method in {"GET", "HEAD"} and re.search(r"/models(?:/[^/]+)?$", path):
        return True
    # These modality/vector APIs are outside the declared LLM-completion scope.
    if re.search(r"/(?:embeddings|images/(?:generations|edits|variations)|audio/(?:speech|transcriptions|translations))$", path):
        return True
    return False
