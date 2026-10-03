"""Offline wire checks: the dummy child never drives a model or reads auth."""
import json
import io
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from agent.transports import codex_app_server as cas
from agent.transports.codex_app_server_session import CodexAppServerSession


@pytest.fixture
def dummy_codex(tmp_path):
    script = tmp_path / "dummy-codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' in msg and 'method' in msg:\n"
        "        print(json.dumps({'id': msg['id'], 'result': {'ok': True}}), flush=True)\n"
    )
    script.chmod(0o700)
    return str(script)


def test_serialized_rpc_limit_counts_envelope_escapes_and_newline(dummy_codex):
    params = {"text": "é" * 15}
    frame = {"id": 1, "method": "turn/start", "params": params}
    size = len((json.dumps(frame) + "\n").encode("utf-8"))
    with cas.CodexAppServerClient(codex_bin=dummy_codex, max_rpc_bytes=size - 1) as client:
        with pytest.raises(ValueError, match="RPC.*bytes"):
            client.request("turn/start", params)
        assert client._pending == {}
        assert client.request("ping") == {"ok": True}
    with cas.CodexAppServerClient(codex_bin=dummy_codex, max_rpc_bytes=size) as client:
        assert client.request("turn/start", params) == {"ok": True}


def test_trial_wire_rejects_oversized_frame_without_dispatch(dummy_codex):
    with cas.CodexAppServerClient(codex_bin=dummy_codex, subscription_only_trial=True) as client:
        with pytest.raises(ValueError, match="RPC.*bytes"):
            client.request("turn/start", {"text": "x" * (1024 * 1024)})
        assert client._pending == {}


def test_trial_spawn_strips_provider_credentials(monkeypatch):
    captured = {}

    class DummyProcess:
        stdin = stdout = stderr = None
        def __init__(self, cmd, **kwargs):
            captured.update(kwargs["env"])
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, timeout):
            return 0

    monkeypatch.setattr(subprocess, "Popen", DummyProcess)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-dummy-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://offline.invalid")
    with cas.CodexAppServerClient(subscription_only_trial=True):
        pass
    assert "OPENAI_API_KEY" not in captured
    assert "OPENAI_BASE_URL" not in captured


@pytest.mark.parametrize("override", [{"env": {"OPENAI_API_KEY": "offline-dummy"}},
                                       {"extra_args": ["-c", 'model_provider="custom"']}])
def test_trial_spawn_rejects_unqualified_overrides_before_child(monkeypatch, override):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kw: spawned.append(args))
    with pytest.raises(ValueError, match="trial.*override"):
        cas.CodexAppServerClient(subscription_only_trial=True, **override)
    assert spawned == []


def test_send_failure_cleans_pending_request(dummy_codex):
    client = cas.CodexAppServerClient(codex_bin=dummy_codex)
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.request("turn/start", {"text": "dummy"})
    assert client._pending == {}


@pytest.mark.linux_only
@pytest.mark.parametrize("operation", ["notify", "respond", "respond_error"])
def test_control_send_deadline_retires_blocked_child(tmp_path, operation):
    dummy = tmp_path / "nonreading-control-child"
    dummy.write_text(f"#!{sys.executable}\nimport threading\nthreading.Event().wait(30)\n")
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy))
    fd = client._proc.stdin.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            os.write(fd, b"x" * 4096)
    except BlockingIOError:
        pass
    finally:
        os.set_blocking(fd, True)
    errors = []

    def send():
        try:
            if operation == "notify":
                client.notify("initialized", timeout=0.05)
            elif operation == "respond":
                client.respond("approval", {"decision": "decline"}, timeout=0.05)
            else:
                client.respond_error("unknown", -32601, "unsupported", timeout=0.05)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=send, daemon=True)
    try:
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        assert client._closed
        client._proc.wait(timeout=2)
    finally:
        client.close(timeout=0)


@pytest.mark.parametrize("operation", ["notify", "respond", "respond_error"])
def test_control_send_deadline_covers_serialization(dummy_codex, monkeypatch, operation):
    client = cas.CodexAppServerClient(codex_bin=dummy_codex)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_dumps = cas.json.dumps

    def dumps(obj):
        entered.set()
        try:
            release.wait(2)
            return original_dumps(obj)
        finally:
            finished.set()

    monkeypatch.setattr(cas.json, "dumps", dumps)
    try:
        with pytest.raises(TimeoutError):
            if operation == "notify":
                client.notify("initialized", timeout=0.05)
            elif operation == "respond":
                client.respond(42, {"ok": True}, timeout=0.05)
            else:
                client.respond_error(42, -32601, "unsupported", timeout=0.05)
        assert entered.is_set() and client._closed
        client._proc.wait(timeout=2)
    finally:
        release.set()
        assert finished.wait(2)
        client.close(timeout=0)


