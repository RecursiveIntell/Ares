"""First publication owns its profile; aliases need exact physical route proof."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tests.tui_gateway.test_failed_turn_retention import _session, emits, turn_env  # noqa: F401


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    launch = tmp_path / "default"
    target = launch / "profiles" / "cognitive-scientist"
    target.mkdir(parents=True)
    launch_db = SessionDB(launch / "state.db")
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_db", launch_db)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_wire_callbacks", lambda *a: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *a: None)
    monkeypatch.setattr(server, "_persist_session_cwd_and_schedule_git_meta", lambda *a, **kw: None)
    monkeypatch.setattr("tools.approval.register_gateway_notify", lambda *a: None)
    monkeypatch.setattr("tools.approval.load_permanent_allowlist", lambda: None)
    events, agents = [], []

    def make(sid, key, **kw):
        agent = SimpleNamespace(session_id=key, model="model", provider="custom",
            _session_db=kw.get("session_db") or launch_db, _owns_session_db=False)
        agents.append(agent)
        return agent

    def emit(name, sid, payload=None):
        if name == "session.info":
            record = server._sessions[sid]
            events.append((dict(payload), record.get("profile_home"),
                           record["agent"]._session_db, record["agent"]._owns_session_db))
    monkeypatch.setattr(server, "_make_agent", make)
    monkeypatch.setattr(server, "_emit", emit)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    yield SimpleNamespace(host=host, launch=launch, target=target, launch_db=launch_db,
                          events=events, agents=agents, monkeypatch=monkeypatch)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a: None)
    host.close()
    for agent in agents:
        if agent._owns_session_db:
            agent._session_db.close()
    launch_db.close()


@pytest.mark.parametrize("named", [False, True])
def test_first_host_metadata_already_has_target_profile_owner(profile_env, named):
    env = profile_env
    frame = {"sid": "runtime-id", "session_key": "stored-id", "source": "desktop"}
    if named:
        frame["profile_home"] = str(env.target)
    session = env.host._ensure_server_session(server, frame)
    assert len(env.events) == 1
    info, home, db, owns = env.events[0]
    assert info["profile_name"] == ("cognitive-scientist" if named else "default")
    assert home == (str(env.target) if named else None)
    assert info["session_id"] == "runtime-id"
    assert info["stored_session_id"] == "stored-id"
    assert Path(db.db_path) == (env.target if named else env.launch) / "state.db"
    assert owns is named
    assert session["agent"]._session_db is db


@pytest.mark.parametrize("case", ["matching", "implicit_mode", "legacy", "trailing_slash", "configured_precedence",
    "unknown_alias", "disabled", "wrong_endpoint", "wrong_model", "fallback", "custom_fallback",
    "wrong_mode", "url_path_case", "missing_endpoint", "missing_mode", "unknown_model", "wrong_served_model",
    "stale_token", "stale_session_token", "bad_attempt", "changed_key", "changed_agent", "changed_owner",
    "changed_agent_session", "changed_endpoint", "changed_config", "changed_display", "not_completed",
    "no_calls", "untyped_witness"])
def test_named_alias_requires_actual_configured_transport(emits, turn_env, monkeypatch, tmp_path, case):
    from agent.turn_finalizer import AcceptedResponseRoute
    import yaml
    config = {"providers": {"ollama-launch": {"name": "Ollama (MSI)",
        "base_url": "http://127.0.0.1:11436/v1", "transport": "chat_completions"}}}
    if case == "implicit_mode":
        config["providers"]["ollama-launch"].pop("transport")
    elif case == "legacy":
        config = {"custom_providers": [{"name": "ollama-launch", "base_url": "http://127.0.0.1:11436/v1"}]}
    elif case == "configured_precedence":
        config["custom_providers"] = [{"name": "ollama-launch", "base_url": "http://wrong.invalid/v1"}]
    elif case == "disabled":
        config["providers"]["ollama-launch"]["enabled"] = False
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = server.set_hermes_home_override(str(home))
    agent = SimpleNamespace(session_id="session-key", provider="custom", model="model",
        base_url="http://127.0.0.1:11436/v1", api_mode="chat_completions", clear_interrupt=lambda: None)
    session = _session(agent=agent, model_override={"provider": "ollama-launch", "model": "model"},
                       profile_home=str(home))
    if case == "unknown_alias":
        session["model_override"]["provider"] = "unknown-alias"
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    def run(*args, **kw):
        fields = dict(turn_token=agent._route_turn_token, turn_id="turn", session_id="session-key",
            attempt_id="turn:api:1:0", provider="custom", model="model", served_model="model")
        fields.update(base_url=agent.base_url, api_mode=agent.api_mode)
        if case in ("wrong_endpoint", "custom_fallback"):
            fields["base_url"] = "http://other.invalid/v1"
        elif case == "trailing_slash":
            fields["base_url"] += "/"
        elif case == "wrong_model":
            fields["model"] = fields["served_model"] = "other-model"
        elif case == "fallback":
            fields["provider"] = "fallback-provider"
        elif case == "url_path_case":
            fields["base_url"] = fields["base_url"].replace("/v1", "/V1")
        elif case == "wrong_mode":
            fields["api_mode"] = "codex_responses"
        elif case == "missing_endpoint":
            fields["base_url"] = None
        elif case == "missing_mode":
            fields["api_mode"] = None
        elif case == "unknown_model":
            fields["served_model"] = None
        elif case == "wrong_served_model":
            fields["served_model"] = "other-model"
        elif case == "stale_token":
            fields["turn_token"] = object()
        elif case == "stale_session_token":
            session["_readiness_turn_token"] = object()
        elif case == "bad_attempt":
            fields["attempt_id"] = "other-turn:api:1:0"
        elif case == "changed_key":
            session["session_key"] = "other-key"
        elif case == "changed_agent":
            session["agent"] = SimpleNamespace()
        elif case == "changed_owner":
            server._sessions["sid"] = _session(agent=SimpleNamespace())
        elif case == "changed_agent_session":
            agent.session_id = "other-session"
        elif case == "changed_endpoint":
            agent.base_url = "http://other.invalid/v1"
        elif case == "changed_config":
            config["providers"]["ollama-launch"]["base_url"] = "http://other.invalid/v1"
            (home / "config.yaml").write_text(yaml.safe_dump(config))
        elif case == "changed_display":
            session["model_override"]["provider"] = "other-alias"
        return {"final_response": "offline reply", "completed": case != "not_completed",
                "api_calls": 0 if case == "no_calls" else 1,
                "accepted_response_route": fields if case == "untyped_witness" else AcceptedResponseRoute(**fields)}
    agent.run_conversation = run
    try:
        server._run_prompt_submit("request", "sid", session, "offline input")
        expected = case in ("matching", "implicit_mode", "legacy", "trailing_slash", "configured_precedence")
        assert session.get("model_verified_for") == (("ollama-launch", "model") if expected else None)
        info = server._session_info(agent, session)
        assert info["model_ready"] is expected
        if expected:
            assert info["provider"] == "ollama-launch"
            # A serving caller's launch context must not replace this profile's config.
            other_home = tmp_path / "other-profile"
            other_home.mkdir()
            (other_home / "config.yaml").write_text("providers: {}\n")
            other_token = server.set_hermes_home_override(str(other_home))
            try:
                assert server._session_info(agent, session)["model_ready"] is True
            finally:
                server.reset_hermes_home_override(other_token)
            # Later display cannot retain a positive proof after endpoint drift.
            agent.base_url = "http://later.invalid/v1"
            assert server._session_info(agent, session)["model_ready"] is False
    finally:
        server.reset_hermes_home_override(token)


@pytest.mark.parametrize("when", ["before_info", "after_info"])
def test_named_profile_fallback_record_preserves_publication_owner(profile_env, when):
    env = profile_env
    def fail(*args, **kwargs):
        raise RuntimeError("inert init side effect failed")
    env.monkeypatch.setattr(server, "_notify_session_boundary" if when == "before_info"
                           else "_schedule_mcp_late_refresh", fail)
    session = env.host._ensure_server_session(server, {"sid": "runtime-id", "session_key": "stored-id",
        "profile_home": str(env.target), "source": "desktop"})
    assert session["profile_home"] == str(env.target)
    assert session["agent"]._session_db.db_path == env.target / "state.db"
    assert session["agent"]._owns_session_db is True
    assert len(env.events) == (0 if when == "before_info" else 1)
    assert all(event[0]["profile_name"] == "cognitive-scientist" for event in env.events)
    assert server._session_info(session["agent"], session)["profile_name"] == "cognitive-scientist"


def test_failed_named_profile_construction_closes_store_without_publication(profile_env):
    env = profile_env
    closes = []
    def fail(sid, key, **kwargs):
        db = kwargs["session_db"]
        original = db.close
        def close():
            closes.append(db.db_path)
            original()
        db.close = close
        raise RuntimeError("inert constructor failure")
    env.monkeypatch.setattr(server, "_make_agent", fail)
    with pytest.raises(RuntimeError, match="inert constructor failure"):
        env.host._ensure_server_session(server, {"sid": "runtime-id", "session_key": "stored-id",
                                                "profile_home": str(env.target)})
    assert closes == [env.target / "state.db"]
    assert env.events == []
    assert "runtime-id" not in server._sessions
    assert env.launch_db.get_session("stored-id") is None


@pytest.mark.parametrize("case", ["matching", "wrong_boot", "wrong_request", "wrong_sid"])
def test_terminal_metadata_keeps_supervisor_boot_request_and_sid_guards(tmp_path, monkeypatch, case):
    import threading
    from tui_gateway.host_supervisor import HostSupervisor
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "owned-boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda deadline: None)
    monkeypatch.setattr(host, "_send_frame", lambda *args, **kwargs: None)
    settled = threading.Event()
    mirrored = []
    session = {"_metadata_mirror": {"model_ready": False}}
    def project(frame):
        server._apply_compute_host_metadata_mirror(session, frame)
        mirrored.append(frame)
        settled.set()
    host.submit_turn({"sid": "owned-sid", "request_id": "owned-request"}, on_complete=project)
    frame = {"type": "turn.end", "sid": "owned-sid", "request_id": "owned-request",
             "_host_boot_id": "owned-boot", "session_info": {"model_ready": True,
             "model": "model", "provider": "ollama-launch"}}
    if case == "wrong_boot":
        frame["_host_boot_id"] = "stale-boot"
    elif case == "wrong_request":
        frame["request_id"] = "stale-request"
    elif case == "wrong_sid":
        frame["sid"] = "other-sid"
    host._handle_host_frame(frame)
    if case == "matching":
        assert settled.wait(2)
        assert len(mirrored) == 1
        assert session["_metadata_mirror"]["model_ready"] is True
        host._handle_host_frame(frame)
        assert len(mirrored) == 1
    else:
        assert host._terminal_workers == set()
        assert mirrored == []
        assert session["_metadata_mirror"]["model_ready"] is False
        assert "owned-request" in host._pending_turns


def test_host_crash_revokes_mirrored_readiness(monkeypatch):
    session = _session(agent=SimpleNamespace(), _compute_host_active=True,
        _metadata_mirror={"provider": "ollama-launch", "model": "model", "model_ready": True})
    events = []
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    server._on_compute_host_crash()
    assert session["_metadata_mirror"]["model_ready"] is False
    assert events[-1][2]["model_ready"] is False
