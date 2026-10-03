"""Exercise native runtime outcomes and qualification barriers offline."""
from types import SimpleNamespace

import pytest

from agent import codex_runtime
from agent.transports import codex_app_server_session as sessions
from ares_runtime.continuity.runtime import ContextDispatchError


@pytest.fixture
def runtime(monkeypatch):
    memory, review, construction = [], [], []
    turn = SimpleNamespace(final_text="draft", partial_text="draft", completed=False,
                           projected_messages=[], tool_iterations=0, interrupted=False,
                           error=None, should_retire=False, thread_id="thread", turn_id="turn")
    class DummySession:
        def __init__(self, **kwargs):
            construction.append(kwargs)
        def run_turn(self, **kwargs):
            return turn
        def close(self):
            pass
        def matches_route(self, **kwargs):
            return True
    monkeypatch.setattr(sessions, "CodexAppServerSession", DummySession)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *args: {})
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *args: None)
    agent = SimpleNamespace(model="openai-codex/gpt-test", provider="openai-codex",
                            api_mode="codex_app_server", session_cwd="/tmp", context_rebase_enabled=False,
                            _skill_nudge_interval=1, _iters_since_skill=0, valid_tool_names={"skill_manage"},
                            _sync_external_memory_for_turn=lambda **kw: memory.append(kw),
                            _spawn_background_review=lambda **kw: review.append(kw))
    def run():
        return codex_runtime.run_codex_app_server_turn(
            agent, user_message="dummy", original_user_message="dummy", messages=[],
            effective_task_id="offline", should_review_memory=True)
    return SimpleNamespace(agent=agent, turn=turn, run=run, memory=memory, review=review,
                           construction=construction)


def test_runtime_never_infers_success_from_partial_text(runtime):
    result = runtime.run()
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["final_response"] == ""
    assert result["partial_response"] == "draft"
    assert runtime.memory == []
    assert runtime.review == []


def test_runtime_passes_selected_model_and_provider(runtime):
    runtime.run()
    assert runtime.construction[0]["model"] == "openai-codex/gpt-test"
    assert runtime.construction[0]["provider"] == "openai-codex"
    assert runtime.construction[0]["subscription_only_trial"] is False


def test_native_trial_is_unqualified_before_child_dispatch(runtime):
    runtime.agent.context_rebase_enabled = True
    with pytest.raises(ContextDispatchError, match="PROVIDER_CONTEXT_RESET_UNQUALIFIED"):
        runtime.run()
    assert runtime.construction == []
    assert runtime.memory == []
    assert runtime.review == []


def test_runtime_accepts_only_explicit_success(runtime):
    runtime.turn.completed = True
    runtime.turn.final_text = "complete"
    runtime.turn.partial_text = ""
    result = runtime.run()
    assert result["completed"] is True
    assert result["final_response"] == "complete"
    assert len(runtime.memory) == 1
    assert len(runtime.review) == 1


@pytest.mark.parametrize("current_mode", ["codex_app_server", "chat_completions"])
def test_trial_prologue_blocks_native_primary_before_restore_or_auxiliary(monkeypatch, current_mode):
    from agent import turn_context

    restored = []
    def restore():
        restored.append(True)
        raise AssertionError("unqualified native primary reached runtime restoration")
    agent = SimpleNamespace(api_mode=current_mode, context_rebase_enabled=True,
                            _primary_runtime={"api_mode": "codex_app_server"},
                            session_id="offline", _memory_write_origin="assistant_tool",
                            _restore_primary_runtime=restore)
    monkeypatch.setattr(turn_context, "recover_rotated_compression_session", lambda *a: None)
    noop = lambda *a, **kw: None
    with pytest.raises(ContextDispatchError, match="PROVIDER_CONTEXT_RESET_UNQUALIFIED"):
        turn_context.build_turn_context(
            agent, "dummy", None, None, "offline", None, None,
            restore_or_build_system_prompt=noop, install_safe_stdio=noop,
            sanitize_surrogates=noop, summarize_user_message_for_log=noop,
            set_session_context=noop, set_current_write_origin=noop, ra=SimpleNamespace())
    assert restored == []


