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
COMMENTARY = "Earlier unrelated commentary."
BRIDGE_EVENTS = [
    {"method": "item/agentMessage/delta", "params": {"delta": COMMENTARY}},
    {"method": "item/started", "params": {"item": {
        "type": "commandExecution", "id": "bridge-tool", "command": "inert command",
    }}},
    {"method": "item/completed", "params": {"item": {
        "type": "commandExecution", "id": "bridge-tool", "command": "inert command",
        "exitCode": 0, "aggregatedOutput": "inert result",
    }}},
    {"method": "item/agentMessage/delta", "params": {"delta": DRAFT[:20]}},
]


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
    memory, review, results, inputs, events = [], [], [], [], []

    class NativeSession:
        def __init__(self, **kwargs):
            self.on_event = kwargs.get("on_event")

        def matches_route(self, **kwargs):
            return True

        def run_turn(self, **kwargs):
            for event in events:
                self.on_event(event)
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
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.tool_start_callback = kwargs.get("tool_start_callback")
            self.tool_complete_callback = kwargs.get("tool_complete_callback")
            self._stream_callback = None
            self.emit_deltas = False
            self.stream_deltas = None
            self.history_prefix = None
            self.rotated_session_id = None

        def clear_interrupt(self):
            self._interrupt_requested = False

        def _sync_external_memory_for_turn(self, **kwargs):
            memory.append(kwargs)

        def _spawn_background_review(self, **kwargs):
            review.append(kwargs)

        def _fire_stream_delta(self, delta):
            if self.stream_delta_callback:
                self.stream_delta_callback(delta)
            if delta is not None and self._stream_callback:
                self._stream_callback(delta)

        def run_conversation(self, user_message=None, conversation_history=None, **kwargs):
            inputs.append({"session_id": self.session_id, "history": list(conversation_history or [])})
            self._interrupt_requested = turn.interrupted
            self._interrupt_message = None
            self._stream_callback = kwargs.get("stream_callback")
            if self.stream_delta_callback or self._stream_callback:
                for delta in (self.stream_deltas if self.stream_deltas is not None else [DRAFT] if self.emit_deltas else []):
                    self._fire_stream_delta(delta)
            messages = list(self.history_prefix if self.history_prefix is not None else conversation_history or [])
            messages.append({"role": "user", "content": user_message})
            result = codex_runtime.run_codex_app_server_turn(
                self, user_message=user_message, original_user_message=user_message,
                messages=messages, effective_task_id=self.session_id,
                should_review_memory=True,
            )
            if self.rotated_session_id:
                self.session_id = self.rotated_session_id
                self._last_compaction_in_place = True
            results.append(result)
            return result

    return SimpleNamespace(Agent=NativeAgent, turn=turn, results=results, inputs=inputs, events=events, memory=memory, review=review)


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


@pytest.mark.parametrize("deltas", [
    ["Earlier unrelated commentary."],
    [DRAFT[:20]],
    [DRAFT],
    ["Earlier unrelated commentary.", None, DRAFT[:20]],
    [DRAFT, None],
    "native_bridge",
    ["Earlier unrelated commentary ends in A"],
])
def test_cli_partial_draft_reconciles_actual_visible_text_across_tool_boundary(native, deltas):
    from tests.cli.test_cli_interrupt_ack_race import _make_cli
    import cli as cli_module

    cli = _make_cli()
    cli.final_response_markdown = "raw"
    cli.agent = native.Agent(session_id=cli.session_id, tool_progress_callback=cli._on_tool_progress)
    cli.agent.stream_deltas = [] if deltas == "native_bridge" else deltas
    if deltas == "native_bridge":
        native.events.extend(BRIDGE_EVENTS)
    cli.agent.stream_delta_callback = cli._stream_delta
    panels, printed = [], []
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

    displayed = "\n".join(str(text) for text in printed) + "\n" + "\n".join(
        getattr(panel.renderable, "plain", str(panel.renderable))
        for panel in panels if hasattr(panel, "renderable")
    )
    assert displayed.count(DRAFT[:20]) == 1
    assert displayed.count(DRAFT[20:]) == 1
    assert displayed.count("Partial response") == 1
    assert DRAFT in response and ERROR in response
    _assert_incomplete(native)


