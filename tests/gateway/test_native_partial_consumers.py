"""Native producer results reach real consumers offline, without becoming finals."""

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from agent import codex_runtime
from agent.transports import codex_app_server_session as sessions
from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms import api_server
from gateway.run import _normalize_empty_agent_response, _should_clear_resume_pending_after_turn
from gateway.session import SessionSource
from hermes_state import SessionDB


DRAFT = "A native draft that must remain accessible."
ERROR = "native terminal incomplete"


@pytest_asyncio.fixture
async def inert_api_executor(monkeypatch):
    """Exercise real API execution/result code while excluding worker scheduling.

    All collaborators are inert and SQLite is a temporary local store. Return
    completed Futures so these consumer tests do not depend on executor wakeups;
    gateway tests above separately retain the real threaded stream boundary.
    """
    loop = asyncio.get_running_loop()

    def run_in_executor(executor, func, *args):
        assert executor is None, "consumer test unexpectedly requested a custom executor"
        future = loop.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr(loop, "run_in_executor", run_in_executor)
    yield


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    turn = sessions.TurnResult(
        final_text=DRAFT, partial_text=DRAFT, completed=False,
        error=ERROR, thread_id="native-thread", turn_id="native-turn",
        projected_messages=[{"role": "assistant", "content": DRAFT}],
    )
    memory, review, results = [], [], []

    class NativeSession:
        def __init__(self, **kwargs):
            pass

        def matches_route(self, **kwargs):
            return True

        def run_turn(self, **kwargs):
            return turn

        def close(self):
            pass

    monkeypatch.setattr(sessions, "CodexAppServerSession", NativeSession)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *args: {})
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *args: None)

    class NativeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "partial-session")
            self.model = "openai-codex/gpt-test"
            self.provider = "openai-codex"
            self.api_mode = "codex_app_server"
            self.session_cwd = str(tmp_path)
            self.context_rebase_enabled = False
            self._skill_nudge_interval = 1
            self._iters_since_skill = 0
            self.valid_tool_names = {"skill_manage"}
            self.tools = []
            self.max_iterations = 500
            self._active_children = []
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.emit_deltas = False

        def clear_interrupt(self):
            self._interrupt_requested = False

        def _sync_external_memory_for_turn(self, **kwargs):
            memory.append(kwargs)

        def _spawn_background_review(self, **kwargs):
            review.append(kwargs)

        def run_conversation(self, user_message=None, conversation_history=None, **kwargs):
            self._interrupt_requested = turn.interrupted
            self._interrupt_message = None
            if self.emit_deltas and self.stream_delta_callback:
                self.stream_delta_callback(DRAFT)
            messages = list(conversation_history or [])
            messages.append({"role": "user", "content": user_message})
            result = codex_runtime.run_codex_app_server_turn(
                self, user_message=user_message, original_user_message=user_message,
                messages=messages, effective_task_id=self.session_id,
                should_review_memory=True,
            )
            results.append(result)
            return result

    return SimpleNamespace(Agent=NativeAgent, turn=turn, results=results, memory=memory, review=review)


def _assert_incomplete(native):
    assert native.results
    for result in native.results:
        assert result["final_response"] == ""
        assert result["partial_response"] == DRAFT
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["error"] == ERROR
        assert result["interrupted"] is native.turn.interrupted
    assert native.memory == []
    assert native.review == []


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_cli_chat_renders_labeled_native_draft(native, interrupted, streaming):
    from tests.cli.test_cli_interrupt_ack_race import _make_cli
    import cli as cli_module

    native.turn.interrupted = interrupted
    cli = _make_cli()
    cli.agent = native.Agent(session_id=cli.session_id)
    cli.agent.emit_deltas = streaming
    cli.agent.stream_delta_callback = cli._stream_delta
    panels = []
    printed = []
    with patch.object(cli, "_ensure_runtime_credentials", return_value=True), \
         patch.object(cli, "_resolve_turn_agent_config", return_value={
             "signature": cli._active_agent_route_signature,
             "model": None, "runtime": None, "request_overrides": None,
         }), \
         patch.object(cli, "_init_agent", return_value=True), \
         patch.object(cli_module, "ChatConsole") as console, \
         patch.object(cli_module, "_cprint", side_effect=printed.append):
        console.return_value.print.side_effect = panels.append
        response = cli.chat("continue")

    assert DRAFT in response
    assert "Partial response" in response
    assert ERROR in response
    if streaming:
        assert sum(DRAFT in str(text) for text in printed) == 1
        assert sum("Partial response" in str(text) for text in printed) == 1
        assert not any(DRAFT in str(getattr(panel, "renderable", "")) for panel in panels)
    else:
        assert any(DRAFT in str(getattr(panel, "renderable", "")) for panel in panels)
    assert cli._last_turn_interrupted is interrupted
    _assert_incomplete(native)


