"""#84733: prompt-cache TTL/prefix propagation into MoA/aux paths + failover re-preflight.

The main loop threads ``agent._cache_ttl`` and the stable system prefix into
``build_prompt_cache_plan``, but the MoA/aux helper only accepted
``cache_disabled`` — so a configured ``1h`` regressed to the 5m default and
the destination system prompt was marked as one whole breakpoint. These
tests pin the threaded parameters (TTL + static prefix) on
``plan_cache_sections_for_destination`` and the MoA decoration helper, the
per-destination Qwen clamp (1h -> 5m), and the failover re-preflight
contract (every fallback activation must restart the outer iteration so the
pre-API preflight re-runs against the fallback's context window).
"""

import ast
import inspect
import copy
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests.run_agent.test_run_agent import agent as canonical_minimal_agent


@pytest.fixture
def minimal_agent(request):
    # Canonical constructor, with its metadata warm-up made inert as well.
    with patch("agent.agent_init.fetch_model_metadata", return_value={}):
        return request.getfixturevalue("canonical_minimal_agent")


class TestFailoverConversationBehavior:
    """Full-loop witnesses with canonical real-agent construction and inert LLMs."""

    @staticmethod
    def response(text):
        return SimpleNamespace(id="inert", model="inert", usage=None,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=text, tool_calls=None, reasoning=None, reasoning_content=None))])

    @staticmethod
    def prepare(item):
        item._cached_system_prompt = "Model: primary\nProvider: openrouter\nAnswer the current question."
        item.model = "g2-primary"
        item.provider = "openrouter"
        item._use_prompt_caching = False
        item.save_trajectories = False
        item.context_rebase_enabled = False
        item.platform = "cron"
        item._api_max_retries = 3
        item.max_iterations = 4
        item.tools = []
        item._valid_tool_names = set()
        item._cached_system_prompt_static = None
        item.compression_checkpoint_required = False

    @staticmethod
    def external_boundaries(stack, item):
        # Keep request building, preflight, dispatch and fallback real. Only
        # unrelated persistence/display and external client creation are inert.
        for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources",
                     "_try_refresh_env_client_credentials", "_should_start_quiet_spinner"):
            stack.enter_context(patch.object(item, name, return_value=False))
        stack.enter_context(patch.object(item, "_create_request_openai_client", side_effect=lambda **_:item.client))
        stack.enter_context(patch.object(item, "_create_openai_client", side_effect=lambda *_, **__:item.client))
        stack.enter_context(patch("agent.auxiliary_client.get_model_context_length", return_value=64000))
        stack.enter_context(patch("agent.model_metadata.fetch_model_metadata", return_value={}))

    def test_smaller_window_fallback_rebuilds_and_compacts_before_dispatch(self, minimal_agent):
        from agent import conversation_loop as loop
        from agent.model_metadata import estimate_request_tokens_rough
        item = minimal_agent
        self.prepare(item)
        item.compression_enabled = True
        item.compression_in_place = False
        compressor = item.context_compressor
        compressor.protect_first_n = 1
        compressor.protect_last_n = 2
        compressor.update_model(model=item.model, context_length=256000,
            base_url=item.base_url, api_key=item.api_key, provider=item.provider,
            api_mode=item.api_mode, max_tokens=1024)
        history = [{"role":"user" if i % 2 == 0 else "assistant",
                    "content":f"historical row {i}: " + "long evidence " * 1800}
                   for i in range(20)]
        before = copy.deepcopy(history)
        events, requests = [], []
        primary, fallback = MagicMock(), MagicMock()
        fallback.base_url = "https://fallback.invalid/v1"
        primary.chat.completions.create.side_effect = lambda **kw: (
            events.append(("dispatch", "primary")), requests.append(copy.deepcopy(kw)),
            (_ for _ in ()).throw(ValueError("ordinary SDK validation failure")))[-1]
        def complete(**kw):
            events.append(("dispatch", "fallback"))
            requests.append(copy.deepcopy(kw))
            return self.response("fallback completed")
        fallback.chat.completions.create.side_effect = complete
        item.client = primary
        item._fallback_chain = [dict(provider="openai", model="g2-fallback", api_key="inert-test-key",
                                    base_url=str(fallback.base_url), api_mode="chat_completions")]
        item._fallback_index = 0
        def summary(**kw):
            events.append(("summary", item.model))
            return self.response("Task Snapshot: historical evidence reviewed.\nPending Asks: answer the current question.")
        real_estimate = loop.estimate_messages_tokens_rough
        def measure(*args, **kwargs):
            result = real_estimate(*args, **kwargs)
            events.append(("preflight", item.model, compressor.context_length, result))
            return result
        real_build = item._build_api_kwargs
        def build(*args, **kwargs):
            events.append(("build", item.model))
            return real_build(*args, **kwargs)
        with ExitStack() as stack:
            self.external_boundaries(stack, item)
            stack.enter_context(patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback,"g2-fallback")))
            stack.enter_context(patch("agent.model_metadata.get_model_context_length", return_value=64000))
            stack.enter_context(patch("agent.context_compressor.call_llm", side_effect=summary))
            stack.enter_context(patch("agent.auxiliary_client.call_llm", side_effect=summary))
            stack.enter_context(patch.object(loop, "estimate_messages_tokens_rough", side_effect=measure))
            stack.enter_context(patch.object(item, "_build_api_kwargs", side_effect=build))
            activation = stack.enter_context(patch.object(item, "_try_activate_fallback", wraps=item._try_activate_fallback))
            result = item.run_conversation("Answer this current question.", conversation_history=history)
        assert result["final_response"] == "fallback completed", (result, events)
        assert activation.call_count == 1
        assert len(requests) == 2
        assert compressor.context_length == 64000
        first = events.index(("dispatch", "primary"))
        last = events.index(("dispatch", "fallback"))
        pressure = [i for i,event in enumerate(events) if event[0] == "preflight" and event[1] == "g2-fallback"]
        summaries = [i for i,event in enumerate(events) if event[0] == "summary"]
        builds = [i for i,event in enumerate(events) if event == ("build", "g2-fallback")]
        assert pressure and summaries and builds, events
        assert first < pressure[0] < summaries[0] < builds[-1] < last, events
        assert any(events[i][3] >= compressor.threshold_tokens for i in pressure), events
        assert estimate_request_tokens_rough(requests[1]["messages"], tools=requests[1].get("tools")) < compressor.threshold_tokens
        assert requests[0]["messages"] != requests[1]["messages"]
        assert history == before, "request rebuilding mutated the caller's history"

    def test_typed_native_refusal_has_no_provider_or_fallback_attempt(self, minimal_agent):
        from agent.transports import ri_llm
        from agent import chat_completion_helpers as helpers
        item = minimal_agent
        self.prepare(item)
        item.compression_enabled = False
        item.provider = "ollama-launch"
        item.api_key = "no-key-required"
        item.base_url = "http://inert.invalid/v1"
        item._client_kwargs = {"api_key":item.api_key, "base_url":item.base_url}
        item._fallback_chain = [dict(provider="openai", model="unused", api_key="inert-test-key")]
        ri_llm.configure_ri_pipeline(item, {"agent":{"llm_pipeline":{"enabled":True}}})
        with ExitStack() as stack:
            self.external_boundaries(stack, item)
            unused_client = MagicMock()
            unused_client.base_url = "https://unused.invalid/v1"
            stack.enter_context(patch("agent.auxiliary_client.resolve_provider_client", return_value=(unused_client,"unused")))
            stack.enter_context(patch("agent.model_metadata.get_model_context_length", return_value=64000))
            native_pipeline, native_config = MagicMock(), MagicMock()
            stack.enter_context(patch.multiple(ri_llm, create=True,
                _NATIVE_AVAILABLE=True, _NativePipeline=native_pipeline, LlmConfig=native_config))
            selected = stack.enter_context(patch.object(helpers, "ri_pipeline_chat_completion", wraps=helpers.ri_pipeline_chat_completion))
            activation = stack.enter_context(patch.object(item, "_try_activate_fallback", wraps=item._try_activate_fallback))
            result = item.run_conversation("Question requiring unsupported chat history.",
                conversation_history=[{"role":"user","content":"prior"},{"role":"assistant","content":"answer"}])
            assert "RI_PIPELINE_REQUEST_UNSUPPORTED" in result.get("error", ""), result
            assert result.get("failed") is True
            assert selected.call_count == 1
            activation.assert_not_called()
            item.client.chat.completions.create.assert_not_called()
            native_pipeline.assert_not_called()
            native_config.assert_not_called()
            unused_client.chat.completions.create.assert_not_called()

    def test_exhausted_real_preflight_prevents_dispatch(self, minimal_agent):
        from agent.context_compressor import ContextCompressor
        from ares_runtime.continuity.runtime import ContextDispatchError
        item = minimal_agent
        self.prepare(item)
        item.compression_enabled = True
        # Real compressor policy with a history too short to summarize. Its
        # real no-progress result must stop admission before provider dispatch.
        compressor = item.context_compressor
        assert isinstance(compressor, ContextCompressor)
        compressor.update_model(model=item.model, context_length=64000,
            base_url=item.base_url, api_key=item.api_key, provider=item.provider,
            api_mode=item.api_mode, max_tokens=1024)
        history = [{"role":"user","content":"historical evidence " * 17000},
                   {"role":"assistant","content":"historical response " * 17000}]
        item.client.chat.completions.create.return_value = self.response("unexpected admission")
        with ExitStack() as stack:
            self.external_boundaries(stack, item)
            compression = stack.enter_context(patch.object(item, "_compress_context", wraps=item._compress_context))
            summary = stack.enter_context(patch("agent.context_compressor.call_llm", side_effect=AssertionError("short history must not call summarizer")))
            build = stack.enter_context(patch.object(item, "_build_api_kwargs", wraps=item._build_api_kwargs))
            dispatch = stack.enter_context(patch.object(item, "_interruptible_api_call", wraps=item._interruptible_api_call))
            with pytest.raises(ContextDispatchError, match="CONTEXT_PREFLIGHT_EXHAUSTED"):
                item.run_conversation("Answer the current question.", conversation_history=history)
            assert compression.call_count >= 1
            summary.assert_not_called()
            build.assert_not_called()
            dispatch.assert_not_called()
            item.client.chat.completions.create.assert_not_called()