@pytest.mark.parametrize("deltas", [
    "native_bridge", "native_bridge_delayed", [DRAFT], [COMMENTARY], ["Earlier commentary ends in A"],
])
def test_cli_tts_display_reconciles_actual_sentence_callbacks(native, monkeypatch, deltas):
    from tests.cli.test_cli_interrupt_ack_race import _make_cli
    import cli as cli_module
    import threading
    from tools import tts_tool

    cli = _make_cli()
    cli.final_response_markdown = "raw"
    cli.streaming_enabled = False
    cli._voice_tts = True
    bridge = isinstance(deltas, str)
    tool_finished = threading.Event()

    def tool_progress(event_type, *args, **kwargs):
        cli._on_tool_progress(event_type, *args, **kwargs)
        if event_type == "tool.completed":
            tool_finished.set()

    cli.agent = native.Agent(session_id=cli.session_id, tool_progress_callback=tool_progress)
    cli.agent.stream_deltas = [] if bridge else deltas
    if bridge:
        native.events.extend(BRIDGE_EVENTS)
    sentences, printed, panels, gate_results = [], [], [], []

    def consume(text_queue, stop_event, done_event, display_callback=None):
        try:
            if deltas == "native_bridge_delayed":
                # Commentary is produced before tool.started, but no display
                # callback runs until the actual bridged tool has completed.
                gate_results.append(tool_finished.wait(timeout=2))
            while True:
                sentence = text_queue.get(timeout=2)
                if sentence is None:
                    return
                sentences.append(sentence)
                display_callback(sentence)
        finally:
            done_event.set()

    monkeypatch.setattr(tts_tool, "_import_sounddevice", lambda: None)
    monkeypatch.setattr(tts_tool, "check_tts_requirements", lambda: True)
    monkeypatch.setattr(tts_tool, "stream_tts_to_speaker", consume)
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
    assert sentences == ([COMMENTARY, DRAFT[:20]] if bridge else deltas)
    if deltas == "native_bridge_delayed":
        assert gate_results == [True]
    displayed = "\n".join(str(text) for text in printed) + "\n" + "\n".join(
        getattr(panel.renderable, "plain", str(panel.renderable))
        for panel in panels if hasattr(panel, "renderable")
    )
    # The real sentence callback strips trailing whitespace before display.
    assert displayed.count(DRAFT[:20].rstrip()) == 1
    assert displayed.count(DRAFT[20:]) == 1
    assert displayed.count("Partial response") == 1
    assert DRAFT in response and ERROR in response
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
@pytest.mark.parametrize("endpoint", ["chat", "responses"])
@pytest.mark.parametrize("deltas", [
    ["Earlier unrelated commentary."], [DRAFT[:20]], [DRAFT],
    ["Earlier unrelated commentary.", None, DRAFT[:20]],
    "native_bridge",
    ["Earlier unrelated commentary ends in A"],
])
async def test_api_stream_reconciles_distinct_or_partly_delivered_native_draft(
    native, monkeypatch, inert_api_executor, endpoint, deltas,
):
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True))

    def create_agent(**kwargs):
        agent = native.Agent(**kwargs)
        agent.stream_deltas = [] if deltas == "native_bridge" else deltas
        return agent

    if deltas == "native_bridge":
        native.events.extend(BRIDGE_EVENTS)
    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    monkeypatch.setattr(api_server, "_publish_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server, "_clear_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server.web, "StreamResponse", CaptureStream)
    body = {"stream": True, "model": "hermes-agent"}
    if endpoint == "chat":
        body["messages"] = [{"role": "user", "content": "continue"}]
        handler = adapter._handle_chat_completions
    else:
        body["input"] = "continue"
        handler = adapter._handle_responses
    request = SimpleNamespace(headers={}, json=AsyncMock(return_value=body), query={})
    response = await asyncio.wait_for(handler(request), timeout=5)
    events = _events(response)
    if deltas == "native_bridge":
        if endpoint == "chat":
            progress = [event for name, event in events if name == "hermes.tool.progress"]
            assert [event["status"] for event in progress] == ["running", "completed"]
        else:
            assert any(event.get("item", {}).get("type") == "function_call_output" for _, event in events)
    if endpoint == "chat":
        text = "".join(event["choices"][0]["delta"].get("content", "")
                       for _, event in events if "choices" in event)
        assert events[-1][1]["choices"][0]["finish_reason"] == "error"
        outcome = events[-1][1]["hermes"]
    else:
        text = "".join(event["delta"] for name, event in events if name == "response.output_text.delta")
        terminal = events[-1][1]["response"]
        assert terminal["status"] == "incomplete"
        assert terminal["output"][-1]["content"][0]["text"] == DRAFT
        assert terminal["output"][-1]["status"] == "incomplete"
        outcome = terminal["hermes"]
    assert text.count(DRAFT[:20]) == 1
    assert text.count(DRAFT[20:]) == 1
    assert outcome["completed"] is False and outcome["error"] == ERROR
    _assert_incomplete(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True, "disconnect_after_result"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_incomplete_responses_snapshot_preserves_native_tool_context_and_rotated_session(
    native, monkeypatch, inert_api_executor, streaming, compressed,
):
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True))
    prior = [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"}]
    summary = [{"role": "user", "content": "[Earlier context summary]"}]
    native.turn.projected_messages = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "native-tool", "type": "function", "function": {"name": "terminal", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "native-tool", "content": "real projected tool output"},
        {"role": "assistant", "content": DRAFT},
    ]
    adapter._response_store.put("resp_prior", {
        "response": {"id": "resp_prior", "status": "completed"},
        "conversation_history": prior, "session_id": "native-parent",
    })
    agents = []

    def create_agent(**kwargs):
        agent = native.Agent(**kwargs)
        if compressed and not agents:
            # Simulate a compressed continuation at the inert native session
            # boundary; real _run_agent annotates rotation/compression itself.
            agent.history_prefix = summary
            agent.rotated_session_id = "native-child"
        agents.append(agent)
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    monkeypatch.setattr(api_server, "_publish_turn_process_ownership", lambda *args: None)
    monkeypatch.setattr(api_server, "_clear_turn_process_ownership", lambda *args: None)
    class DisconnectAfterResult(CaptureStream):
        async def write(self, data):
            if b"event: response.output_text.done\n" in data:
                raise ConnectionResetError("inert transport disconnected after native result")
            await super().write(data)

    monkeypatch.setattr(api_server.web, "StreamResponse", DisconnectAfterResult if streaming == "disconnect_after_result" else CaptureStream)
    monkeypatch.setattr(api_server, "_reap_disconnected_agent_processes", lambda *args: None)
    request = SimpleNamespace(headers={}, query={}, json=AsyncMock(return_value={
        "input": "continue", "stream": bool(streaming), "previous_response_id": "resp_prior",
    }))
    response = await asyncio.wait_for(adapter._handle_responses(request), timeout=5)
    if streaming == "disconnect_after_result":
        created = _events(response)[0][1]["response"]
        terminal = adapter._response_store.get(created["id"])["response"]
        assert terminal["hermes"]["completed"] is False
        assert terminal["hermes"]["error"] == ERROR
    else:
        terminal = _events(response)[-1][1]["response"] if streaming else json.loads(response.text)
    assert terminal["status"] == "incomplete"
    stored = adapter._response_store.get(terminal["id"])
    expected = native.results[0]["messages"]
    assert stored["conversation_history"] == expected
    assert any(message.get("role") == "tool" for message in expected)
    assert stored["session_id"] == ("native-child" if compressed else "native-parent")
    if compressed:
        assert native.results[0]["_compressed"] is True
        assert expected[0] == summary[0]
        assert not any(message in expected for message in prior)
    followup = SimpleNamespace(headers={}, query={}, json=AsyncMock(return_value={
        "input": "follow up", "previous_response_id": terminal["id"],
    }))
    await asyncio.wait_for(adapter._handle_responses(followup), timeout=5)
    assert native.inputs[1]["history"] == expected
    assert native.inputs[1]["session_id"] == stored["session_id"]
    _assert_incomplete(native)


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
