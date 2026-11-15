"""Best-effort turn markers on Desktop/TUI resume, never effect authority.

The sidecar prompt can survive a process death, but failed clears or same-UID
writes mean its presence is not proof of death, noncompletion, or safe replay.
Tests cover marker I/O, normal turn retirement, explicitly opted-in automatic
continuation, and the disabled route's non-executing UI projection. The marker
is not an authenticated task, provider outcome, or model-history entry.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from tui_gateway import server
from tui_gateway.turn_marker import (
    clear_turn_marker,
    read_turn_marker,
    record_turn_start,
)


class _InlineThread:
    """Run threads synchronously so tests observe final state."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        **extra,
    }


@pytest.fixture()
def emits(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: captured.append((event, sid, payload)),
    )
    return captured


@pytest.fixture()
def marker_home(monkeypatch, tmp_path):
    """Point the server's marker storage at a temp HERMES_HOME."""
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, marker_home):
    """Neutralize the turn pipeline's environment-heavy side paths."""
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


# ── Marker module ──────────────────────────────────────────────────────


def test_marker_roundtrip(tmp_path):
    record_turn_start(tmp_path, "abc", "fix the bug", attempts=1)

    marker = read_turn_marker(tmp_path, "abc")
    assert marker is not None
    assert marker["prompt"] == "fix the bug"
    assert marker["attempts"] == 1
    assert marker["started_at"] == pytest.approx(time.time(), abs=5)

    clear_turn_marker(tmp_path, "abc")
    assert read_turn_marker(tmp_path, "abc") is None


def test_marker_survives_corrupt_sidecar(tmp_path):
    path = tmp_path / "desktop" / "interrupted_turns.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert read_turn_marker(tmp_path, "abc") is None
    record_turn_start(tmp_path, "abc", "prompt")
    assert read_turn_marker(tmp_path, "abc")["prompt"] == "prompt"


# ── Turn lifecycle owns the marker ─────────────────────────────────────


def test_concluded_turn_clears_marker(emits, turn_env, marker_home):
    seen_mid_turn: list = []

    def _run(message, **kwargs):
        seen_mid_turn.append(read_turn_marker(marker_home, "session-key"))
        return {"final_response": "done"}

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "do the thing")

    # Written before the turn ran (this is what survives a process death) …
    assert seen_mid_turn and seen_mid_turn[0] is not None
    assert seen_mid_turn[0]["prompt"] == "do the thing"
    assert seen_mid_turn[0]["attempts"] == 0
    # … and cleared once the turn concluded.
    assert read_turn_marker(marker_home, "session-key") is None


def test_handled_failure_still_clears_marker(emits, turn_env, marker_home):
    """An exception is a CONCLUDED turn (terminal frame + retained snapshot own
    recovery) — only a process death may leave the marker behind."""

    def _boom(message, **kwargs):
        raise RuntimeError("provider exploded")

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_boom, clear_interrupt=lambda: None
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "do the thing")

    assert read_turn_marker(marker_home, "session-key") is None


def test_continuation_turn_records_attempt_and_original_prompt(
    emits, turn_env, marker_home
):
    """A continuation's marker must carry the attempt count (crash-loop
    breaker) and the ORIGINAL prompt — recording its own recovery note would
    nest note inside note on a second crash."""
    seen: list = []

    def _run(message, **kwargs):
        seen.append(read_turn_marker(marker_home, "session-key"))
        return {"final_response": "done"}

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None
    )
    session = _session(
        agent=agent,
        running=True,
        _auto_continue_attempt=2,
        _auto_continue_prompt="the original prompt",
    )

    server._run_prompt_submit("rid", "sid", session, server._auto_continue_note("the original prompt"))

    assert [(m["attempts"], m["prompt"]) for m in seen] == [(2, "the original prompt")]
    # Consumed, so the NEXT user turn starts from a clean slate.
    assert "_auto_continue_attempt" not in session
    assert "_auto_continue_prompt" not in session


def test_older_agent_still_gets_the_post_turn_stamp(emits, turn_env, marker_home):
    """An agent whose run_conversation predates turn-start typing keeps the
    original behavior — the row is typed once the turn concludes."""
    stamped: list = []

    class _LegacyDB:
        def set_latest_matching_message_display_kind(self, session_id, **kwargs):
            stamped.append((session_id, kwargs["display_kind"]))
            return True

    def _run(message, conversation_history=None, stream_callback=None, **_kwargs):
        return {"final_response": "done"}

    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=_run,
        clear_interrupt=lambda: None,
        _session_db=_LegacyDB(),
    )
    note = server._auto_continue_note("the original prompt")

    server._run_prompt_submit(
        "rid", "sid", _session(agent=agent, running=True), note,
        display_kind="auto_continue",
    )

    assert stamped == [("session-key", "auto_continue")]


def test_auto_continue_note_does_not_assert_unproven_process_death():
    note = server._auto_continue_note("the original prompt")
    assert note.startswith("[System note: A previous turn may have been interrupted")
    assert "outcome is unknown" in note.lower()
    assert "Your previous turn was interrupted mid-run" not in note
    assert "stopped before the turn could finish" not in note
    assert "the original prompt" in note
    assert server._legacy_display_kind("user", note) == "auto_continue"
    legacy = "[System note: Your previous turn was interrupted mid-run — old note]"
    assert server._legacy_display_kind("user", legacy) == "auto_continue"


# ── Scheduling decision ────────────────────────────────────────────────


@pytest.fixture()
def schedule_env(monkeypatch, marker_home):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    submitted: list = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, session, text, **kw: submitted.append((text, kw)),
    )
    return submitted