@pytest.mark.parametrize("interrupted", [False, True])
def test_quiet_single_query_main_exposes_draft_and_failure(native, monkeypatch, capsys, interrupted):
    import cli as cli_module
    import signal

    native.turn.interrupted = interrupted
    cli = SimpleNamespace(
        agent=native.Agent(), session_id="partial-session", conversation_history=[],
        _claim_active_session=lambda *args, **kwargs: True,
        _ensure_runtime_credentials=lambda: True,
        _init_agent=lambda **kwargs: True,
        _active_agent_route_signature=("inert",),
        _resolve_turn_agent_config=lambda *args: {
            "signature": ("inert",), "model": None, "runtime": None,
            "request_overrides": None,
        },
    )
    monkeypatch.setattr(cli_module, "HermesCLI", lambda **kwargs: cli)
    monkeypatch.setattr(cli_module, "CLI_CONFIG", {"worktree": False})
    monkeypatch.setattr(cli_module.atexit, "register", lambda *args: None)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli_module, "_finalize_single_query", lambda *args: None)
    monkeypatch.setenv("HERMES_INTERACTIVE", "")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "")
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "0")
    with pytest.raises(SystemExit) as exit_info:
        cli_module.main(query="continue", quiet=True, toolsets="terminal")
    stdout, stderr = capsys.readouterr()
    assert stdout.strip() == DRAFT
    assert "Partial response" in stderr and ERROR in stderr
    assert exit_info.value.code == 1
    _assert_incomplete(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_gateway_actual_turn_preserves_native_partial(native, monkeypatch, tmp_path, streaming, interrupted):
    from tests.gateway.test_stale_finalize_suppression import FinalizeCaptureAdapter, _make_runner
    import gateway.run as gateway_run

    native.turn.interrupted = interrupted
    adapter = FinalizeCaptureAdapter()
    runner = _make_runner(adapter)
    runner.config.streaming = StreamingConfig(enabled=streaming, edit_interval=0.01, buffer_threshold=1)
    (tmp_path / "config.yaml").write_text(json.dumps({
        "display": {"tool_progress": "off", "interim_assistant_messages": False},
        "streaming": {"enabled": streaming, "edit_interval": 0.01, "buffer_threshold": 1},
    }))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {})
    fake_agent_module = types.ModuleType("run_agent")

    class StreamingNativeAgent(native.Agent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.emit_deltas = streaming

    fake_agent_module.AIAgent = StreamingNativeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_agent_module)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="partial-chat", chat_type="dm")
    result = await asyncio.wait_for(runner._run_agent(
        message="continue", context_prompt="", history=[], source=source,
        session_id="partial-session", session_key="agent:main:telegram:dm:partial-chat",
    ), timeout=5)

    assert result["final_response"] == ""
    assert result["partial_response"] == DRAFT
    assert result["completed"] is False
    assert result["interrupted"] is interrupted
    assert not _should_clear_resume_pending_after_turn(result)
    delivery = _normalize_empty_agent_response(result, result["final_response"])
    assert "Partial response" in delivery and ERROR in delivery
    if streaming:
        assert result["partial_response_previewed"] is True
        assert DRAFT not in delivery
        payloads = [msg["content"] for msg in adapter.sent] + [msg["content"] for msg in adapter.edits]
        assert any(DRAFT in payload for payload in payloads)
        assert sum(DRAFT in msg["content"] for msg in adapter.sent) == 1
    else:
        assert DRAFT in delivery
        assert adapter.sent == []
    _assert_incomplete(native)