def _collect_cache_controls(obj):
    """Return every ``cache_control`` marker dict reachable in ``obj``."""
    markers = []
    if isinstance(obj, dict):
        if "cache_control" in obj:
            markers.append(obj["cache_control"])
        for value in obj.values():
            markers.extend(_collect_cache_controls(value))
    elif isinstance(obj, list):
        for value in obj:
            markers.extend(_collect_cache_controls(value))
    return markers


class TestPlanCacheSectionsThreadsTtlAndPrefix:
    def test_cache_ttl_1h_reaches_markers(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.8",
            cache_disabled=False,
            cache_ttl="1h",
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "expected cache_control markers on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the configured 1h tier must reach the destination plan markers"
        )

    def test_static_system_prefix_gets_early_breakpoint(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "stable prefix\nvolatile suffix"},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.8",
            cache_disabled=False,
            cache_ttl="5m",
            static_system_prefix="stable prefix",
        )
        system_content = out_msgs[0]["content"]
        assert isinstance(system_content, list) and len(system_content) == 2, (
            "the destination system prompt must split into [static, volatile] "
            "parts instead of marking the whole prompt as one breakpoint"
        )
        assert system_content[0]["text"] == "stable prefix"
        assert system_content[1]["text"] == "\nvolatile suffix"

    def test_qwen_1h_clamped_to_5m(self):
        from agent.agent_runtime_helpers import plan_cache_sections_for_destination

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = plan_cache_sections_for_destination(
            messages,
            None,
            provider="opencode",
            base_url="https://api.opencode.ai",
            api_mode="chat_completions",
            model="qwen3.6-plus",
            cache_disabled=False,
            cache_ttl="1h",
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "opencode+qwen is a cache-honoring route"
        assert all("ttl" not in m for m in markers), (
            "Qwen's 5-minute-only context cache must clamp a configured 1h"
        )


class TestMoACacheControlThreadsTtl:
    def test_moa_decoration_uses_threaded_1h(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        runtime = {
            "provider": "anthropic",
            "model": "claude-opus-4.8",
            "base_url": "",
            "api_mode": "anthropic_messages",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False, cache_ttl="1h"
        )
        markers = _collect_cache_controls(out)
        assert markers, "expected MoA decoration on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the agent's 1h tier must stop regressing to 5m on MoA advisor calls"
        )
        # Caller messages must stay undecorated.
        assert not _collect_cache_controls(messages)

    def test_moa_qwen_1h_clamped_to_5m(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        runtime = {
            "provider": "opencode",
            "model": "qwen3.6-plus",
            "base_url": "",
            "api_mode": "chat_completions",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False, cache_ttl="1h"
        )
        markers = _collect_cache_controls(out)
        assert markers, "opencode+qwen is a cache-honoring MoA route"
        assert all("ttl" not in m for m in markers), (
            "MoA decoration must clamp 1h to 5m on Qwen destinations"
        )

    def test_moa_decoration_defaults_to_5m_without_ttl(self):
        from agent.moa_loop import _maybe_apply_moa_cache_control

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        runtime = {
            "provider": "anthropic",
            "model": "claude-opus-4.8",
            "base_url": "",
            "api_mode": "anthropic_messages",
        }
        out = _maybe_apply_moa_cache_control(
            messages, runtime, cache_disabled=False
        )
        markers = _collect_cache_controls(out)
        assert markers
        assert all("ttl" not in m for m in markers)


class TestFailoverRestartsPreflight:
    """#84733: a fallback provider switch must re-run the pre-API preflight.

    ``_try_activate_fallback`` already shrinks the compressor's context
    window to the fallback's; the pre-API preflight runs at the top of the
    OUTER iteration loop, before the retry loop. So the restart discipline
    is loop-aware:

    - Sites INSIDE the retry loop (``while retry_count < max_retries``)
      must ``break`` out of it with ``restart_with_rebuilt_messages`` set,
      so the handler after the retry loop refunds the budget and
      ``continue``s the outer iteration (which re-runs the preflight).
      A plain ``continue`` there would only re-fire the retry loop and
      skip the preflight — the original bug.
    - Sites DIRECTLY in the outer loop must ``continue`` — the next outer
      iteration re-runs the preflight already. A ``break`` there would
      exit the conversation loop and end the turn without ever calling
      the just-activated fallback.

    Source-level guard: parsing the function is cheap, and the assertion
    encodes the bug class — a new failover site added with the wrong
    restart statement for its loop fails here on purpose.
    """

    def test_every_fallback_activation_restarts_preflight(self):
        from agent import conversation_loop

        tree = ast.parse(inspect.getsource(conversation_loop.run_conversation))

        # Parent map so each site can be bound to its nearest enclosing loop.
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        retry_loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.While)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "retry_count"
        ]
        assert retry_loops, "expected the retry loop in run_conversation"
        retry_loop_ids = {id(loop) for loop in retry_loops}

        def _inside_retry_loop(node):
            cur = parents.get(node)
            while cur is not None:
                if id(cur) in retry_loop_ids:
                    return True
                cur = parents.get(cur)
            return False

        fallback_ifs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Attribute)
            and node.test.func.attr == "_try_activate_fallback"
        ]
        assert fallback_ifs, "expected _try_activate_fallback sites in run_conversation"
        # Every reference to _try_activate_fallback must be one of the matched
        # `if agent._try_activate_fallback(...):` sites — a site written as
        # `activated = agent._try_activate_fallback()` would silently escape
        # this guard.
        all_refs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "_try_activate_fallback"
        ]
        assert len(all_refs) == len(fallback_ifs), (
            "every _try_activate_fallback reference must be a direct "
            "`if agent._try_activate_fallback(...):` site so this guard "
            "can bind its restart discipline (#84733)"
        )
        for node in fallback_ifs:
            if _inside_retry_loop(node):
                assert any(isinstance(stmt, ast.Break) for stmt in node.body), (
                    "retry-loop fallback activation must break to the "
                    "restart-with-rebuilt-messages handler so the pre-API "
                    "preflight re-runs against the fallback's context "
                    "window (#84733)"
                )
            else:
                assert any(
                    isinstance(stmt, ast.Continue) for stmt in node.body
                ), (
                    "outer-loop fallback activation must continue the outer "
                    "iteration (which re-runs the preflight); a break here "
                    "would end the turn without calling the fallback (#84733)"
                )
                assert not any(
                    isinstance(stmt, ast.Break) for stmt in node.body
                ), (
                    "outer-loop fallback activation must not break — that "
                    "exits the conversation loop and ends the turn (#84733)"
                )

    def test_restart_handler_clears_preflight_block(self):
        """The single consumer of restart_with_rebuilt_messages must clear
        _preflight_compression_blocked, so every retry-loop failover gets a
        fresh preflight against the fallback's context window (#84733)."""
        from agent import conversation_loop

        tree = ast.parse(inspect.getsource(conversation_loop.run_conversation))
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "restart_with_rebuilt_messages"
        ]
        assert handlers, "expected the restart_with_rebuilt_messages handler"
        consumer = [
            node
            for node in handlers
            if any(
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute)
                    and t.attr == "restart_with_rebuilt_messages"
                    for t in stmt.targets
                )
                for stmt in node.body
            )
        ]
        assert consumer, "expected the flag-consuming handler"
        for node in consumer:
            assert any(
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Name)
                    and t.id == "_preflight_compression_blocked"
                    for t in stmt.targets
                )
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is False
                for stmt in node.body
            ), (
                "the restart handler must clear _preflight_compression_blocked "
                "so the re-run preflight isn't skipped (#84733)"
            )