@pytest.mark.linux_only
@pytest.mark.parametrize("operation", ["run_turn", "compact_thread", "initialize"])
def test_default_control_send_bound_retires_session_and_handshake(tmp_path, operation):
    dummy = tmp_path / "nonreading-session-child"
    dummy.write_text(f"#!{sys.executable}\nimport threading\nthreading.Event().wait(30)\n")
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy))
    fd = client._proc.stdin.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            os.write(fd, b"x" * 4096)
    except BlockingIOError:
        pass
    finally:
        os.set_blocking(fd, True)
    entered = threading.Event()
    original_send = client._send

    def send(obj, **kwargs):
        entered.set()
        return original_send(obj, **kwargs)

    client._send = send
    def request(method, *args, **kwargs):
        if method == "thread/read":
            return {"thread": {"id": "dummy-thread", "status": {"type": "idle"}, "turns": []}}
        if method == "thread/compact/start":
            return {}
        return {"turn": {"id": "new-turn"}}

    client.request = request
    client._server_requests.put({"id": 42, "method": "item/permissions/requestApproval",
        "params": {"threadId": "dummy-thread", "turnId": "new-turn"}})
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "dummy-thread"
    results = []
    errors = []

    def run():
        try:
            if operation == "initialize":
                client.initialize()
            else:
                kwargs = {"turn_timeout": 0.1, "notification_poll_timeout": 0.001}
                results.append(session.run_turn("dummy", **kwargs) if operation == "run_turn"
                    else session.compact_thread(**kwargs))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
        assert entered.wait(2)
        session.request_interrupt()
        worker.join(7)
        assert not worker.is_alive()
        assert client._closed
        client._proc.wait(timeout=2)
        if operation == "initialize":
            assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
            assert not client._initialized
        else:
            assert errors == [] and len(results) == 1
            assert results[0].should_retire and not results[0].completed
            assert results[0].final_text == ""
            assert session._closed and session._active_turn_id is None
            assert not session._interrupt_event.is_set()
    finally:
        client.close(timeout=0)
        session.close()


def test_empty_compaction_ack_without_lifecycle_times_out_and_retires(dummy_codex):
    client = cas.CodexAppServerClient(codex_bin=dummy_codex)
    client.request = lambda method, *args, **kwargs: (
        {"thread": {"id": "dummy-thread", "status": {"type": "idle"}, "turns": []}}
        if method == "thread/read" else {})
    client._server_requests.put({"id": 42, "method": "item/permissions/requestApproval"})
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "dummy-thread"
    try:
        result = session.compact_thread(turn_timeout=0.01)
        assert not result.completed and result.final_text == ""
        assert result.should_retire and "timed out" in result.error
        assert client._closed and session._closed
        client._proc.wait(timeout=2)
        assert not client.is_alive()
        assert session._active_turn_id is None
        assert not session._interrupt_event.is_set()
        assert client._server_requests.qsize() == 0
    finally:
        client.close(timeout=0)
        session.close()


