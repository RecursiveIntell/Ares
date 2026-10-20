"""Trial context cannot escape to unqualified auxiliary providers."""

from __future__ import annotations

import asyncio
import contextvars

import pytest


def _trial_runtime():
    return {
        "provider": "openai-codex",
        "model": "gpt-5-codex",
        "api_mode": "codex_app_server",
        "context_dispatch_required": True,
    }


def _raise_if_resolution_starts(*args, **kwargs):
    raise AssertionError("auxiliary resolution reached credential/provider work")


@pytest.mark.parametrize(
    "dispatch",
    ["provider", "cached", "vision", "sync_call"],
)
def test_trial_runtime_blocks_sync_auxiliary_dispatch_before_resolution(
    dispatch, monkeypatch
):
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    monkeypatch.setattr(ac, "_validate_proxy_env_urls", _raise_if_resolution_starts)
    monkeypatch.setattr(ac, "_resolve_task_provider_model", _raise_if_resolution_starts)

    with ac.scoped_runtime_main(_trial_runtime()):
        with pytest.raises(ContextDispatchError, match="CODEX_NATIVE_AUXILIARY_UNQUALIFIED"):
            if dispatch == "provider":
                ac.resolve_provider_client("openrouter")
            elif dispatch == "cached":
                ac._get_cached_client("auto")
            elif dispatch == "vision":
                ac.resolve_vision_provider_client(provider="auto")
            else:
                ac.call_llm(provider="openrouter", messages=[])


def test_trial_runtime_blocks_async_auxiliary_dispatch_before_resolution(monkeypatch):
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    monkeypatch.setattr(ac, "_resolve_task_provider_model", _raise_if_resolution_starts)

    async def dispatch():
        with ac.scoped_runtime_main(_trial_runtime()):
            with pytest.raises(
                ContextDispatchError,
                match="CODEX_NATIVE_AUXILIARY_UNQUALIFIED",
            ):
                await ac.async_call_llm(provider="openrouter", messages=[])

    asyncio.run(dispatch())


def test_explicit_runtime_override_cannot_clear_ambient_trial_guard(monkeypatch):
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    monkeypatch.setattr(ac, "_validate_proxy_env_urls", _raise_if_resolution_starts)
    ordinary_override = {
        "provider": "openrouter",
        "model": "some-model",
        "api_mode": "chat_completions",
    }
    with ac.scoped_runtime_main(_trial_runtime()):
        with pytest.raises(
            ContextDispatchError,
            match="CODEX_NATIVE_AUXILIARY_UNQUALIFIED",
        ):
            ac._get_cached_client("openrouter", main_runtime=ordinary_override)


def test_set_runtime_main_carries_trial_authority_flag():
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    token = ac.set_runtime_main(
        "openai-codex",
        "gpt-5-codex",
        api_mode="codex_app_server",
        context_dispatch_required=True,
    )
    try:
        with pytest.raises(
            ContextDispatchError,
            match="CODEX_NATIVE_AUXILIARY_UNQUALIFIED",
        ):
            ac._ensure_auxiliary_context_qualified()
    finally:
        ac.reset_runtime_main(token)


def test_unrequired_native_runtime_keeps_auxiliary_resolution(monkeypatch):
    from agent import auxiliary_client as ac

    client = object()
    monkeypatch.setattr(ac, "resolve_provider_client", lambda *a, **k: (client, "model"))
    runtime = {
        "provider": "openai-codex",
        "model": "gpt-5-codex",
        "api_mode": "codex_app_server",
        "context_dispatch_required": False,
    }
    with ac.scoped_runtime_main(runtime):
        resolved_client, model = ac._get_cached_client("openrouter")
    assert resolved_client is client
    assert model == "model"


@pytest.mark.parametrize(
    "override",
    [
        None,
        {},
        {
            "provider": "openrouter",
            "model": "some-model",
            "api_mode": "chat_completions",
            "context_dispatch_required": False,
        },
    ],
    ids=["empty", "missing-fields", "explicit-false"],
)
@pytest.mark.parametrize("dispatch", ["sync", "async"])
def test_nested_runtime_scope_cannot_clear_required_trial_guard(
    override, dispatch, monkeypatch
):
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    monkeypatch.setattr(ac, "_validate_proxy_env_urls", _raise_if_resolution_starts)
    monkeypatch.setattr(ac, "_resolve_task_provider_model", _raise_if_resolution_starts)

    async def invoke():
        with ac.scoped_runtime_main(_trial_runtime()):
            with ac.scoped_runtime_main(override):
                with pytest.raises(
                    ContextDispatchError,
                    match="CODEX_NATIVE_AUXILIARY_UNQUALIFIED",
                ):
                    if dispatch == "sync":
                        ac.resolve_provider_client("openrouter")
                    else:
                        await ac.async_call_llm(provider="openrouter", messages=[])

    asyncio.run(invoke())


def test_nested_runtime_scope_restores_parent_and_isolates_independent_context(monkeypatch):
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    monkeypatch.setattr(ac, "_validate_proxy_env_urls", _raise_if_resolution_starts)
    with ac.scoped_runtime_main(_trial_runtime()):
        with ac.scoped_runtime_main({"provider": "openrouter"}):
            assert ac._RUNTIME_MAIN_CONTEXT.get() == _trial_runtime()
            with pytest.raises(ContextDispatchError):
                ac._ensure_auxiliary_context_qualified()

        independent = contextvars.Context()

        def ordinary_context_dispatch():
            with ac.scoped_runtime_main({"provider": "openrouter"}):
                ac._ensure_auxiliary_context_qualified()

        independent.run(ordinary_context_dispatch)
        with pytest.raises(ContextDispatchError):
            ac._ensure_auxiliary_context_qualified()

    ac._ensure_auxiliary_context_qualified()


def test_set_runtime_main_cannot_clear_inherited_required_trial_guard():
    from ares_runtime.continuity.runtime import ContextDispatchError
    from agent import auxiliary_client as ac

    with ac.scoped_runtime_main(_trial_runtime()):
        token = ac.set_runtime_main("openrouter", "some-model", api_mode="chat_completions")
        try:
            runtime = ac._RUNTIME_MAIN_CONTEXT.get()
            assert runtime["provider"] == "openai-codex"
            assert runtime["model"] == "gpt-5-codex"
            assert runtime["api_mode"] == "codex_app_server"
            assert runtime["context_dispatch_required"] is True
            assert runtime["base_url"] == ""
            with pytest.raises(ContextDispatchError):
                ac._ensure_auxiliary_context_qualified()
        finally:
            ac.reset_runtime_main(token)
