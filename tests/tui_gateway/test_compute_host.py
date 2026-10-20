import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tui_gateway.compute_host import ComputeHost, HostSession


@pytest.mark.parametrize('exit_path', ['stdin_closed', 'sigterm'])
def test_live_work_process_exit_records_exact_boot_unknown(tmp_path, exit_path):
    # Real Linux pipes/process exit, no provider, credentials or service. A
    # barrier worker deliberately cannot settle within the shutdown window.
    script = '''
import io,json,signal,sys,threading
wire=sys.stdout
from tui_gateway.compute_host import ComputeHost
from tui_gateway import server
host=ComputeHost(stdout=wire,heartbeat_secs=0)
started=threading.Event()
blocked=threading.Event()
def work():
    started.set()
    blocked.wait()
future=host._executor.submit(work)
host._track_turn_future(future,'live-child')
host._active_request_ids['live-child']='request-owner'
server._sessions={}
started.wait(2)
host.emit({'type':'hello','boot_id':host._boot_id})
def stop(*args):
    host.shutdown(reason='sigterm',wait=0.1)
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
for line in sys.stdin:
    pass
host.shutdown(reason='stdin_closed',wait=0.1)
'''
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen([sys.executable, '-c', script], cwd=repo, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'HERMES_HOME': str(tmp_path), 'PYTHONPATH': str(repo)})
    out = _stdout_queue(proc)
    try:
        try:
            hello = _read_json_line(out, timeout=10)
        except AssertionError:
            proc.kill()
            proc.wait(timeout=2)
            raise AssertionError(proc.stderr.read())
        if exit_path == 'stdin_closed':
            proc.stdin.close()
        else:
            proc.terminate()
        proc.wait(timeout=4)
        assert proc.returncode == 0
        receipts = [json.loads(line) for line in
            (tmp_path / 'state' / 'dashboard-compute-host-outcomes.jsonl').read_text().splitlines()]
        assert any(r['boot_id'] == hello['boot_id'] and r['sid'] == 'live-child'
            and r['request_id'] == 'request-owner' and r['outcome'] == 'unknown'
            and r['classification'] == 'HOST_SHUTDOWN_UNSETTLED' for r in receipts)
        assert all(r.get('outcome') != 'completed' for r in receipts)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)


def _stdout_queue(proc: subprocess.Popen) -> queue.Queue[dict]:
    out: queue.Queue[dict] = queue.Queue()
    assert proc.stdout is not None

    def drain() -> None:
        for line in proc.stdout or []:
            out.put(json.loads(line))

    threading.Thread(target=drain, daemon=True).start()
    return out


def _read_json_line(out: queue.Queue[dict], timeout: float = 2.0) -> dict:
    try:
        return out.get(timeout=timeout)
    except queue.Empty as exc:
        raise AssertionError("timed out waiting for compute host JSON") from exc


def test_compute_host_line_json_seed_turn_interrupt():
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tui_gateway.compute_host"],
        cwd=str(repo),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None
    out = _stdout_queue(proc)
    try:
        hello = _read_json_line(out)
        assert hello["type"] == "hello"
        assert hello["host_pid"] == proc.pid

        proc.stdin.write(json.dumps({"type": "session.seed", "sid": "s1", "request_id": "seed"}) + "\n")
        proc.stdin.flush()
        assert _read_json_line(out)["type"] == "session.seeded"

        proc.stdin.write(
            json.dumps(
                {
                    "type": "turn.start",
                    "sid": "s1",
                    "request_id": "turn",
                    "prompt": "hello",
                    "delta_count": 3,
                    "delay_s": 0,
                }
            )
            + "\n"
        )
        proc.stdin.flush()

        seen = []
        while True:
            frame = _read_json_line(out)
            seen.append(frame["type"])
            if frame["type"] == "turn.end":
                assert frame["history_version"] == 1
                assert frame["message_count"] == 2
                break
        assert seen.count("delta") == 3

        proc.stdin.write(json.dumps({"type": "shutdown", "request_id": "stop"}) + "\n")
        proc.stdin.flush()
        assert _read_json_line(out)["type"] == "shutdown.ack"
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("kind", ["legacy", "hard-only", "dynamic-getattr"])
def test_compute_host_interrupt_uses_explicit_stop_compatibility(kind):
    calls = []

    class _Legacy:
        def interrupt(self):
            calls.append("legacy")

    class _HardOnly:
        def hard_interrupt(self):
            calls.append("hard")

    class _Dynamic:
        def interrupt(self):
            calls.append("legacy")

        def __getattr__(self, name):
            if name == "hard_interrupt":
                return lambda: calls.append("fabricated-hard")
            raise AttributeError(name)

    agent = {
        "legacy": _Legacy(),
        "hard-only": _HardOnly(),
        "dynamic-getattr": _Dynamic(),
    }[kind]
    host = ComputeHost(heartbeat_secs=0)
    host._sessions["s1"] = HostSession(sid="s1", agent=agent)
    emitted = []
    host.emit = emitted.append
    try:
        host._handle_interrupt({"sid": "s1", "request_id": "stop"})
    finally:
        host.close()

    assert calls == ["hard" if kind == "hard-only" else "legacy"]
    assert emitted[-1]["applied"] is True
