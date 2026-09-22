"""Real Python conversation/SDK HTTP/todo owner, not native-loop qualification.

The subprocess avoids autouse constructor/provider mocks. Only the remote model
is a deterministic loopback fixture. Application and SQLite owners are real.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario", ["fresh", "resumed", "seed", "create-failure", "contended"])
def test_http_todo_write_and_fresh_agent_recovery(tmp_path, scenario):
    home = tmp_path / "home"
    profile = home / "profile"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "mcp_servers: {}\nplugins: {}\ncompression:\n  enabled: false\n"
        "monitoring:\n  stack_observation:\n    enabled: false\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n",
        encoding="utf-8",
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "HERMES_HOME": str(profile), "ARES_HOME": str(profile),
           "HERMES_TEST_ISOLATION": str(profile), "LANG": "C.UTF-8",
           "TZ": "UTC", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path),
         scenario],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((tmp_path / "observation.json").read_text(encoding="utf-8"))
    if scenario == "create-failure":
        assert report["posts"] == []
        assert report["creation_failed"] is True
        assert report["denied_connections"] == []
        return
    assert report["selected"]["todos"] == report["expected"]
    assert report["hydrated"] == report["expected"]
    assert len(report["posts"]) == 3
    assert report["denied_connections"] == [], json.dumps(report["denied_connections"], indent=2)


def _exercise(root, scenario):
    import http.server
    import threading

    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    items = [{"id": "real-http", "content": "Retain actual dispatched work", "status": "in_progress"}]
    report = {"expected": items, "posts": [], "denied_connections": []}
    peer_held = threading.Event()

    def network_guard(event, args):
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
                report["denied_connections"].append(str(address))
                raise PermissionError("test permits loopback only")
        if event == "socket.getaddrinfo" and args[0] not in ("127.0.0.1", "::1", "localhost"):
            import traceback
            report["denied_connections"].append({"host": str(args[0]), "stack": [f"{f.filename}:{f.lineno} {f.name}" for f in traceback.extract_stack() if str(repo) in f.filename]})
            raise PermissionError("test permits loopback only")

    sys.addaudithook(network_guard)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self.send_json({"object": "list", "data": [
                {"id": "todo-http-local", "object": "model", "context_length": 131072},
            ]})

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            assert 0 < size < 2_000_000
            request = json.loads(self.rfile.read(size))
            report["posts"].append(request)
            assert not peer_held.is_set(), "provider effect before peer released custody"
            first = len(report["posts"]) == 1
            calls = [{"id": "call-http-todo", "type": "function", "function": {
                "name": "todo", "arguments": json.dumps({"todos": items}),
            }}] if first else None
            message = {"role": "assistant", "content": "" if first else "LOCAL_DONE"}
            if calls:
                message["tool_calls"] = calls
            finish = "tool_calls" if first else "stop"
            common = {"id": "local-response", "model": "todo-http-local", "created": 1}
            if request.get("stream"):
                delta = dict(message)
                if calls:
                    delta["tool_calls"] = [dict(calls[0], index=0)]
                chunks = [dict(common, object="chat.completion.chunk", choices=[{
                    "index": 0, "delta": delta, "finish_reason": None,
                }]), dict(common, object="chat.completion.chunk", choices=[{
                    "index": 0, "delta": {}, "finish_reason": finish,
                }])]
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                        + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json(dict(common, object="chat.completion", choices=[{
                    "index": 0, "message": message, "finish_reason": finish,
                }], usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = agent = None
    try:
        from hermes_state import SessionDB
        from run_agent import AIAgent

        def create_agent(owner):
            return AIAgent(
                api_key="loopback-fixture-not-a-secret",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                provider="openai-compat", api_mode="chat_completions", model="todo-http-local",
                max_iterations=3, enabled_toolsets=["todo"], quiet_mode=True,
                skip_context_files=True, skip_memory=True, skip_background_review=True,
                save_trajectories=False, session_id="http-owner", session_db=owner,
                fallback_model=None, run_budget_seconds=30, max_tokens=128,
            )

        db = SessionDB(db_path=root / "state.db")
        if scenario == "resumed":
            db.create_session("http-owner", source="test")
        agent = create_agent(db)
        seed = [{"role": "user", "content": "Preserve my seed"},
                {"role": "assistant", "content": "Seed acknowledged"}]
        kwargs = {"system_message": "Local boundary test."}
        if scenario in ("seed", "contended"):
            kwargs["conversation_history"] = seed
        if scenario == "create-failure":
            # Scratch-only SQLite failure injection; no owner or loop mocks.
            db._conn.execute("CREATE TRIGGER deny_session BEFORE INSERT ON sessions "
                             "BEGIN SELECT RAISE(ABORT, 'injected creation failure'); END")
            result = agent.run_conversation("Track the requested work", **kwargs)
            assert result["failed"] is True and result["api_calls"] == 0
            assert result["error"] == "session_persistence_admission_failed:http-owner"
            assert report["posts"] == []
            assert db.get_session("http-owner") is None
            assert agent._todo_store.read() == []
            assert agent._active_session_turn_lease_holder is None
            assert db.try_acquire_session_turn_lease("http-owner", "cleanup-probe")
            db.release_session_turn_lease("http-owner", "cleanup-probe")
            report["creation_failed"] = True
            return
        if scenario == "contended":
            # Separate real owner handle; wait callback is only an observation.
            peer = SessionDB(db_path=root / "state.db")
            holder = f"pid={os.getpid()}:peer"
            assert peer.try_acquire_session_turn_lease("http-owner", holder)
            peer_held.set()
            waiting, finished = threading.Event(), threading.Event()
            agent.status_callback = lambda _kind, text=None: waiting.set() if text and "waiting for it" in text else None
            results, errors = [], []

            def run_turn():
                try:
                    results.append(agent.run_conversation("Track the requested work", **kwargs))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=run_turn)
            worker.start()
            try:
                assert waiting.wait(10), (report["posts"], errors)
                assert report["posts"] == []
                assert not finished.is_set()
                peer.create_session("http-owner", source="test")
                peer.append_message("http-owner", "user", "Peer durable turn")
                peer.append_message("http-owner", "assistant", "Peer durable answer")
                peer_held.clear()
                peer.release_session_turn_lease("http-owner", holder)
            finally:
                peer_held.clear()
                peer.release_session_turn_lease("http-owner", holder)
                worker.join(timeout=45)
                peer.close()
            assert not worker.is_alive()
            assert not errors, errors
            result = results[0]
        else:
            result = agent.run_conversation("Track the requested work", **kwargs)
        if scenario == "seed":
            # Persistence annotates the shallow-copied messages with
            # _db_persisted; the caller's role/content and provider bytes survive.
            expected_seed = [{"role": "user", "content": "Preserve my seed"},
                             {"role": "assistant", "content": "Seed acknowledged"}]
            assert [{k: m[k] for k in ("role", "content")} for m in seed] == expected_seed
            sent = report["posts"][0]["messages"]
            for message in expected_seed:
                assert message in sent
        if scenario == "contended":
            sent = report["posts"][0]["messages"]
            assert any(m.get("content") == "Peer durable answer" for m in sent)
            assert not any(m.get("content") == "Preserve my seed" for m in sent)
        report["first_result"] = {k: result.get(k) for k in ("final_response", "completed", "error")}
        assert result["final_response"] == "LOCAL_DONE"
        assert len(report["posts"]) == 2
        assert any(t["function"]["name"] == "todo" for t in report["posts"][0]["tools"])
        outcomes = [json.loads(m["content"]) for m in report["posts"][1]["messages"] if m["role"] == "tool"]
        report["tool_results"] = outcomes
        assert len(outcomes) == 1 and outcomes[0].get("todos") == items, outcomes
        report["selected"] = db.get_current_todo_snapshot("http-owner")
        assert report["selected"]["todos"] == items
        agent.close()
        agent = None
        db.close()
        db = SessionDB(db_path=root / "state.db")
        agent = create_agent(db)
        assert agent._todo_store.read() == []
        recovered = agent.run_conversation("Continue", system_message="Local boundary test.")
        assert recovered["final_response"] == "LOCAL_DONE"
        report["hydrated"] = agent._todo_store.read()
        assert report["hydrated"] == items
    finally:
        if agent is not None:
            agent.close()
        if db is not None:
            db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (root / "observation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    _exercise(Path(sys.argv[1]), sys.argv[2])