@pytest.mark.linux_only
@pytest.mark.parametrize("buffered", [False, True])
def test_interrupt_rpc_timeout_covers_blocked_stdin(tmp_path, buffered):
    """A real full child pipe must not outlive the RPC's send/reply deadline."""
    dummy = tmp_path / "nonreading-child"
    dummy.write_text(
        f"#!{sys.executable}\nimport threading\nthreading.Event().wait(30)\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy), subscription_only_trial=True)
    fd = client._proc.stdin.fileno()
    os.set_blocking(fd, False)
    filled = 0
    try:
        while True:
            filled += os.write(fd, b"x" * 4096)
    except BlockingIOError:
        pass
    finally:
        os.set_blocking(fd, True)
    if buffered:
        client._proc.stdin = io.BufferedWriter(client._proc.stdin, buffer_size=1)
    outcome = []

    def interrupt():
        try:
            client.request("turn/interrupt", {"threadId": "dummy", "turnId": "dummy"}, timeout=0.05)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=interrupt, daemon=True)
    start = time.monotonic()
    worker.start()
    worker.join(0.4)
    exceeded_bound = worker.is_alive()
    elapsed = time.monotonic() - start
    # Kill first even on RED, so the baseline's BufferedWriter.close cannot hang.
    if exceeded_bound:
        client._proc.kill()
        client._proc.wait(timeout=2)
    worker.join(2)
    try:
        print({"filled_pipe_bytes": filled, "buffered": buffered, "elapsed_s": elapsed,
               "outcome": [type(exc).__name__ for exc in outcome]})
        assert not exceeded_bound, "RPC deadline does not bound blocked stdin write/flush"
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
        client._proc.wait(timeout=2)
        assert client._closed and not client.is_alive()
        assert client._pending == {}
        with pytest.raises(RuntimeError, match="closed"):
            client.request("ping")
    finally:
        client.close(timeout=0.2)


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_request_deadline_survives_descendant_holding_stdin(tmp_path):
    """Killing the direct child alone need not release a BufferedWriter lock."""
    pid_path = tmp_path / "descendant.pid"
    dummy = tmp_path / "pipe-holding-child"
    dummy.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, threading\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(pid_path)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "threading.Event().wait(30)\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy), subscription_only_trial=True)
    ready_deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < ready_deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    descendant_pid = int(pid_path.read_text(encoding="utf-8"))
    fd = client._proc.stdin.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            os.write(fd, b"x" * 4096)
    except BlockingIOError:
        pass
    finally:
        os.set_blocking(fd, True)
    client._proc.stdin = io.BufferedWriter(client._proc.stdin, buffer_size=1)
    outcome = []

    def request():
        try:
            client.request("turn/interrupt", timeout=0.05)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    worker.join(0.4)
    exceeded_bound = worker.is_alive()
    # Always release the inherited read FD; local retirement does not certify
    # descendant effect cancellation, and the test must not leave this dummy.
    os.kill(descendant_pid, signal.SIGKILL)
    if client.is_alive():
        client._proc.kill()
    client._proc.wait(timeout=2)
    worker.join(2)
    try:
        assert not exceeded_bound, "retirement blocked on descendant-held stdin"
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
        assert client._closed and not client.is_alive()
        assert client._pending == {}
    finally:
        client.close(timeout=0.2)


def test_timed_out_queued_writer_cannot_dispatch_late(dummy_codex):
    client = cas.CodexAppServerClient(codex_bin=dummy_codex)
    entered = threading.Event()
    finished = threading.Event()
    writes = []
    stream = client._proc.stdin

    class RecordingStdin:
        @property
        def closed(self):
            return stream.closed
        def write(self, data):
            writes.append(bytes(data))
            return stream.write(data)
        def flush(self):
            return stream.flush()
        def close(self):
            return stream.close()

    client._proc.stdin = RecordingStdin()
    original_send = client._send

    def send(*args, **kwargs):
        entered.set()
        try:
            return original_send(*args, **kwargs)
        finally:
            finished.set()

    client._send = send
    outcome = []

    def request():
        try:
            client.request("turn/start", timeout=0.05)
        except Exception as exc:
            outcome.append(exc)

    client._write_lock.acquire()
    try:
        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        assert entered.wait(2)
        worker.join(2)
        assert not worker.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
        assert client._closed and client._pending == {}
        client._proc.wait(timeout=2)
    finally:
        client._write_lock.release()
    assert finished.wait(2)
    assert writes == []
    client.close()


