"""RecursiveIntell llm-pipeline transport for Hermes.

Provides a Rust-backed LLM calling interface that replaces raw httpx/openai
calls. Falls back gracefully if the native extension is not installed.

Usage::

    from agent.transports.ri_llm import RiPipeline

    pipe = RiPipeline("http://localhost:11434", "llama3.2:3b")
    result = pipe.call("What is 2+2?")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_NATIVE_AVAILABLE = False
try:
    from llm_pipeline._native import LlmConfig, Pipeline as _NativePipeline

    _NATIVE_AVAILABLE = True
except ImportError:
    logger.debug("llm-pipeline native extension not available; using None")


class RiLlmConfig:
    """Python-side mirror of the Rust LlmConfig."""

    def __init__(
        self,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        thinking: bool = False,
        json_mode: bool = False,
    ):
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.json_mode = json_mode

    def _to_native(self):
        if not _NATIVE_AVAILABLE:
            return None
        return LlmConfig(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            thinking=self.thinking,
            json_mode=self.json_mode,
        )


class RiPipeline:
    """Rust-backed LLM pipeline for Hermes transport."""

    def __init__(self, url: str, model: str, *, config: RiLlmConfig | None = None):
        self.url = url
        self.model = model
        self.config = config or RiLlmConfig()
        self._native: _NativePipeline | None = None
        if _NATIVE_AVAILABLE:
            self._native = _NativePipeline(
                url, model, config=self.config._to_native()
            )

    @property
    def available(self) -> bool:
        return self._native is not None

    def call(
        self,
        prompt: str,
        *,
        system: str | None = None,
        config: RiLlmConfig | None = None,
    ) -> str:
        """Call the LLM and return the raw response text."""
        if self._native is None:
            raise RuntimeError(
                "llm-pipeline native extension is not installed. "
                "Install with: pip install llm-pipeline"
            )
        native_config = config._to_native() if config else None
        return self._native.call(prompt, system=system, config=native_config)

    def call_structured(
        self,
        prompt: str,
        json_schema: str,
        *,
        system: str | None = None,
        config: RiLlmConfig | None = None,
    ) -> str:
        """Call the LLM with a JSON schema constraint."""
        if self._native is None:
            raise RuntimeError("llm-pipeline native extension is not installed")
        native_config = config._to_native() if config else None
        return self._native.call_structured(
            prompt, json_schema, system=system, config=native_config
        )

    def __repr__(self) -> str:
        status = "native" if self.available else "unavailable"
        return f"RiPipeline(url={self.url}, model={self.model}, {status})"


# ── Phase 2: RiChatCompletionsTransport ─────────────────────────
#
# Plugs into chat_completion_helpers for request shapes that the native
# Ollama binding can preserve. Default-incompatible requests retain their SDK
# route. Explicit native selection refuses unsupported requests before effects.

import math as _math
import os as _os
from types import SimpleNamespace as _SimpleNamespace
from urllib.parse import urlsplit as _urlsplit


class RiTransportUnsupported(ValueError):
    """The selected native binding cannot preserve this request's contract."""

    def __init__(self, reason: str):
        self.code = "RI_PIPELINE_REQUEST_UNSUPPORTED"
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


class RiCompletionResponse(_SimpleNamespace):
    """Internal raw-text binding result with no reported finish or usage."""


def configure_ri_pipeline(agent, config: dict) -> None:
    """Hydrate transport selection from the slash command's canonical owner."""
    from hermes_cli.llm_pipeline_switch import get_current_state

    enabled, providers = get_current_state(config)
    agent._ri_pipeline_enabled = enabled
    agent._ri_pipeline_providers = providers
    section = config.get("agent", {}) if isinstance(config, dict) else {}
    selection = section.get("llm_pipeline", {}) if isinstance(section, dict) else {}
    agent._ri_pipeline_explicit = bool(providers) or (
        isinstance(selection, dict) and selection.get("enabled") is True
    )


