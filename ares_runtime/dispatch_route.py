"""Nonauthorizing facts from the final immutable inference request.

Logical materialization may name the requested provider. This independent
transport-owner projection names the effective wire endpoint and model selector;
no credential, raw query secret or request content enters the projection.
"""
from __future__ import annotations

import json
import re
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


def request_metadata(body: bytes, *, route: str) -> dict:
    result = {"resolved_route":None, "route_qualification_error":None,
              "output_token_ceiling":None, "output_bound_error":None}
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
