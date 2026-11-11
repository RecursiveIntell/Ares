"""RCA witness: a Stop that lands during a turn drops that turn from session['history'].

Runs the REAL ``server._run_prompt_submit`` with a fake agent (no provider, no network).
The fake agent reproduces exactly what the Stop path does to the session record while a
turn is in flight (tui_gateway/server.py ``_interrupt_session_turn``: bump
``_queued_prompt_generation`` + ``_last_stop_queue_generation``, set
``_turn_cancel_requested``) and then returns the interrupted result the way
``agent.run_conversation`` does, carrying the turn's messages.

Controls:
  * completed turn            -> history grows with the turn's messages (expected, passes today)
  * Stop mid-turn             -> history MUST also contain the turn's messages
                                 (durable store already has them; losing them is the bug)
"""
from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_sessions", dict(server._sessions))  # isolate the registry
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    events: list = []
    monkeypatch.setattr(server, "_emit", lambda e, sid, payload=None: events.append((e, payload)))
    yield events
    # Unregister the fixture session before conftest's teardown closes live sessions:
    # closing it would run the real observability finalizer (blocks in this harness).
    server._sessions.pop("sid", None)


def _session(agent, history):
    return {
        "agent": agent, "session_key": "session-key", "history": list(history),
        "history_lock": threading.Lock(), "history_version": 0, "running": True,
        "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
        "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
    }


def _turn_messages(prior, user_text):
    """What run_conversation returns: prior history + this turn's rows."""
    return list(prior) + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "assistant", "content": "partial"},
    ]


def _apply_stop(session):
    """Exactly the session-record effect of tui_gateway.server._interrupt_session_turn."""
    with session["history_lock"]:
        session["_turn_cancel_requested"] = True
        session["queued_prompt"] = None
        session.pop("queued_prompts", None)
        generation = int(session.get("_queued_prompt_generation", 0)) + 1
        session["_queued_prompt_generation"] = generation
        session["_last_stop_queue_generation"] = generation


def _run(env, *, stop: bool, interrupted: bool):
    prior = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    agent = types.SimpleNamespace(session_id="session-key", provider="p", model="m",
                                  clear_interrupt=lambda: None)

    def run_conversation(message, **kwargs):
        history = kwargs["conversation_history"]
        assert history == prior  # the turn started from the full prior context
        if stop:
            _apply_stop(session)
        return {"final_response": "partial" if not interrupted else "", "interrupted": interrupted,
                "messages": _turn_messages(history, message), "api_calls": 2}

    agent.run_conversation = run_conversation
    session = _session(agent, prior)
    server._sessions["sid"] = session  # live sessions are always registered
    server._start_inflight_turn(session, "do work")
    server._run_prompt_submit("rid", "sid", session, "do work")
    return session, prior


def test_control_completed_turn_is_written_back(env):
    session, prior = _run(env, stop=False, interrupted=False)
    assert len(session["history"]) == len(prior) + 4


def test_control_interrupt_without_stop_generation_is_written_back(env):
    # e.g. interrupt/steer paths that do not bump the stop generation
    session, prior = _run(env, stop=False, interrupted=True)
    assert len(session["history"]) == len(prior) + 4


def test_stop_mid_turn_keeps_the_turn_in_history(env):
    session, prior = _run(env, stop=True, interrupted=True)
    # The durable store holds the turn's 4 rows. The live history must too, or the
    # next turn starts on a stale snapshot (model "forgets" the interrupted work).
    assert len(session["history"]) == len(prior) + 4, (
        f"history stayed at the turn-start snapshot: {len(session['history'])} rows"
    )


def test_stopped_turn_never_overwrites_a_successor_admission(env):
    """Negative control: if a NEWER admission already owns the session, the stopped
    turn's tail must not clobber history (that is what the guard protects)."""
    prior = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    agent = types.SimpleNamespace(session_id="session-key", provider="p", model="m",
                                  clear_interrupt=lambda: None)

    def run_conversation(message, **kwargs):
        history = kwargs["conversation_history"]
        _apply_stop(session)
        # successor admission lands while the stopped thread is still unwinding
        with session["history_lock"]:
            session["inflight_turn"] = {"started_at": object(), "assistant": "", "streaming": True}
            session["history_version"] = int(session.get("history_version", 0)) + 1
        return {"final_response": "", "interrupted": True,
                "messages": _turn_messages(history, message), "api_calls": 2}

    agent.run_conversation = run_conversation
    session = _session(agent, prior)
    server._sessions["sid"] = session
    server._start_inflight_turn(session, "do work")
    server._run_prompt_submit("rid", "sid", session, "do work")
    assert session["history"] == prior