@pytest.fixture
def native_continuity(monkeypatch):
    """Drive the actual runtime and session with an inert authoritative thread."""
    transports = []

    class NativeTransport:
        def __init__(self, **kwargs):
            self.requests, self.notes, self.history = [], [], []
            self.closed = False
            self.thread_id = f"native-{len(transports) + 1}"
            self.reject_model = None
            transports.append(self)

        def initialize(self, **kwargs):
            return {}

        def request(self, method, params=None, timeout=30):
            self.requests.append((method, dict(params or {})))
            if method == "thread/start":
                return {"thread": {"id": self.thread_id}, "model": params["model"],
                        "modelProvider": params["modelProvider"]}
            if method == "turn/start":
                assert params["threadId"] == self.thread_id
                if params.get("model") == self.reject_model:
                    raise sessions.CodexAppServerError(code=-32602, message="unsupported turn model")
                self.history.append((params["model"], params["input"][0]["text"]))
                turn_id = f"turn-{len(self.history)}"
                # The response depends on retained native history, not Hermes' projection.
                text = " | ".join(value for _, value in self.history)
                self.notes.extend([
                    {"method": "item/completed", "params": {
                        "threadId": self.thread_id, "turnId": turn_id,
                        "item": {"type": "agentMessage", "id": f"item-{turn_id}", "text": text}}},
                    {"method": "turn/completed", "params": {
                        "threadId": self.thread_id, "turn": {"id": turn_id, "status": "completed"}}},
                ])
                return {"turn": {"id": turn_id}}
            raise AssertionError(f"unexpected RPC {method}")

        def take_notification(self, timeout=0):
            return self.notes.pop(0) if self.notes else None

        def take_server_request(self, timeout=0):
            return None

        def stderr_tail(self, count=20):
            return []

        def is_alive(self):
            return not self.closed

        def close(self):
            self.closed = True

    monkeypatch.setattr(sessions, "CodexAppServerClient", NativeTransport)
    monkeypatch.setattr(codex_runtime, "make_codex_app_server_event_bridge", lambda agent: None)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *args: {})
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *args: None)
    agent = SimpleNamespace(model="openai-codex/gpt-primary", provider="openai-codex",
                            api_mode="codex_app_server", session_cwd="/tmp", context_rebase_enabled=False,
                            _skill_nudge_interval=0, _iters_since_skill=0, valid_tool_names=set())
    messages = []

    def run(text):
        messages.append({"role": "user", "content": text})
        return codex_runtime.run_codex_app_server_turn(
            agent, user_message=text, original_user_message=text, messages=messages,
            effective_task_id="offline-native-continuity")

    return SimpleNamespace(agent=agent, transports=transports, run=run)


def test_native_model_switch_and_once_rollback_keep_authoritative_history(native_continuity):
    state = native_continuity
    first = state.run("remember this")
    canonical = state.agent._codex_session
    state.agent.model = "openai/gpt-once"
    second = state.run("use another model once")
    # This is the caller's --once rollback of the selected route. The protocol
    # setting is sticky, so the next turn must explicitly restore primary.
    state.agent.model = "openai-codex/gpt-primary"
    third = state.run("return to primary")
    assert first["completed"] and second["completed"] and third["completed"]
    assert state.agent._codex_session is canonical and not canonical._closed
    assert len(state.transports) == 1
    client = state.transports[0]
    assert not client.closed
    assert sum(method == "thread/start" for method, _ in client.requests) == 1
    starts = [params for method, params in client.requests if method == "turn/start"]
    assert [params["model"] for params in starts] == ["gpt-primary", "gpt-once", "gpt-primary"]
    assert {params["threadId"] for params in starts} == {first["codex_thread_id"]}
    assert third["final_response"] == "remember this | use another model once | return to primary"
    canonical.close()


def test_native_provider_change_refuses_before_closing_or_dispatch(native_continuity):
    state = native_continuity
    state.run("remember this")
    canonical = state.agent._codex_session
    count = len(state.transports[0].requests)
    state.agent.provider = "custom"
    refused = state.run("change provider")
    assert not refused["completed"] and "Reset" in refused["error"]
    assert state.agent._codex_session is canonical and not canonical._closed
    assert not state.transports[0].closed and len(state.transports[0].requests) == count
    canonical.close()