class TestAuxFallbackReplanThreadsTtl:
    """#84733 follow-up: the auxiliary fallback replan path threads the
    configured tier too — it has no live agent, so it reads the same
    config key agent_init snapshots into ``agent._cache_ttl``."""

    def test_configured_cache_ttl_reads_valid_tiers(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "1h"}},
        )
        assert arh.configured_cache_ttl() == "1h"
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "5m"}},
        )
        assert arh.configured_cache_ttl() == "5m"

    def test_configured_cache_ttl_none_for_disabled_or_unknown(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        for value in ("off", False, None, "2h"):
            monkeypatch.setattr(
                "hermes_cli.config.load_config_readonly",
                lambda value=value: {"prompt_caching": {"cache_ttl": value}},
            )
            assert arh.configured_cache_ttl() is None, value

    def test_replan_threads_configured_ttl_to_markers(self, monkeypatch):
        from agent import auxiliary_client

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"prompt_caching": {"cache_ttl": "1h"}},
        )
        destination = auxiliary_client._FallbackDestination(
            "anthropic",
            "https://api.anthropic.com",
            "anthropic_messages",
            "claude-opus-4.8",
        )
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        out_msgs, _ = auxiliary_client._replan_synchronous_cache_sections(
            messages, None, destination=destination
        )
        markers = _collect_cache_controls(out_msgs)
        assert markers, "expected cache_control markers on a caching route"
        assert all(m.get("ttl") == "1h" for m in markers), (
            "the configured 1h tier must reach auxiliary fallback replans"
        )