def _native_text_request(agent, api_kwargs: dict):
    """Qualify the prompt-only, unauthenticated Ollama binding before effects.

    The pinned Python API accepts one prompt, one system string and LlmConfig.
    It has no chat history, tools, media, headers, auth or reported-usage API.
    """
    if str(getattr(agent, "provider", "")).strip().lower() != "ollama-launch":
        raise RiTransportUnsupported("native binding supports only ollama-launch")
    base_url = getattr(agent, "base_url", None)
    if not isinstance(base_url, str):
        raise RiTransportUnsupported("missing endpoint")
    try:
        endpoint = _urlsplit(base_url)
        endpoint.port  # Validate the port without making a connection.
        valid_endpoint = (
            endpoint.scheme in {"http", "https"} and endpoint.hostname
            and endpoint.username is None and endpoint.password is None
            and not endpoint.query and not endpoint.fragment
            and endpoint.path.rstrip("/") in {"", "/v1", "/api"}
        )
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise RiTransportUnsupported("endpoint cannot be preserved by native binding")
    # Never invoke a credential supplier during selection/qualification.
    api_key = getattr(agent, "api_key", None)
    if api_key not in (None, "", "no-key-required", "ollama"):
        raise RiTransportUnsupported("authenticated route requires a per-call native auth API")
    client_options = getattr(agent, "_client_kwargs", {})
    if type(client_options) is not dict or set(client_options) - {"api_key", "base_url"}:
        raise RiTransportUnsupported("canonical client options unavailable in native binding")
    if "base_url" in client_options and client_options["base_url"] != base_url:
        raise RiTransportUnsupported("canonical endpoint differs from native endpoint")
    if "api_key" in client_options and client_options["api_key"] != api_key:
        raise RiTransportUnsupported("canonical credential differs from native credential")
    if type(api_kwargs) is not dict:
        raise RiTransportUnsupported("invalid request")
    allowed = {"model", "messages", "temperature", "max_tokens", "stream"}
    if set(api_kwargs) - allowed:
        raise RiTransportUnsupported("request fields unavailable in native binding")
    messages = api_kwargs.get("messages")
    if not isinstance(messages, list) or len(messages) not in {1, 2}:
        raise RiTransportUnsupported("chat history unavailable in native binding")
    roles = [item.get("role") if isinstance(item, dict) else None for item in messages]
    if roles not in (["user"], ["system", "user"]):
        raise RiTransportUnsupported("message roles unavailable in native binding")
    if any(set(item) != {"role", "content"} or not isinstance(item["content"], str) for item in messages):
        raise RiTransportUnsupported("message metadata or media unavailable in native binding")
    system = messages[0]["content"] if len(messages) == 2 else None
    if system == "":
        raise RiTransportUnsupported("empty system role unavailable in native binding")
    if "{input}" in messages[-1]["content"]:
        raise RiTransportUnsupported("native prompt template would alter literal input placeholder")
    model = api_kwargs.get("model", getattr(agent, "model", None))
    if not isinstance(model, str) or not model.strip():
        raise RiTransportUnsupported("invalid model")
    if "temperature" not in api_kwargs or "max_tokens" not in api_kwargs:
        raise RiTransportUnsupported("omitted generation defaults unavailable in native binding")
    temperature = api_kwargs["temperature"]
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not _math.isfinite(temperature)):
        raise RiTransportUnsupported("invalid temperature")
    max_tokens = api_kwargs["max_tokens"]
    if type(max_tokens) is not int or not 0 < max_tokens <= 2**32 - 1:
        raise RiTransportUnsupported("invalid max_tokens")
    if type(api_kwargs.get("stream", False)) is not bool:
        raise RiTransportUnsupported("invalid stream setting")
    return base_url, model, messages[-1]["content"], system, RiLlmConfig(
        temperature=temperature, max_tokens=max_tokens,
    )


def _should_use_ri_pipeline(agent, api_kwargs: dict | None = None) -> bool:
    """Return True when the RiPipeline fast path should be used.

    Default availability is restricted to representable Ollama requests.
    An explicit native selection must pass the typed dispatch qualification.
    Set HERMES_RI_PIPELINE=0 to disable, or HERMES_RI_PIPELINE_PROVIDERS
    to a comma-separated whitelist (e.g. 'ollama-launch,deepseek').
    If no env whitelist is set, agent._ri_pipeline_enabled and
    agent._ri_pipeline_providers from config.yaml determine eligibility.
    """
    if _os.environ.get("HERMES_RI_PIPELINE") == "0":
        return False
    if not _NATIVE_AVAILABLE:
        return False

    if not bool(getattr(agent, "_ri_pipeline_enabled", True)):
        return False

    explicit = bool(getattr(agent, "_ri_pipeline_explicit", False))
    explicit = explicit or _os.environ.get("HERMES_RI_PIPELINE") == "1"
    provider = str(getattr(agent, "provider", "")).strip().lower()
    whitelist = _os.environ.get("HERMES_RI_PIPELINE_PROVIDERS")
    if whitelist:
        allowed = _normalize_ri_pipeline_provider_list(whitelist)
        if provider not in allowed:
            return False
        explicit = True
    else:
        config_whitelist = _normalize_ri_pipeline_provider_list(
            getattr(agent, "_ri_pipeline_providers", [])
        )
        if config_whitelist:
            if provider not in config_whitelist:
                return False
            explicit = True
    # Default availability never selects an incompatible wire protocol.
    if not explicit and provider != "ollama-launch":
        return False
    if api_kwargs is not None:
        try:
            _native_text_request(agent, api_kwargs)
        except RiTransportUnsupported:
            # Explicit selection reaches the typed refusal in dispatch. Ordinary
            # unsupported requests retain their existing same-provider SDK path.
            return explicit
    return True


def _normalize_ri_pipeline_provider_list(raw_providers):
    """Normalize a provider whitelist input into a lowercase set."""
    if raw_providers is None:
        return set()
    if isinstance(raw_providers, str):
        values = raw_providers.split(",")
    elif isinstance(raw_providers, (list, tuple, set)):
        values = raw_providers
    else:
        return set()

    return {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }


def ri_pipeline_chat_completion(agent, api_kwargs: dict):
    """Run a chat-completion request through the Rust llm-pipeline.

    Accepts the same kwargs shape as ``client.chat.completions.create()``
    and returns an OpenAI-compatible response namespace so the rest of
    the agent loop is unchanged.
    """
    base_url, model, prompt, system, config = _native_text_request(agent, api_kwargs)
    return _ri_chat_completion_impl(model, prompt, system, base_url, config)


def _ri_chat_completion_impl(model, prompt, system, base_url, config):
    pipe = RiPipeline(base_url, model, config=config)
    if not pipe.available:
        raise RuntimeError("RiPipeline native extension not available")

    raw = pipe.call(prompt, system=system)

    # Build response namespace matching OpenAI shape
    message = _SimpleNamespace(
        role="assistant",
        content=raw,
        tool_calls=None,
    )
    choice = _SimpleNamespace(
        index=0,
        message=message,
        finish_reason=None,
    )
    # The binding returns raw text only. Estimates cannot attest provider usage,
    # billing, compaction effectiveness or a known-fitting request baseline.
    return RiCompletionResponse(choices=[choice], usage=None, model=model)