def test_concurrent_request_notify_response_frames_remain_whole(tmp_path):
    frames_path = tmp_path / "frames.jsonl"
    dummy = tmp_path / "frame-recording-child"
    dummy.write_text(
        f"#!{sys.executable}\nimport sys, json\n"
        f"with open({str(frames_path)!r}, 'w', encoding='utf-8') as frames:\n"
        "    for line in sys.stdin:\n"
        "        msg = json.loads(line)\n"
        "        frames.write(json.dumps(msg) + '\\n'); frames.flush()\n"
        "        if 'id' in msg and 'method' in msg:\n"
        "            print(json.dumps({'id': msg['id'], 'result': {'tag': msg['params']['tag']}}), flush=True)\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    barrier = threading.Barrier(8)
    results = {}
    errors = []
    payload = "x" * (256 * 1024)
    with cas.CodexAppServerClient(codex_bin=str(dummy), subscription_only_trial=True) as client:
        def send(index):
            try:
                barrier.wait(timeout=2)
                params = {"tag": index, "text": payload}
                if index < 4:
                    results[index] = client.request("ping", params, timeout=5)
                elif index < 6:
                    client.notify("notice", params)
                else:
                    client.respond(f"server-{index}", params)
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=send, args=(index,), daemon=True) for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(6)
        assert not errors
        assert not any(worker.is_alive() for worker in workers)
        assert results == {index: {"tag": index} for index in range(4)}
        # A final reply proves the peer consumed every preceding frame.
        assert client.request("ping", {"tag": "drain"}) == {"tag": "drain"}
        assert client._pending == {}
    frames = [json.loads(line) for line in frames_path.read_text(encoding="utf-8").splitlines()]
    assert len(frames) == 9
    assert len({frame["id"] for frame in frames if frame.get("method") == "ping"}) == 5
    assert sorted((frame.get("params") or frame.get("result"))["tag"] for frame in frames[:-1]) == list(range(8))
    assert all((frame.get("params") or frame.get("result"))["text"] == payload for frame in frames[:-1])


@pytest.mark.parametrize("before_ack", [False, True])
@pytest.mark.parametrize("compaction_event", ["item_pair", "legacy_notification"])
def test_supported_empty_ack_compaction_preserves_thread_over_real_wire(tmp_path, before_ack, compaction_event):
    """The inert child speaks protocol lifecycle, including delayed prior events."""
    dummy = tmp_path / "compact-protocol-child"
    dummy.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def emit(value): print(json.dumps(value), flush=True)\n"
        "def note(method, turn, item=None):\n"
        "    params = {'threadId': 'canonical', 'turnId': turn}\n"
        "    if method.startswith('turn/'): params['turn'] = {'id': turn, 'status': 'completed' if method == 'turn/completed' else 'inProgress'}\n"
        "    if item: params['item'] = item\n"
        "    emit({'method': method, 'params': params})\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg or 'method' not in msg: continue\n"
        "    method = msg['method']\n"
        "    if method == 'thread/read':\n"
        "        emit({'id': msg['id'], 'result': {'thread': {'id': 'canonical', 'status': {'type': 'idle'}, 'turns': [{'id': 'prior', 'status': 'completed'}]}}})\n"
        "    elif method == 'thread/compact/start':\n"
        f"        if not {before_ack!r}: emit({{'id': msg['id'], 'result': {{}}}})\n"
        "        note('turn/started', 'prior')\n"
        "        note('turn/completed', 'prior')\n"
        "        emit({'method': 'turn/started', 'params': {'threadId': 'foreign', 'turn': {'id': 'foreign-turn'}}})\n"
        "        note('turn/completed', 'compact')\n"
        "        note('turn/started', 'compact')\n" +
        ("        note('item/started', 'compact', {'type': 'contextCompaction', 'id': 'compact-item'})\n"
         "        note('item/completed', 'compact', {'type': 'contextCompaction', 'id': 'compact-item'})\n"
         if compaction_event == "item_pair" else
         "        emit({'method': 'thread/compacted', 'params': {'threadId': 'canonical', 'turnId': 'compact'}})\n") +
        "        note('turn/completed', 'compact')\n"
        f"        if {before_ack!r}: emit({{'id': msg['id'], 'result': {{}}}})\n"
        "    elif method == 'turn/start':\n"
        "        emit({'id': msg['id'], 'result': {'turn': {'id': 'next'}}})\n"
        "        note('turn/completed', 'next')\n"
        "    else: emit({'id': msg['id'], 'result': {}})\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy))
    session = CodexAppServerSession()
    session._client, session._thread_id = client, "canonical"
    try:
        compact = session.compact_thread(turn_timeout=1, notification_poll_timeout=0.001)
        assert compact.completed and compact.compacted and compact.turn_id == "compact"
        assert session._thread_id == "canonical" and not session._closed and client.is_alive()
        next_turn = session.run_turn("continue", turn_timeout=1, notification_poll_timeout=0.001)
        assert next_turn.completed and next_turn.thread_id == "canonical"
    finally:
        session.close()


