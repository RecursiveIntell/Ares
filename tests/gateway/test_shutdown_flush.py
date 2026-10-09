"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.shutdown_flush import (
    _serialise_value,
    flush_pending_to_file,
    recover_pending_to_db,
)


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def test_flush_writes_string_pending_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"agent:main:telegram:supergroup:123": "hello world"}
    count = flush_pending_to_file(pending, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_key"] == "agent:main:telegram:supergroup:123"
    assert payload["reason"] == "shutdown"
    assert payload["data"]["text"] == "hello world"
    assert ":" not in files[0].name
    assert "telegram" not in files[0].name


def test_flush_writes_message_event_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MagicMock()
    event.text = "user message"
    event.session_id = "20260728_120000_abc"
    event.platform = "telegram"
    event.sender_id = "456"
    event.sender_name = "Alice"
    event.reply_to = None
    event.media = None
    event.raw_event = None

    count = flush_pending_to_file({"session_key_1": event}, reason="adapter_shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "user message"
    assert payload["data"]["session_id"] == "20260728_120000_abc"


def test_recover_inserts_via_append_message_and_deletes_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    ts = int(time.time())
    # Write a flush file with session_id
    payload = {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown",
        "ts": ts,
        "data": {
            "text": "lost message",
            "session_id": "20260728_120000_abc",
        },
    }
    flush_file = flush_dir / "test_session_123.json"
    flush_file.write_text(json.dumps(payload), encoding="utf-8")

    mock_db = MagicMock()
    count = recover_pending_to_db(mock_db)

    assert count == 1
    mock_db.append_message.assert_called_once_with(
        session_id="20260728_120000_abc",
        role="user",
        content="lost message",
        timestamp=ts,
    )
    assert not flush_file.exists()


def test_recover_closes_owned_db_when_unexpected_exception_escapes(
    tmp_path, monkeypatch
):
    """Owned SessionDB must close even when recovery is interrupted."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    (flush_dir / "pending.json").write_text(
        json.dumps(
            {
                "session_key": "agent:main:telegram:123",
                "data": {"text": "message", "session_id": "sid"},
            }
        ),
        encoding="utf-8",
    )

    class InterruptingDB:
        closed = False

        def append_message(self, **_kwargs):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    db = InterruptingDB()
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

    with pytest.raises(KeyboardInterrupt):
        recover_pending_to_db()

    assert db.closed is True


def test_serialise_object_with_text():
    obj = MagicMock()
    obj.text = "msg"
    obj.session_id = "sid"
    obj.platform = None
    obj.sender_id = None
    obj.sender_name = None
    obj.reply_to = None
    obj.media = None
    obj.raw_event = None
    result = _serialise_value(obj)
    assert result is not None
    assert result["text"] == "msg"
    assert result["session_id"] == "sid"


def test_get_flush_dir_uses_get_hermes_home(tmp_path, monkeypatch):
    """Flush dir must use get_hermes_home(), not hardcoded Path.home()."""
    import gateway.shutdown_flush as mod

    captured = {}

    def fake_get_hermes_home():
        from pathlib import Path
        captured["called"] = True
        return tmp_path

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", fake_get_hermes_home
    )
    result = mod._get_flush_dir()
    assert captured.get("called") is True
    assert result == tmp_path / "pending_messages"




# SD05: expected kwargs below are independently specified contract fixtures,
# never computed by the production transcript mapper.
_SD05_SUPPORTED_FIELD_CASES = [
    pytest.param(
        {
            "role": "assistant",
            "content": None,
            "tool_name": "fixture_tool",
            "tool_calls": [{"id": "call-fixture-1", "type": "function", "function": {"name": "fixture_tool", "arguments": "{\"x\":1}"}}],
            "tool_call_id": "assistant-call-id",
            "reasoning": "assistant reasoning",
            "reasoning_content": "reasoning content",
            "reasoning_details": [{"type": "text", "text": "detail"}],
            "codex_reasoning_items": [{"type": "reasoning", "id": "r1"}],
            "codex_message_items": [{"type": "message", "id": "m1"}],
            "platform_message_id": "platform-assistant",
            "message_id": "ignored-fallback",
            "observed": 1,
            "timestamp": 0,
            "api_content": "",
            "display_kind": "internal_notification",
            "display_metadata": {"source": "fixture", "ordinal": 1},
        },
        {
            "session_id": "sd05-session",
            "role": "assistant",
            "content": "",
            "tool_name": "fixture_tool",
            "tool_calls": [{"id": "call-fixture-1", "type": "function", "function": {"name": "fixture_tool", "arguments": "{\"x\":1}"}}],
            "tool_call_id": "assistant-call-id",
            "reasoning": "assistant reasoning",
            "reasoning_content": "reasoning content",
            "reasoning_details": [{"type": "text", "text": "detail"}],
            "codex_reasoning_items": [{"type": "reasoning", "id": "r1"}],
            "codex_message_items": [{"type": "message", "id": "m1"}],
            "platform_message_id": "platform-assistant",
            "observed": True,
            "timestamp": 1234567,
            "api_content": "",
            "display_kind": "internal_notification",
            "display_metadata": {"source": "fixture", "ordinal": 1},
        },
        id="assistant",
    ),
    pytest.param(
        {
            "role": "tool",
            "content": "tool output",
            "tool_name": "fixture_tool",
            "tool_call_id": "call-fixture-1",
            "reasoning": "must not persist",
            "reasoning_content": "must not persist",
            "reasoning_details": [{"text": "must not persist"}],
            "codex_reasoning_items": [{"id": "must-not-persist"}],
            "codex_message_items": [{"id": "must-not-persist"}],
            "platform_message_id": "",
            "message_id": "platform-tool-fallback",
            "observed": [],
            "timestamp": 50.0,
            "api_content": {"not": "a string"},
            "display_kind": "tool_result",
            "display_metadata": {"source": "fixture", "ordinal": 2},
        },
        {
            "session_id": "sd05-session",
            "role": "tool",
            "content": "tool output",
            "tool_name": "fixture_tool",
            "tool_calls": None,
            "tool_call_id": "call-fixture-1",
            "reasoning": None,
            "reasoning_content": None,
            "reasoning_details": None,
            "codex_reasoning_items": None,
            "codex_message_items": None,
            "platform_message_id": "platform-tool-fallback",
            "observed": False,
            "timestamp": 50.0,
            "api_content": None,
            "display_kind": "tool_result",
            "display_metadata": {"source": "fixture", "ordinal": 2},
        },
        id="tool",
    ),
    pytest.param(
        {
            "role": "user",
            "content": "user message",
            "reasoning": "must not persist",
            "reasoning_content": "must not persist",
            "reasoning_details": [{"text": "must not persist"}],
            "codex_reasoning_items": [{"id": "must-not-persist"}],
            "codex_message_items": [{"id": "must-not-persist"}],
            "message_id": "platform-user-fallback",
            "observed": False,
            "timestamp": None,
            "api_content": "user message\n\nfixture context",
            "display_kind": "user_input",
            "display_metadata": {"source": "fixture", "ordinal": 3},
        },
        {
            "session_id": "sd05-session",
            "role": "user",
            "content": "user message",
            "tool_name": None,
            "tool_calls": None,
            "tool_call_id": None,
            "reasoning": None,
            "reasoning_content": None,
            "reasoning_details": None,
            "codex_reasoning_items": None,
            "codex_message_items": None,
            "platform_message_id": "platform-user-fallback",
            "observed": False,
            "timestamp": 1234567,
            "api_content": "user message\n\nfixture context",
            "display_kind": "user_input",
            "display_metadata": {"source": "fixture", "ordinal": 3},
        },
        id="user",
    ),
]


class _Sd05RecordingDb:
    """Only record supplied kwargs; never construct or open a real DB."""

    def __init__(self):
        self.rows = []

    def append_message(self, **kwargs):
        self.rows.append(kwargs)


def _sd05_write_cap_spool(tmp_path, monkeypatch, message, session_id="sd05-session"):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    payload = {
        "session_key": session_id,
        "reason": "transcript_cap_drop",
        "ts": 1234567,
        "data": {"session_id": session_id, "message": message},
    }
    original = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    assert len(original) < 4096
    path = flush_dir / "sd05-cap.json"
    path.write_bytes(original)
    return path, original


@pytest.mark.parametrize("message, expected", _SD05_SUPPORTED_FIELD_CASES)
def test_recover_transcript_cap_drop_preserves_full_message_fields(
    tmp_path, monkeypatch, message, expected
):
    """Startup restores supported metadata with existing content/time fallbacks."""
    path, original = _sd05_write_cap_spool(tmp_path, monkeypatch, message)

    class AppendBeforeUnlinkDb(_Sd05RecordingDb):
        def append_message(self, **kwargs):
            assert path.exists()
            assert path.read_bytes() == original
            super().append_message(**kwargs)

    db = AppendBeforeUnlinkDb()
    assert recover_pending_to_db(db) == 1
    assert db.rows == [expected]
    assert not path.exists()


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"role": "user", "content": "message"}, id="missing"),
        pytest.param({"role": "user", "content": "message", "timestamp": None}, id="none"),
        pytest.param({"role": "user", "content": "message", "timestamp": 0}, id="zero"),
    ],
)
def test_recover_transcript_cap_drop_uses_payload_timestamp(
    tmp_path, monkeypatch, message
):
    path, _original = _sd05_write_cap_spool(tmp_path, monkeypatch, message)
    db = _Sd05RecordingDb()
    assert recover_pending_to_db(db) == 1
    assert db.rows == [{
        "session_id": "sd05-session",
        "role": "user",
        "content": "message",
        "tool_name": None,
        "tool_calls": None,
        "tool_call_id": None,
        "reasoning": None,
        "reasoning_content": None,
        "reasoning_details": None,
        "codex_reasoning_items": None,
        "codex_message_items": None,
        "platform_message_id": None,
        "observed": False,
        "timestamp": 1234567,
        "api_content": None,
        "display_kind": None,
        "display_metadata": None,
    }]
    assert not path.exists()


@pytest.mark.parametrize("content", [None, "", [], {}], ids=["none", "empty-string", "empty-list", "empty-dict"])
def test_recover_transcript_cap_drop_keeps_falsy_content_compatibility(
    tmp_path, monkeypatch, content
):
    path, _original = _sd05_write_cap_spool(
        tmp_path, monkeypatch, {"role": "assistant", "content": content, "timestamp": 9}
    )
    db = _Sd05RecordingDb()
    assert recover_pending_to_db(db) == 1
    assert db.rows[0]["content"] == ""
    assert db.rows[0]["timestamp"] == 9
    assert not path.exists()


def test_recover_transcript_cap_drop_failure_preserves_spool_bytes(tmp_path, monkeypatch):
    message = {"role": "tool", "content": "result", "tool_call_id": "call-fixture-1", "api_content": "exact bytes"}
    path, original = _sd05_write_cap_spool(tmp_path, monkeypatch, message)

    class FailingRecordingDb(_Sd05RecordingDb):
        def append_message(self, **kwargs):
            assert path.read_bytes() == original
            super().append_message(**kwargs)
            raise RuntimeError("sd05 inert append failure")

    db = FailingRecordingDb()
    # Preserve the current BaseException-before-Exception propagation behavior.
    with pytest.raises(RuntimeError, match="sd05 inert append failure"):
        recover_pending_to_db(db)
    assert len(db.rows) == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "session_id, message",
    [
        pytest.param("", {"role": "user", "content": "kept"}, id="missing-session"),
        pytest.param("sd05-session", ["invalid message"], id="non-dict-message"),
    ],
)
def test_recover_transcript_cap_drop_invalid_payload_retains_spool(
    tmp_path, monkeypatch, session_id, message
):
    path, original = _sd05_write_cap_spool(tmp_path, monkeypatch, message, session_id)
    db = _Sd05RecordingDb()
    assert recover_pending_to_db(db) == 0
    assert db.rows == []
    assert path.read_bytes() == original