class CaptureStream:
    def __init__(self, *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.chunks = []

    async def prepare(self, request):
        pass

    async def write(self, data):
        self.chunks.append(data)

    async def write_eof(self):
        pass


def _events(stream):
    events = []
    for frame in b"".join(stream.chunks).decode().split("\n\n"):
        lines = frame.splitlines()
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if data and data != "[DONE]":
            name = next((line[7:] for line in lines if line.startswith("event: ")), None)
            events.append((name, json.loads(data)))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "responses", "session"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("mode", ["batch", "stream_result_only", "stream_deltas"])
async def test_api_real_handlers_preserve_native_partial(native, monkeypatch, tmp_path, inert_api_executor, endpoint, interrupted, mode):
    native.turn.interrupted = interrupted
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True))
    db = SessionDB(tmp_path / "consumer-state.db")
    db.create_session("partial-session", "api_server")
    adapter._session_db = db

    def create_agent(**kwargs):
        agent = native.Agent(**kwargs)
        agent.emit_deltas = mode == "stream_deltas"
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    monkeypatch.setattr(api_server, "_publish_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server, "_clear_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server.web, "StreamResponse", CaptureStream)
    streaming = mode != "batch"
    body = {"stream": streaming, "model": "hermes-agent"}
    if endpoint == "chat":
        body["messages"] = [{"role": "user", "content": "continue"}]
        handler = adapter._handle_chat_completions
    elif endpoint == "responses":
        body["input"] = "continue"
        handler = adapter._handle_responses
    else:
        body["message"] = "continue"
        handler = adapter._handle_session_chat_stream if streaming else adapter._handle_session_chat
    request = SimpleNamespace(
        headers={}, match_info={"session_id": "partial-session"},
        json=AsyncMock(return_value=body), query={},
    )
    try:
        response = await asyncio.wait_for(handler(request), timeout=5)
        assert response.status == 200
        if not streaming:
            payload = json.loads(response.text)
            if endpoint == "chat":
                assert payload["choices"][0]["message"]["content"] == DRAFT
                assert payload["choices"][0]["finish_reason"] == "error"
                outcome = payload["hermes"]
            elif endpoint == "responses":
                assert payload["status"] == "incomplete"
                assert payload["output"][-1]["content"][0]["text"] == DRAFT
                assert payload["output"][-1]["status"] == "incomplete"
                outcome = payload["hermes"]
                assert adapter._response_store.get(payload["id"])["response"]["status"] == "incomplete"
            else:
                assert payload["message"]["content"] == DRAFT
                outcome = payload
        else:
            events = _events(response)
            if endpoint == "chat":
                content = "".join(event["choices"][0]["delta"].get("content", "") for _, event in events)
                assert content == DRAFT
                assert events[-1][1]["choices"][0]["finish_reason"] == "error"
                outcome = events[-1][1]["hermes"]
            elif endpoint == "responses":
                deltas = [event["delta"] for name, event in events if name == "response.output_text.delta"]
                assert "".join(deltas) == DRAFT
                terminal = events[-1][1]["response"]
                assert terminal["status"] == "incomplete"
                assert terminal["output"][-1]["content"][0]["text"] == DRAFT
                assert terminal["output"][-1]["status"] == "incomplete"
                assert not any(name == "response.completed" for name, _ in events)
                outcome = terminal["hermes"]
            else:
                terminal = next(event for name, event in events if name == "assistant.completed")
                assert terminal["content"] == DRAFT
                outcome = terminal
                terminal_name = "run.cancelled" if interrupted else "run.failed"
                run = next(event for name, event in events if name == terminal_name)
                assert run["completed"] is False
                assert adapter._run_statuses[run["run_id"]]["status"] != "completed"
                assert adapter._run_statuses[run["run_id"]]["last_event"] == terminal_name
                assert not any(name == "run.completed" for name, _ in events)
        assert outcome["completed"] is False
        assert outcome["partial"] is True
        assert outcome["interrupted"] is interrupted
        assert outcome["error"] == ERROR
        _assert_incomplete(native)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_runs_actual_execution_preserves_draft_without_completed_status(native, monkeypatch, inert_api_executor, interrupted):
    native.turn.interrupted = interrupted
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: native.Agent(**kwargs))
    monkeypatch.setattr(api_server, "_publish_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server, "_clear_turn_process_ownership", lambda *args: None)
    request = SimpleNamespace(headers={}, json=AsyncMock(return_value={
        "input": "continue", "session_id": "partial-session",
    }))
    response = await asyncio.wait_for(adapter._handle_runs(request), timeout=5)
    assert response.status == 202
    run_id = json.loads(response.text)["run_id"]
    task = adapter._active_run_tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=5)
    outcome = adapter._run_statuses[run_id]
    assert outcome["status"] == ("cancelled" if interrupted else "failed")
    assert outcome["output"] == DRAFT
    assert outcome["completed"] is False
    assert outcome["partial"] is True
    assert outcome["interrupted"] is interrupted
    assert outcome["error"] == ERROR
    events = []
    queue = adapter._run_streams[run_id]
    while not queue.empty():
        event = queue.get_nowait()
        if event is not None:
            events.append(event)
    assert not any(event.get("event") == "run.completed" for event in events)
    assert any(event.get("output") == DRAFT for event in events)
    _assert_incomplete(native)
