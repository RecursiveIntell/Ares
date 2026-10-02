"""Capability refresh must retain the admitted turn's profile and live runtime."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tests.tui_gateway.test_prompt_recovery_contract import _session, turn_env  # noqa: F401

_REAL_THREAD = threading.Thread
SID = "runtime-bot"
KEY = "durable-bot"


class FakeAgent:
    """Inert constructor/provider boundary; admission and SQLite stay real."""
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self._session_db = kw["session_db"]
        self._owns_session_db = False
        self._session_title_hint = "Bot Chat"
        self.context_rebase_enabled = True
        self.calls = []
        self.client_closes = []
        self.client = object()

    def release_clients(self):
        self.client_closes.append(self.client)
        self.client = None

    def clear_interrupt(self):
        pass

    def _close_openai_client(self, client, *, reason, shared):
        self.client_closes.append(client)

    def switch_model(self, *, new_model, new_provider, **kw):
        self.model, self.provider = new_model, new_provider
        self.__dict__.update(kw)

    def run_conversation(self, text, *, persist_user_event_id=None,
                         persist_user_message=None, **kw):
        from agent.context_input import accept_turn_input
        db = self._session_db
        db.create_session(self.session_id, source=self.platform)
        receipt = accept_turn_input(self, user_message=text,
            persist_user_message=persist_user_message, timestamp=None,
            display_kind=None, display_metadata=None,
            event_id=persist_user_event_id)
        self.calls.append((receipt, self.model, self.reasoning_config, self.service_tier))
        # The provider is inert; use the real message sink selected by this agent.
        db.append_message(self.session_id, "user", persist_user_message)
        db.append_message(self.session_id, "assistant", "offline reply")
        return {"final_response": "offline reply", "messages": [
            {"role": "user", "content": persist_user_message},
            {"role": "assistant", "content": "offline reply"}]}


@pytest.fixture
def env(monkeypatch, tmp_path, turn_env):
    # turn_env inlines helper threads; real SQLite needs its real writer thread.
    monkeypatch.setattr(server.threading, "Thread", _REAL_THREAD)
    launch = tmp_path / "launch"
    target = tmp_path / "target"
    launch.mkdir()
    target.mkdir()
    launch_db = SessionDB(launch / "state.db")
    target_db = SessionDB(target / "state.db")
    monkeypatch.setattr(server, "_db", launch_db)
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(server, "_sessions", {})
    events, built = [], []
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("config-model", "fake"))
    monkeypatch.setattr(server, "_config_model_target", lambda: ("config-model", "fake"))
    monkeypatch.setattr(server, "_resolve_runtime_with_fallback", lambda kw: SimpleNamespace(
        runtime={"provider": "fake", "api_key": "inert", "base_url": "http://invalid",
                 "api_mode": "chat_completions"}, used_fallback=False))
    monkeypatch.setattr(server, "_load_reasoning_config", lambda model: {"effort": "low"})
    monkeypatch.setattr(server, "_load_service_tier", lambda: "priority")
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform: [])
    monkeypatch.setattr(server, "_load_fallback_model", lambda: None)
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *args: None)
    monkeypatch.setattr("hermes_cli.mcp_startup.wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr("tui_gateway.entry.wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr("tools.bot_mode_probe.capability_fingerprint", lambda home: "new-caps")

    def construct(**kw):
        agent = FakeAgent(**kw)
        built.append(agent)
        return agent
    monkeypatch.setattr("run_agent.AIAgent", construct)
    old = FakeAgent(session_db=target_db, session_id=KEY, platform="tui",
        model="effective-model", provider="fake", api_key="inert-live",
        base_url="http://effective.invalid", api_mode="chat_completions",
        reasoning_config={"effort": "high"}, service_tier=None)
    old._owns_session_db = True
    target_db.create_session(KEY, source="tui")
    launch_db.create_session("launch-sentinel", source="tui")
    launch_db.append_message("launch-sentinel", "user", "untouched")
    session = _session(old, sid=SID, session_key=KEY, profile_home=str(target),
        bot_caps_seen="old-caps", config_model_seen=("config-model", "fake"), source="tui")
    server._sessions[SID] = session
    home_token = server.set_hermes_home_override(str(target))
    yield SimpleNamespace(session=session, old=old, target=target_db, launch=launch_db,
        built=built, events=events, construct=construct, home=target, monkeypatch=monkeypatch)
    server.reset_hermes_home_override(home_token)
    target_db.close()
    launch_db.close()


def test_real_prompt_refresh_keeps_admitted_receipt_and_messages_in_target(env):
    receipt = server._accept_tui_context_input(env.session, "offline input", event_id="input-1")
    assert receipt is not None
    env.session["running"] = True
    server._run_prompt_submit("request", SID, env.session, "offline input",
                              context_input_event_id=receipt.event_id)
    env.session["_run_thread"].join(timeout=10)
    assert not env.session["_run_thread"].is_alive()
    agent = env.session["agent"]
    assert agent is not env.old
    assert agent._session_db is env.target
    assert agent.calls[0][0] == receipt
    assert env.target.read_context_input(KEY, source="tui", event_id="input-1") == receipt
    assert env.launch.get_session(KEY) is None
    assert [(r["role"], r["content"]) for r in env.target.get_messages(KEY)] == [
        ("user", "offline input"), ("assistant", "offline reply")]
    assert env.launch.get_messages(KEY) == []
    assert env.launch.get_messages("launch-sentinel")[0]["content"] == "untouched"
    assert env.target.get_session(KEY)["ended_at"] is None
    assert agent.model == "effective-model"
    assert agent.reasoning_config == {"effort": "high"}
    assert agent.service_tier in (None, "")
    assert agent._owns_session_db is True
    assert env.old._owns_session_db is False
    assert env.session["bot_caps_seen"] == "new-caps"


@pytest.mark.parametrize("mode", ["pinned", "resumed", "once"])
@pytest.mark.parametrize("reasoning,tier", [(None, None), ({"enabled": False}, ""),
                                           ({"effort": "high"}, "priority")])
def test_refresh_keeps_effective_runtime_not_stale_session_metadata(env, mode, reasoning, tier):
    env.old.reasoning_config, env.old.service_tier = reasoning, tier
    if mode == "pinned":
        env.session["model_override"] = {"model": "stale-pin"}
    elif mode == "resumed":
        env.session["resume_runtime_overrides"] = {"model_override": "stale-resume",
            "reasoning_config_override": {"effort": "low"}, "service_tier_override": "priority"}
    else:
        env.session["one_turn_model_restore"] = {"model": "after-once"}
    server._sync_bot_capabilities(SID, env.session)
    new = env.session["agent"]
    assert new is not env.old
    for field in ("model", "provider", "api_key", "base_url", "api_mode"):
        assert getattr(new, field) == getattr(env.old, field)
    assert new.reasoning_config == reasoning
    assert (new.service_tier or "") == (tier or "")
    if isinstance(reasoning, dict):
        assert new.reasoning_config is not reasoning
    assert env.old.client_closes and len(env.old.client_closes) == 1
    assert new.client_closes == []
    assert env.target.get_session(KEY)["ended_at"] is None
    assert env.session["config_model_seen"] == ("config-model", "fake")


def test_once_turn_runs_effective_model_then_restores_original_after_refresh(env):
    original = server._snapshot_agent_model_runtime(env.old)
    original["model"] = "after-once"
    env.session["one_turn_model_restore"] = original
    env.session["_one_turn_model_runtime"] = {
        "session": env.session, "agent": env.old, "sid": SID, "active": False,
    }
    receipt = server._accept_tui_context_input(env.session, "once input", event_id="once")
    server._run_prompt_submit("once-request", SID, env.session, "once input",
                              context_input_event_id=receipt.event_id)
    env.session["_run_thread"].join(timeout=10)
    assert not env.session["_run_thread"].is_alive()
    new = env.session["agent"]
    assert new.calls[0][1] == "effective-model"
    assert new.model == "after-once"
    assert "one_turn_model_restore" not in env.session
    assert env.launch.get_session(KEY) is None


@pytest.mark.parametrize("title,seen,fingerprint", [
    ("Group: six", "old-caps", "new-caps"),
    ("Ordinary chat", "old-caps", "new-caps"),
    ("Bot Chat", None, "new-caps"),
    ("Bot Chat", "new-caps", "new-caps"),
    ("Bot Chat", "old-caps", "unavailable"),
])
def test_noop_capability_states_do_not_construct_or_release(env, title, seen, fingerprint):
    env.old._session_title_hint = title
    env.session["bot_caps_seen"] = seen
    env.monkeypatch.setattr("tools.bot_mode_probe.capability_fingerprint", lambda home: fingerprint)
    server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is env.old
    assert env.old._owns_session_db is True
    assert env.built == []
    assert env.old.client_closes == []
    assert env.session["bot_caps_seen"] == (fingerprint if title == "Bot Chat" and seen is None else seen)


def test_construction_failure_keeps_owner_and_retries_same_fingerprint(env):
    def fail(**kw):
        raise RuntimeError("inert constructor failure")
    env.monkeypatch.setattr("run_agent.AIAgent", fail)
    server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is env.old
    assert env.old._owns_session_db is True
    assert env.session["bot_caps_seen"] == "old-caps"
    assert env.old.client_closes == []
    assert env.target.get_session(KEY)["ended_at"] is None
    env.monkeypatch.setattr("run_agent.AIAgent", env.construct)
    server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is not env.old
    assert env.session["bot_caps_seen"] == "new-caps"
    assert len(env.old.client_closes) == 1


@pytest.mark.parametrize("change", ["record", "closing", "cancel", "key", "profile", "agent", "db", "owns"])
def test_stale_owner_during_construction_never_publishes(env, change):
    def build_then_change(**kw):
        new = env.construct(**kw)
        if change == "record":
            server._sessions[SID] = dict(env.session)
        elif change == "closing":
            env.session["_closing"] = True
        elif change == "cancel":
            env.session["_turn_cancel_requested"] = True
        elif change == "key":
            env.session["session_key"] = "replacement-key"
        elif change == "profile":
            env.session["profile_home"] = str(Path(env.launch.db_path).parent)
        elif change == "agent":
            env.session["agent"] = SimpleNamespace()
        elif change == "db":
            env.old._session_db = env.launch
        else:
            env.old._owns_session_db = False
        return new
    env.monkeypatch.setattr("run_agent.AIAgent", build_then_change)
    with pytest.raises(RuntimeError, match="BOT_CAPABILITY_OWNER_CHANGED"):
        server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is not env.built[0]
    assert env.built[0]._owns_session_db is False
    assert len(env.built[0].client_closes) == 1
    assert env.old.client_closes == []
    assert env.session["bot_caps_seen"] == "old-caps"
    assert env.target.get_session(KEY)["ended_at"] is None
    assert env.launch.get_session(KEY) is None


@pytest.mark.parametrize("change", ["db", "session_id", "model", "provider", "api_key", "base_url", "api_mode",
                                    "reasoning_config", "service_tier", "acp_command", "acp_args"])
def test_replacement_drift_is_rejected_and_cleaned_once(env, change):
    def wrong(**kw):
        new = env.construct(**kw)
        setattr(new, "_session_db" if change == "db" else change,
                env.launch if change == "db" else "different")
        return new
    env.monkeypatch.setattr("run_agent.AIAgent", wrong)
    server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is env.old
    assert env.old._owns_session_db is True
    assert env.old.client_closes == []
    assert env.session["bot_caps_seen"] == "old-caps"
    assert len(env.built[0].client_closes) == 1
    assert env.built[0]._owns_session_db is False
    assert env.target.get_session(KEY)["ended_at"] is None
    assert env.launch.get_session(KEY) is None


def test_shared_launch_handle_remains_unowned_and_usable(env):
    env.session["profile_home"] = None
    env.old._session_db = env.launch
    env.old._owns_session_db = False
    env.launch.create_session(KEY, source="tui")
    server._sync_bot_capabilities(SID, env.session)
    new = env.session["agent"]
    assert new is not env.old
    assert new._session_db is env.launch
    assert new._owns_session_db is False
    assert env.old._owns_session_db is False
    env.launch.append_message(KEY, "user", "still open")
    assert env.launch.get_messages(KEY)[0]["content"] == "still open"
    assert env.launch.get_session(KEY)["ended_at"] is None


def test_wrong_current_store_aborts_prompt_before_any_fake_provider_call(env):
    receipt = server._accept_tui_context_input(env.session, "owned input", event_id="wrong-owner")
    env.old._session_db = env.launch
    server._run_prompt_submit("wrong-request", SID, env.session, "owned input",
                              context_input_event_id=receipt.event_id)
    env.session["_run_thread"].join(timeout=10)
    assert not env.session["_run_thread"].is_alive()
    assert env.old.calls == []
    assert env.built == []
    assert env.target.get_messages(KEY) == []
    assert env.launch.get_session(KEY) is None
    terminals = [args[2] for args in env.events if args[0] == "message.complete"]
    assert len(terminals) == 1
    assert terminals[0]["status"] == "error"
    assert "BOT_CAPABILITY_STORE_MISMATCH" in terminals[0]["error"]


def test_transfer_failure_keeps_predecessor_and_releases_only_candidate(env):
    env.monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)
    server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is env.old
    assert env.old._owns_session_db is True
    assert env.old.client_closes == []
    assert env.session["bot_caps_seen"] == "old-caps"
    assert len(env.built[0].client_closes) == 1
    assert env.target.get_session(KEY)["ended_at"] is None


@pytest.mark.parametrize("field,value", [("model", "changed"), ("provider", "changed"),
    ("api_key", "changed"), ("base_url", "http://changed.invalid"), ("api_mode", "changed"),
    ("reasoning_config", {"effort": "low"}), ("service_tier", "priority"),
    ("acp_command", "changed"), ("acp_args", ["changed"])])
def test_effective_runtime_change_during_build_aborts_stale_publication(env, field, value):
    def change_runtime(**kw):
        new = env.construct(**kw)
        if field == "reasoning_config":
            env.old.reasoning_config.update(value)
        else:
            setattr(env.old, field, value)
        return new
    env.monkeypatch.setattr("run_agent.AIAgent", change_runtime)
    with pytest.raises(RuntimeError, match="BOT_CAPABILITY_OWNER_CHANGED"):
        server._sync_bot_capabilities(SID, env.session)
    assert env.session["agent"] is env.old
    assert env.old._owns_session_db is True
    assert getattr(env.old, field) == value
    assert env.old.client_closes == []
    assert len(env.built[0].client_closes) == 1
    assert env.session["bot_caps_seen"] == "old-caps"


def test_invalid_owned_launch_handle_is_rejected_before_constructor(env):
    env.session["profile_home"] = None
    env.old._session_db = env.launch
    with pytest.raises(RuntimeError, match="BOT_CAPABILITY_STORE_MISMATCH"):
        server._sync_bot_capabilities(SID, env.session)
    assert env.built == []
    assert env.session["agent"] is env.old
    assert env.session["bot_caps_seen"] == "old-caps"
    assert env.launch.get_messages("launch-sentinel")[0]["content"] == "untouched"


def test_existing_make_agent_none_reasoning_argument_still_inherits_config(env):
    new = server._make_agent(SID, KEY, session_db=env.target, reasoning_config_override=None)
    assert new.reasoning_config == {"effort": "low"}


def test_failed_client_retirement_cannot_undo_published_handoff(env):
    def fail():
        env.old.client_closes.append("attempt")
        raise RuntimeError("inert cleanup failure")
    env.old.release_clients = fail
    server._sync_bot_capabilities(SID, env.session)
    new = env.session["agent"]
    assert new is not env.old
    assert new._owns_session_db is True
    assert env.old._owns_session_db is False
    assert env.old.client_closes == ["attempt"]
    assert env.session["bot_caps_seen"] == "new-caps"
    assert env.target.get_session(KEY)["ended_at"] is None
