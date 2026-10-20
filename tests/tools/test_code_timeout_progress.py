"""Real child/socket regressions; barrier dispatchers are explicit test harnesses."""
import contextvars
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from tools import code_execution_tool as sandbox
from tools.interrupt import set_interrupt
from tools.thread_context import propagate_context_to_thread


@pytest.fixture(autouse=True)
def neutral_local(monkeypatch):
    # Optional LSP is enabled by default; neutral tests explicitly disable it
    # in their temporary home so file RPC never starts an unrelated service.
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("lsp:\n  enabled: false\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(sandbox, "_load_config", lambda: {"timeout": 0.35, "max_tool_calls": 10})
    # Only the script-approval boundary is bypassed; child/RPC/file I/O is real.
    monkeypatch.setattr("tools.approval.check_execute_code_guard", lambda *a, **k: {"approved": True})
    import model_tools  # Warm real dispatch imports in the neutral home before short budgets.
    from agent.lsp import get_service
    assert get_service() is None
    set_interrupt(False)
    yield
    set_interrupt(False)


def run(code, tools=None):
    return json.loads(sandbox.execute_code(code, task_id="sandbox-progress-test", enabled_tools=tools))


def test_print_before_timeout_is_retained():
    result = run('import time\nprint("PROGRESS_SENTINEL")\ntime.sleep(30)')
    assert result["status"] == "timeout", result
    assert "PROGRESS_SENTINEL" in result["output"]
    assert result["execution"]["phase"] == "timeout"
    assert result["execution"]["outer_timeout_seconds"] == 0.35
    assert result["execution"]["rpc_timeout_seconds"] == 300
    assert result["cleanup"]["status"] == "complete"


def test_stop_retains_unflushed_output():
    tid = threading.get_ident()
    timer = threading.Timer(0.2, lambda: set_interrupt(True, tid, reason="test Stop"))
    timer.start()
    try:
        result = run('import time\nprint("STOP_SENTINEL")\ntime.sleep(30)')
        assert result["status"] == "interrupted", result
        assert "STOP_SENTINEL" in result["output"]
        assert result["cleanup"]["status"] == "complete"
    finally:
        timer.join()


def test_output_cap_and_secret_redaction_survive_timeout(monkeypatch):
    secret = "sk-" + "aB3cD4eF5gH6iJ7kL8mN9pQ0rS1tU2vW3xY4zZ5aB6cD7eF8"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    code = f'import os,time\nassert "OPENAI_API_KEY" not in os.environ\nprint("HEAD")\nprint({secret!r})\nprint("x" * 60000)\nprint("TAIL")\ntime.sleep(30)'
    result = run(code)
    assert result["status"] == "timeout", result
    assert result["stdout_truncated"]
    assert result["stdout_bytes_captured"] <= sandbox.MAX_STDOUT_BYTES
    assert "HEAD" in result["output"] and "TAIL" in result["output"]
    assert secret not in json.dumps(result)


def test_real_rpc_artifact_and_partial_serial_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_load_config", lambda: {"timeout": 5, "max_tool_calls": 10})
    artifact = tmp_path / "created.txt"
    code = f'from hermes_tools import write_file,read_file\nimport time\nprint("BEFORE_RPC")\nwrite_file({str(artifact)!r}, "artifact sentinel")\nprint(read_file({str(artifact)!r})["content"])\ntime.sleep(30)'
    result = run(code, ["write_file", "read_file"])
    assert result["status"] == "timeout", result
    assert artifact.read_text() == "artifact sentinel"
    assert "BEFORE_RPC" in result["output"]
    assert "artifact sentinel" in result["output"]
    assert result["tool_calls_made"] == 2
    assert result["rpc"]["completed"] == 2
    assert result["rpc"]["active"] == 0
    assert result["rpc"]["calls"][0]["artifact_operation"] == "write_file"
    assert "args_preview" not in json.dumps(result["rpc"])


def socket_server(monkeypatch, dispatch):
    import model_tools
    monkeypatch.setattr(model_tools, "handle_function_call", dispatch)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Abstract namespace avoids filesystem socket-path lifetime in direct tests.
    address = "\0sandbox-progress-" + str(time.monotonic_ns())
    server.bind(address)
    server.listen(1)
    stop = threading.Event()
    log, counter = [], [0]
    thread = threading.Thread(target=propagate_context_to_thread(sandbox._rpc_server_loop), args=(server, "owned-task", log, counter, 10, sandbox.SANDBOX_ALLOWED_TOOLS, stop, "test-token"), daemon=True)
    thread.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(address)
    return server, client, stop, thread, log, counter


def send(client, tool="read_file", args=None, token="test-token"):
    client.sendall((json.dumps({"tool": tool, "args": args or {}, "token": token}) + "\n").encode())


def test_idle_connected_rpc_stop_settles(monkeypatch):
    server, client, stop, thread, _, _ = socket_server(monkeypatch, lambda *a, **k: "{}")
    try:
        send(client)
        assert client.recv(65536)
        stop.set()
        thread.join(0.5)
        assert not thread.is_alive(), "connected recv ignored Stop"
    finally:
        stop.set()
        client.close()
        server.close()
        thread.join(1)


def test_stop_denies_buffered_second_call_and_keeps_scope(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    marker = contextvars.ContextVar("sandbox_test_marker", default="missing")
    token = marker.set("caller scope")
    observed = []

    def barrier(name, args, task_id=None):
        observed.append((name, task_id, marker.get()))
        entered.set()
        assert release.wait(5)
        return "{}"

    server, client, stop, thread, log, counter = socket_server(monkeypatch, barrier)
    marker.reset(token)
    try:
        # Both requests are in one recv buffer while the first blocks.
        request = json.dumps({"tool": "read_file", "args": {}, "token": "test-token"}) + "\n"
        client.sendall((request * 2).encode())
        assert entered.wait(2)
        stop.set()
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        assert observed == [("read_file", "owned-task", "caller scope")]
        assert counter == [1]
        assert all("args_preview" not in item for item in log)
    finally:
        release.set()
        stop.set()
        client.close()
        server.close()
        thread.join(1)


def test_active_tool_timeout_reports_uncertain_cleanup_and_late_settlement(monkeypatch):
    import model_tools
    entered, release = threading.Event(), threading.Event()
    observed = []

    def barrier(name, args, task_id=None):
        observed.append((name, task_id, threading.current_thread()))
        if len(observed) == 2:
            entered.set()
            assert release.wait(8)
        return json.dumps({"harness": "completed"})

    monkeypatch.setattr(model_tools, "handle_function_call", barrier)
    try:
        result = run('from hermes_tools import read_file,terminal\nprint("START")\nread_file("test-only")\nprint("FIRST_DONE")\nterminal("test-only", timeout=2)\nprint("UNREACHABLE")', ["read_file", "terminal"])
        assert entered.is_set(), result
        assert result["status"] == "timeout"
        assert "FIRST_DONE" in result["output"]
        assert "UNREACHABLE" not in result["output"]
        assert result["tool_calls_made"] == 2  # admitted, including the active call
        assert result["rpc"]["completed"] == 1
        assert result["rpc"]["active"] == 1
        assert result["rpc"]["calls"][1]["requested_tool_timeout_seconds"] == 2
        assert result["cleanup"]["status"] == "incomplete"
        assert result["cleanup"]["rpc_thread_alive"]
        thread = observed[-1][2]
        frozen = json.dumps(result, sort_keys=True)
        assert thread.sandbox_execution.snapshot()["active"] == 1
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert thread.sandbox_execution.snapshot()["active"] == 0
        assert json.dumps(result, sort_keys=True) == frozen
        assert result["status"] == "timeout"
    finally:
        release.set()
        for _, _, thread in observed:
            thread.join(2)


def test_auth_whitelist_and_terminal_parameter_guards(monkeypatch):
    observed = []
    server, client, stop, thread, log, counter = socket_server(monkeypatch, lambda name, args, **k: observed.append((name, args)) or "{}")
    try:
        send(client, token="bad-token")
        assert "Unauthorized" in client.recv(65536).decode()
        send(client, tool="delegate_task")
        assert "not available" in client.recv(65536).decode()
        send(client, tool="terminal", args={"command": "harness-only", "timeout": 2, "background": True, "pty": True, "notify": True, "notify_on_complete": True, "watch_patterns": ["private sentinel"]})
        assert client.recv(65536)
        assert observed == [("terminal", {"command": "harness-only", "timeout": 2})]
        assert counter == [1]
        assert "harness-only" not in json.dumps(log)
        assert "private sentinel" not in json.dumps(log)
    finally:
        stop.set()
        client.close()
        server.close()
        thread.join(1)


def test_real_child_launch_flushes_progress_before_kill(tmp_path):
    import sys
    script = tmp_path / "progress.py"
    script.write_text('import time\nprint("LIVE_PROGRESS")\ntime.sleep(30)')
    proc = sandbox._start_sandbox_process(sys.executable, str(script), str(tmp_path), sandbox._scrub_child_env(os.environ))
    seen = []
    reader = threading.Thread(target=lambda: seen.append(proc.stdout.readline()), daemon=True)
    reader.start()
    try:
        reader.join(1)
        assert seen == [b"LIVE_PROGRESS\n"], "print remained buffered while child was alive"
    finally:
        sandbox._kill_process_group(proc, escalate=True)
        proc.wait(timeout=2)
        reader.join(2)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode is not None


def test_execution_owner_freezes_partial_metadata_and_denies_stop_admission():
    owner = sandbox._SandboxExecution(threading.Event(), [], [0], 0.5)
    first = owner.begin_call("write_file", {})
    owner.finish_call(first, '{"status":"success"}')
    second = owner.begin_call("terminal", {"command": "private", "timeout": 2})
    before = owner.snapshot()
    owner.stop()
    assert owner.begin_call("read_file", {}) is None
    assert before["admitted"] == 2 and before["completed"] == 1 and before["active"] == 1
    assert before["calls"][0]["artifact_operation"] == "write_file"
    assert before["calls"][1]["outer_remaining_seconds"] <= 0.5
    assert "private" not in json.dumps(before)
    owner.finish_call(second, '{}')
    assert owner.snapshot()["active"] == 0
    assert before["active"] == 1


class TempFileBackend:
    """Test-only backend: real shell/file IPC in a temp home, no service."""
    def __init__(self, root):
        self.root = str(root)
        self.commands = []

    def get_temp_dir(self):
        return self.root

    def execute(self, command, cwd=None, timeout=None):
        import subprocess
        self.commands.append(command)
        env = sandbox._scrub_child_env(os.environ)
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        from tools.interrupt import is_interrupted
        deadline = time.monotonic() + timeout
        while True:
            interrupted = is_interrupted()
            if interrupted or time.monotonic() >= deadline:
                sandbox._kill_process_group(proc, escalate=True)
                output, _ = proc.communicate(timeout=2)
                return {"output": output.decode(), "returncode": 130 if interrupted else 124}
            try:
                output, _ = proc.communicate(timeout=min(0.02, deadline - time.monotonic()))
                return {"output": output.decode(), "returncode": proc.returncode}
            except subprocess.TimeoutExpired:
                continue


def test_file_rpc_real_progress_artifact_timeout(tmp_path, monkeypatch):
    backend = TempFileBackend(tmp_path)
    artifact = tmp_path / "remote-artifact.txt"
    monkeypatch.setattr(sandbox, "_get_or_create_env", lambda task: (backend, "test-file-backend"))
    monkeypatch.setattr(sandbox, "_load_config", lambda: {"timeout": 5, "max_tool_calls": 10})
    result = json.loads(sandbox._execute_remote(f'from hermes_tools import write_file\nimport time\nprint("REMOTE_START")\nwrite_file({str(artifact)!r}, "real artifact")\nprint("REMOTE_RPC_DONE")\ntime.sleep(30)', "file-rpc-test", ["write_file"]))
    assert result["status"] == "timeout", result
    assert "REMOTE_RPC_DONE" in result["output"], result
    assert artifact.exists(), result
    assert artifact.read_text() == "real artifact"
    assert "REMOTE_START" in result["output"]
    assert "REMOTE_RPC_DONE" in result["output"]
    assert result["rpc"]["completed"] == 1
    assert result["cleanup"]["status"] == "complete"


def test_owner_interrupts_only_owned_thread_and_retains_supervision():
    from tools.interrupt import is_interrupted
    owner = sandbox._SandboxExecution(threading.Event(), [], [0], 1)
    entered, release = threading.Event(), threading.Event()
    foreign_entered = threading.Event()
    observations = []

    def owned_barrier():
        owner.attach_worker()
        entry = owner.begin_call("read_file", {})
        entered.set()
        try:
            assert release.wait(5)
            observations.append(("owned", is_interrupted()))
            owner.finish_call(entry, '{}')
        finally:
            owner.finish_worker()

    def foreign_barrier():
        foreign_entered.set()
        assert release.wait(5)
        observations.append(("foreign", is_interrupted()))

    owned = threading.Thread(target=owned_barrier, daemon=True)
    foreign = threading.Thread(target=foreign_barrier, daemon=True)
    owned.start()
    foreign.start()
    try:
        assert entered.wait(1) and foreign_entered.wait(1)
        owner.stop()
        owned.join(0.01)
        metadata = owner.result_metadata("timeout", owned)
        assert metadata["cleanup"]["status"] == "incomplete"
        assert owned.sandbox_execution is owner
        assert not owner.settled.is_set()
        assert owner.begin_call("terminal", {}) is None
        release.set()
        owned.join(1)
        foreign.join(1)
        assert sorted(observations) == [("foreign", False), ("owned", True)]
        assert owner.settled.is_set()
        assert metadata["rpc"]["active"] == 1
        assert owner.snapshot()["active"] == 0
    finally:
        release.set()
        owned.join(1)
        foreign.join(1)


def test_file_rpc_barrier_retains_owner_without_late_backend_writes(tmp_path, monkeypatch):
    import model_tools
    backend = TempFileBackend(tmp_path)
    # This witness targets an in-flight second RPC, not interpreter/file-IPC
    # startup. Keep a finite test-only outer budget and a larger requested tool
    # timeout; all ownership and late-write assertions below remain intact.
    outer_budget = 3.0
    requested_tool_timeout = 10
    monkeypatch.setattr(sandbox, "_load_config", lambda: {"timeout": outer_budget, "max_tool_calls": 10})
    entered, release = threading.Event(), threading.Event()
    observed = []
    marker = contextvars.ContextVar("file_rpc_scope", default="missing")
    token = marker.set("caller scope")

    def barrier(name, args, task_id=None):
        observed.append((task_id, marker.get(), threading.current_thread()))
        if len(observed) == 2:
            entered.set()
            assert release.wait(5)
        return '{"harness":"returned"}'

    monkeypatch.setattr(model_tools, "handle_function_call", barrier)
    monkeypatch.setattr(sandbox, "_get_or_create_env", lambda task: (backend, "test-file-backend"))
    try:
        result = json.loads(sandbox._execute_remote(f'from hermes_tools import read_file,terminal\nprint("FILE_START")\nread_file("harness-only")\nprint("FIRST_RETURNED")\nterminal("harness-only", timeout={requested_tool_timeout})\nprint("UNREACHABLE")', "owned-file-task", ["read_file", "terminal"]))
        assert entered.is_set(), result
        assert result["status"] == "timeout"
        assert "FIRST_RETURNED" in result["output"]
        assert "UNREACHABLE" not in result["output"]
        assert result["rpc"]["admitted"] == 2
        assert result["rpc"]["completed"] == 1
        assert result["rpc"]["active"] == 1
        assert result["rpc"]["calls"][1]["requested_tool_timeout_seconds"] == requested_tool_timeout
        assert result["rpc"]["calls"][1]["outer_remaining_seconds"] < outer_budget < requested_tool_timeout
        assert result["cleanup"]["status"] == "incomplete"
        assert not result["cleanup"]["staging_removed"]
        staging = Path(result["cleanup"]["retained_staging_directory"])
        assert staging.is_dir()
        worker = observed[-1][2]
        assert observed[0][:2] == observed[1][:2] == ("owned-file-task", "caller scope")
        assert worker.sandbox_execution.snapshot()["active"] == 1
        commands_before = list(backend.commands)
        frozen = json.dumps(result, sort_keys=True)
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert worker.sandbox_execution.settled.is_set()
        assert worker.sandbox_execution.snapshot()["active"] == 0
        assert backend.commands == commands_before
        assert not list((staging / "rpc").glob("res_000002*"))
        assert json.dumps(result, sort_keys=True) == frozen
    finally:
        marker.reset(token)
        release.set()
        for _, _, worker in observed:
            worker.join(1)


def test_file_backend_output_cap_and_secret_sentinels(tmp_path, monkeypatch):
    backend = TempFileBackend(tmp_path)
    monkeypatch.setattr(sandbox, "_get_or_create_env", lambda task: (backend, "test-file-backend"))
    secret = "sk-" + "aB3cD4eF5gH6iJ7kL8mN9pQ0rS1tU2vW3xY4zZ5aB6cD7eF8"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    code = f'import os,time\nassert "OPENAI_API_KEY" not in os.environ\nprint("HEAD")\nprint({secret!r})\nprint("x" * 60000)\nprint("TAIL")\ntime.sleep(30)'
    result = json.loads(sandbox._execute_remote(code, "file-output-task", ["read_file"]))
    assert result["status"] == "timeout", result
    assert result["stdout_truncated"]
    assert result["stdout_bytes_captured"] == sandbox.MAX_STDOUT_BYTES
    assert result["stdout_bytes_total"] == result["stdout_bytes_captured"] + result["stdout_bytes_omitted"]
    assert "HEAD" in result["output"] and "TAIL" in result["output"]
    assert secret not in json.dumps(result)
    assert result["rpc"]["admitted"] == 0


def test_file_backend_stop_keeps_print_progress(tmp_path, monkeypatch):
    backend = TempFileBackend(tmp_path)
    monkeypatch.setattr(sandbox, "_get_or_create_env", lambda task: (backend, "test-file-backend"))
    tid = threading.get_ident()
    timer = threading.Timer(0.2, lambda: set_interrupt(True, tid, reason="test Stop"))
    timer.start()
    try:
        result = json.loads(sandbox._execute_remote('import time\nprint("FILE_STOP_PROGRESS")\ntime.sleep(30)', "file-stop-task", ["read_file"]))
        assert result["status"] == "interrupted", result
        assert "FILE_STOP_PROGRESS" in result["output"]
        assert result["execution"]["phase"] == "interrupted"
        assert result["rpc"]["active"] == 0
    finally:
        timer.join()


def test_real_serial_tool_deadline_does_not_complete_outer_task(tmp_path, monkeypatch):
    backend = TempFileBackend(tmp_path)
    artifact = tmp_path / "after-inner-timeout.txt"
    monkeypatch.setattr(sandbox, "_get_or_create_env", lambda task: (backend, "test-file-backend"))
    monkeypatch.setattr(sandbox, "_load_config", lambda: {"timeout": 5, "max_tool_calls": 10})
    code = f'from hermes_tools import terminal,write_file\nimport time\nprint("BEFORE_INNER")\nprint(terminal("sleep 3", timeout=1))\nwrite_file({str(artifact)!r}, "after inner timeout")\nprint("SECOND_RETURNED")\ntime.sleep(30)'
    result = json.loads(sandbox._execute_remote(code, "serial-deadline-task", ["terminal", "write_file"]))
    assert result["status"] == "timeout", result
    assert artifact.read_text() == "after inner timeout"
    assert "SECOND_RETURNED" in result["output"]
    assert result["rpc"]["admitted"] == result["rpc"]["completed"] == 2
    assert result["execution"]["phase"] == "timeout"
    inner = result["rpc"]["calls"][0]
    assert inner["requested_tool_timeout_seconds"] == 1
    assert inner["tool_phase"] == "timeout"
    assert inner["rpc_timeout_seconds"] == 300
    assert inner["outer_remaining_seconds"] <= 5
    assert result["rpc"]["calls"][1]["artifact_bytes_written"] > 0


def test_expired_outer_budget_denies_admission_with_timeout_phase():
    owner = sandbox._SandboxExecution(threading.Event(), [], [0], 0.01)
    owner.deadline = time.monotonic() - 1
    assert owner.begin_call("read_file", {}) is None
    assert owner.deadline_expired.is_set()
    assert owner.result_metadata("success", None)["execution"]["phase"] == "timeout"
    assert owner.snapshot()["admitted"] == 0