@pytest.mark.parametrize("invalid_event", [
    {"method": "thread/compacted", "params": {"threadId": "foreign", "turnId": "compact"}},
    {"method": "thread/compacted", "params": {"threadId": "canonical", "turnId": "prior"}},
    {"method": "thread/compacted", "params": {}},
    {"method": "thread/compacted", "params": {"threadId": "canonical"}},
    {"method": "thread/compacted", "params": {"turnId": "compact"}},
    {"method": "thread/compacted", "params": {"thread_id": "canonical", "turn_id": "compact"}},
    {"method": "thread/compacted", "params": {
        "threadId": "canonical", "turnId": "compact", "turn": {"id": "prior"}}},
    {"method": "thread/compacted", "params": {
        "threadId": "canonical", "turnId": "compact", "thread_id": "foreign"}},
    {"method": "thread/tokenUsage/updated", "params": {"threadId": "canonical", "turnId": "compact"}},
    None,
], ids=["foreign", "prior", "unscoped", "missing-turn", "missing-thread", "unsupported-aliases",
        "conflicting-turn", "conflicting-thread", "noncompaction", "terminal-only"])
def test_legacy_compaction_requires_canonical_bound_completion_over_wire(tmp_path, invalid_event):
    """Negative peer frames cannot certify compaction or preserve an uncertain child."""
    dummy = tmp_path / "legacy-negative-child"
    dummy.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"event = {invalid_event!r}\n"
        "def emit(frame): print(json.dumps(frame), flush=True)\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg or 'method' not in msg: continue\n"
        "    if msg['method'] == 'thread/read':\n"
        "        emit({'id': msg['id'], 'result': {'thread': {'id': 'canonical', 'status': {'type': 'idle'}, 'turns': [{'id': 'prior', 'status': 'completed'}]}}})\n"
        "    elif msg['method'] == 'thread/compact/start':\n"
        "        emit({'id': msg['id'], 'result': {}})\n"
        "        emit({'method': 'turn/started', 'params': {'threadId': 'canonical', 'turn': {'id': 'compact', 'status': 'inProgress'}}})\n"
        "        if event is not None: emit(event)\n"
        "        emit({'method': 'turn/completed', 'params': {'threadId': 'canonical', 'turn': {'id': 'compact', 'status': 'completed', 'error': None}}})\n"
        "    else: emit({'id': msg['id'], 'result': {}})\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy))
    session = CodexAppServerSession()
    session._client, session._thread_id = client, "canonical"
    try:
        result = session.compact_thread(turn_timeout=1, notification_poll_timeout=0.001)
        assert not result.completed and result.final_text == "" and result.error
        assert result.should_retire and session._closed and client._closed
        assert result.thread_id == "canonical" and result.turn_id == "compact"
    finally:
        session.close()


@pytest.mark.parametrize("timing", ["before-start", "prequeued"])
def test_legacy_compaction_preboundary_notification_cannot_certify_over_wire(tmp_path, timing):
    dummy = tmp_path / "legacy-boundary-child"
    dummy.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"timing = {timing!r}\n"
        "def emit(frame): print(json.dumps(frame), flush=True)\n"
        "def start(): emit({'method': 'turn/started', 'params': {'threadId': 'canonical', 'turn': {'id': 'compact', 'status': 'inProgress'}}})\n"
        "def compacted(): emit({'method': 'thread/compacted', 'params': {'threadId': 'canonical', 'turnId': 'compact'}})\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg or 'method' not in msg: continue\n"
        "    if msg['method'] == 'thread/read':\n"
        "        if timing == 'prequeued': start(); compacted()\n"
        "        emit({'id': msg['id'], 'result': {'thread': {'id': 'canonical', 'status': {'type': 'idle'}, 'turns': []}}})\n"
        "    elif msg['method'] == 'thread/compact/start':\n"
        "        emit({'id': msg['id'], 'result': {}})\n"
        "        if timing == 'before-start': compacted()\n"
        "        start()\n"
        "        emit({'method': 'turn/completed', 'params': {'threadId': 'canonical', 'turn': {'id': 'compact', 'status': 'completed', 'error': None}}})\n"
        "    else: emit({'id': msg['id'], 'result': {}})\n",
        encoding="utf-8",
    )
    dummy.chmod(0o700)
    client = cas.CodexAppServerClient(codex_bin=str(dummy))
    session = CodexAppServerSession()
    session._client, session._thread_id = client, "canonical"
    try:
        result = session.compact_thread(turn_timeout=0.1, notification_poll_timeout=0.001)
        assert not result.completed and result.should_retire and result.error
        assert session._closed and client._closed and result.final_text == ""
    finally:
        session.close()