def test_fresh_marker_schedules_continuation(
    emits, schedule_env, marker_home, monkeypatch
):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "fix the flaky test")
    session = _session()

    result = server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert result is not None
    assert result["attempt"] == 1
    assert session["running"] is True
    assert session["_auto_continue_attempt"] == 1
    (text, kwargs), = schedule_env
    assert text.startswith("[System note: A previous turn may have been interrupted")
    assert "fix the flaky test" in text
    assert kwargs["display_kind"] == "auto_continue"
    assert ("message.start", "sid", None) in [(e, s, p) for e, s, p in emits]


def test_disabled_auto_continue_preserves_interrupted_prompt_for_review(
    marker_home, monkeypatch
):
    """Cold resume must show an interrupted prompt without rerunning it."""
    monkeypatch.setattr(
        server, "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": False}}},
    )
    record_turn_start(marker_home, "session-key", "inspect before retry")
    session = _session()
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    assert read_turn_marker(marker_home, "session-key") is not None
    snapshot = server._cold_interrupted_turn_projection(session, "session-key")
    assert snapshot["user"] == "inspect before retry"
    assert snapshot["status"] == "error"
    assert snapshot["error"].startswith("Possible interrupted turn;")
    assert snapshot["streaming"] is False
    assert snapshot["error_surface"]["retryable"] is False


def test_deferred_desktop_resume_projects_disabled_interrupted_prompt(tmp_path, monkeypatch):
    """Desktop's defer_history+omit_messages path must not hide a crash marker."""
    class _DB:
        def get_session(self, session_id):
            return {"id": session_id, "message_count": 0}

        def resolve_resume_session_id(self, session_id):
            return session_id

    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(
        server, "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": False}}},
    )
    record_turn_start(tmp_path, "cold-stored", "review this before retry")
    response = server.handle_request({
        "id": "resume", "method": "session.resume",
        "params": {"session_id": "cold-stored", "defer_history": True,
                   "omit_messages": True, "source": "desktop"},
    })
    try:
        assert "error" not in response, response
        result = response["result"]
        assert result["hydrating"] is True
        assert result["messages"] == []
        assert result["inflight"]["user"] == "review this before retry"
        assert result["inflight"]["error_surface"]["retryable"] is False
        assert "auto_continue" not in result
        assert server._sessions[result["session_id"]]["history"] == []
        assert read_turn_marker(tmp_path, "cold-stored") is not None
    finally:
        if "result" in response:
            server._sessions.pop(response["result"]["session_id"], None)


def test_auto_continue_is_disabled_when_config_is_absent(
    schedule_env, marker_home
):
    record_turn_start(marker_home, "session-key", "prompt")

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    # No automatic retry, but preserve the interrupted prompt for review.
    assert read_turn_marker(marker_home, "session-key") is not None


def test_stale_marker_is_cleared_not_continued(schedule_env, marker_home, monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "old prompt")
    monkeypatch.setattr(
        server, "time", types.SimpleNamespace(time=lambda: time.time() + 3600)
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    assert read_turn_marker(marker_home, "session-key") is None


def test_config_widens_freshness_window(emits, schedule_env, marker_home, monkeypatch):
    record_turn_start(marker_home, "session-key", "old prompt")
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "desktop": {
                "auto_continue": {
                    "enabled": True,
                    "freshness_minutes": 120,
                }
            }
        },
    )
    monkeypatch.setattr(
        server, "time", types.SimpleNamespace(time=lambda: time.time() + 3600)
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is not None
    assert len(schedule_env) == 1


def test_exhausted_attempts_break_the_loop(schedule_env, marker_home, monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "crashy prompt", attempts=2)

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    assert read_turn_marker(marker_home, "session-key") is None


def test_disabled_by_config(schedule_env, marker_home, monkeypatch):
    record_turn_start(marker_home, "session-key", "prompt")
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": False}}},
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env


def test_no_marker_means_no_continuation(schedule_env, marker_home):
    assert server._maybe_schedule_auto_continue("sid", _session(), "session-key") is None
    assert not schedule_env


def test_running_session_wins_over_continuation(
    emits, schedule_env, marker_home, monkeypatch
):
    """A real user prompt that raced the kickoff keeps its turn; the marker is
    left for that turn's own conclusion to clear."""
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "prompt")
    session = _session(running=True)

    result = server._maybe_schedule_auto_continue("sid", session, "session-key")

    # Scheduled (the descriptor is returned), but the kickoff bailed.
    assert result is not None
    assert not schedule_env
    assert session["_auto_continue_scheduled"] is False
    assert read_turn_marker(marker_home, "session-key") is not None
    # Nothing left behind for the racing user turn to inherit.
    assert "_auto_continue_attempt" not in session
    assert "_auto_continue_prompt" not in session


def test_double_schedule_is_guarded(
    emits, schedule_env, marker_home, monkeypatch
):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "prompt")
    session = _session()

    first = server._maybe_schedule_auto_continue("sid", session, "session-key")
    second = server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert first is not None
    assert second is None
    assert len(schedule_env) == 1


def test_failed_agent_build_leaves_marker_for_retry(
    emits, schedule_env, marker_home, monkeypatch
):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": True}}},
    )
    record_turn_start(marker_home, "session-key", "prompt")
    monkeypatch.setattr(
        server,
        "_wait_agent",
        lambda session, rid, timeout=30.0: {"error": {"message": "boom"}},
    )
    session = _session()

    result = server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert result is not None
    assert not schedule_env
    assert session["_auto_continue_scheduled"] is False
    assert read_turn_marker(marker_home, "session-key") is not None


# ── End to end: continuation runs a real turn and clears the marker ────