def test_unsupported_native_turn_model_is_visible_without_history_retirement(native_continuity):
    state = native_continuity
    state.run("remember this")
    canonical = state.agent._codex_session
    state.transports[0].reject_model = "unsupported"
    state.agent.model = "unsupported"
    refused = state.run("must be refused")
    assert not refused["completed"] and "unsupported turn model" in refused["error"]
    assert state.agent._codex_session is canonical and not canonical._closed
    assert state.transports[0].history == [("gpt-primary", "remember this")]
    state.agent.model = "gpt-primary"
    next_turn = state.run("try primary again")
    assert next_turn["completed"] and next_turn["final_response"] == "remember this | try primary again"
    canonical.close()


def test_explicit_native_reset_creates_a_distinct_thread(native_continuity):
    state = native_continuity
    first = state.run("old history")
    previous = state.agent._codex_session
    # Explicit reset ownership already closes and clears the native session.
    previous.close()
    state.agent._codex_session = None
    state.agent.model = "gpt-new"
    next_turn = state.run("new conversation")
    assert previous._closed and state.transports[0].closed
    assert next_turn["completed"] and next_turn["codex_thread_id"] != first["codex_thread_id"]
    assert len(state.transports) == 2 and next_turn["final_response"] == "new conversation"
    state.agent._codex_session.close()


def test_tui_once_restore_owner_preserves_native_thread_and_history(native_continuity, monkeypatch):
    """Real TUI snapshot/restore and AIAgent.switch_model; clients stay inert."""
    from types import MethodType
    from agent import agent_runtime_helpers as runtime_helpers
    from run_agent import AIAgent
    from tui_gateway import server
    from hermes_cli import config

    state = native_continuity
    agent = state.agent
    built = []
    agent.switch_model = MethodType(AIAgent.switch_model, agent)
    agent.api_key = "synthetic-native-key"
    agent.base_url = "https://offline.invalid/v1"
    agent.requested_provider = agent.provider
    agent.client = SimpleNamespace()
    agent._client_kwargs = {}
    agent._credential_pool = object()
    agent.context_compressor = None
    agent._fallback_chain = []
    agent._primary_runtime = None
    agent.quiet_mode = True
    agent._read_reasoning_echo_from_config = lambda: False
    agent._apply_client_headers_for_base_url = lambda *args: None
    agent._ensure_lmstudio_runtime_loaded = lambda *args: None
    agent._lmstudio_load_was_unverified = lambda *args: False
    agent._effective_lmstudio_context_length = lambda *args: None
    agent._anthropic_prompt_cache_policy = lambda **kwargs: (False, False)

    def create_client(kwargs, **unused):
        client = SimpleNamespace(kwargs=dict(kwargs))
        built.append(client)
        return client

    agent._create_openai_client = create_client
    monkeypatch.setattr(runtime_helpers, "get_provider_request_timeout", lambda *args: None)
    monkeypatch.setattr(runtime_helpers, "sync_credential_pool_entry_id", lambda *args: None)
    monkeypatch.setattr(config, "load_config_readonly", lambda *args, **kwargs: {})
    monkeypatch.setattr(config, "load_config", lambda *args, **kwargs: {})
    # No SDK constructor, credential read, metadata probe or external provider.
    first = state.run("retain native history")
    canonical = agent._codex_session
    snapshot = server._snapshot_agent_model_runtime(agent)
    agent.switch_model(
        new_model="gpt-once", new_provider="openai-codex", api_key=agent.api_key,
        base_url=agent.base_url, api_mode="codex_app_server",
    )
    temporary = state.run("temporary selection")
    server._restore_agent_model_runtime(agent, snapshot)
    assert agent.model == "openai-codex/gpt-primary"
    assert agent.api_mode == "codex_app_server" and agent._codex_session is canonical
    restored = state.run("after actual TUI restore")
    assert first["completed"] and temporary["completed"] and restored["completed"]
    assert len(built) == 2 and len(state.transports) == 1
    starts = [params for method, params in state.transports[0].requests if method == "turn/start"]
    assert [params["model"] for params in starts] == ["gpt-primary", "gpt-once", "gpt-primary"]
    assert {params["threadId"] for params in starts} == {first["codex_thread_id"]}
    assert restored["final_response"] == "retain native history | temporary selection | after actual TUI restore"
    assert not canonical._closed and not state.transports[0].closed
    canonical.close()
