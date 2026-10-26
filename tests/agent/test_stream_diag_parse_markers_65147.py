"""Port of upstream #65147: jiter SSE parse ValueErrors are transient provider failures.

Incident: 2026-10-26 session 20261026_041441_58fcfd (claude-opus-5-5,
provider anthropic) aborted a 356-message turn with
``ValueError: key must be a string at line 1 column 28`` raised from
``anthropic/lib/streaming/_messages.py: accumulate_event`` via
``jiter.from_json(json_buf, partial_mode=True)``. The fork's
``_is_provider_stream_parse_error`` only matched
``"expected ident at line"``, so the error fell through to the
``is_local_validation_error`` abort as a non-retryable client error
instead of taking the transient stream-retry / fallback path.

Behavior contracts (not snapshots):
- every jiter/serde stream-parse shape in PROVIDER_STREAM_PARSE_MARKERS
  → NOT a local validation error (retryable);
- unrelated ValueError/TypeError text → still local (abort preserved);
- UnicodeEncodeError / JSONDecodeError / SSLError exclusions preserved;
- the run_agent gate stays scoped to api_mode == "anthropic_messages";
- the user-facing summary collapses the shape to the malformed-stream
  one-liner.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.stream_diag import (
    PROVIDER_STREAM_PARSE_MARKERS,
    is_provider_stream_parse_error,
)

# The exact incident string plus the rest of the upstream marker vocabulary.
JITER_SHAPES = [
    "key must be a string at line 1 column 28",  # the observed incident
    "key must be a string at line 1 column 2",
    "expected ident at line 1 column 149",
    "expected value at line 1 column 1",
    "EOF while parsing a value at line 1 column 5",
    "EOF while parsing a string at line 1 column 12",
    "EOF while parsing a list at line 1 column 9",
    "EOF while parsing an object at line 1 column 9",
    "trailing characters at line 1 column 9",
    "trailing comma at line 1 column 8",
    "expected `,` or `}` at line 1 column 8",
    "expected `,` or `]` at line 1 column 8",
    "expected `:` at line 1 column 7",
    "invalid escape at line 1 column 4",
    "invalid number at line 1 column 7",
]


def _mirror_is_local_validation_error(api_error: BaseException) -> bool:
    """Mirror of agent/conversation_loop.py's predicate (imports the real helper).

    Kept in lock-step with the source: if you change one, change both — or,
    better, extract the predicate so tests import it directly.
    """
    import ssl

    return (
        isinstance(api_error, (ValueError, TypeError))
        and not isinstance(api_error, (UnicodeEncodeError, json.JSONDecodeError))
        and not isinstance(api_error, ssl.SSLError)
        and not is_provider_stream_parse_error(api_error)
        and not (
            isinstance(api_error, TypeError)
            and "nonetype" in str(api_error).lower()
            and "not iterable" in str(api_error).lower()
        )
    )


@pytest.mark.parametrize("msg", JITER_SHAPES)
def test_jiter_stream_parse_shapes_are_not_local_validation(msg: str) -> None:
    assert is_provider_stream_parse_error(ValueError(msg)) is True
    assert _mirror_is_local_validation_error(ValueError(msg)) is False


def test_markers_cover_all_tested_shapes() -> None:
    lowered = [m.lower() for m in PROVIDER_STREAM_PARSE_MARKERS]
    for msg in JITER_SHAPES:
        assert any(m in msg.lower() for m in lowered), f"uncovered shape: {msg!r}"


def test_unrelated_valueerror_with_line_suffix_stays_local() -> None:
    assert _mirror_is_local_validation_error(ValueError("bad config at line 3 column 1")) is True
    assert _mirror_is_local_validation_error(ValueError("bad arg")) is True
    assert _mirror_is_local_validation_error(TypeError("tools must be a list")) is True


def test_jsondecode_unicode_ssl_exclusions_preserved() -> None:
    try:
        json.loads("{not valid json")
    except json.JSONDecodeError as exc:
        assert is_provider_stream_parse_error(exc) is False
        assert _mirror_is_local_validation_error(exc) is False
    else:  # pragma: no cover
        raise AssertionError("json.loads should have raised")
    import ssl

    assert _mirror_is_local_validation_error(UnicodeEncodeError("utf-8", "x", 0, 1, "r")) is False
    assert _mirror_is_local_validation_error(ssl.SSLError("certificate verify failed")) is False


class TestRunAgentGate:
    def test_gate_fires_only_for_anthropic_messages_mode(self) -> None:
        from types import SimpleNamespace

        from run_agent import AIAgent

        err = ValueError("key must be a string at line 1 column 28")
        assert AIAgent._is_provider_stream_parse_error(SimpleNamespace(api_mode="anthropic_messages"), err) is True
        assert AIAgent._is_provider_stream_parse_error(SimpleNamespace(api_mode="chat_completions"), err) is False

    def test_summary_collapses_malformed_stream(self) -> None:
        from run_agent import AIAgent

        out = AIAgent._summarize_api_error(ValueError("key must be a string at line 1 column 28"))
        assert out.startswith("Malformed provider streaming response:")


class TestIncidentStreamParseRetriesThroughRealPath:
    """Real-path regression for the 2026-10-26 incident.

    Injects the exact incident ValueError through
    ``AIAgent._interruptible_streaming_api_call`` with a failing-then-healthy
    mock stream pair, exercising the production retry gate
    (``chat_completion_helpers`` → ``AIAgent._is_provider_stream_parse_error``)
    rather than a mirrored predicate. Without the fix the error is not
    recognized as transient and propagates; with it the call recovers on
    the second stream.
    """

    @patch("run_agent.AIAgent._rebuild_anthropic_client")
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    def test_incident_jiter_error_retries_before_delivery(
        self, mock_replace, mock_rebuild, monkeypatch
    ) -> None:
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://api.anthropic.com",
            provider="anthropic",
            model="claude-opus-5-5",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.api_mode = "anthropic_messages"
        agent._interrupt_requested = False
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        class _BadStream:
            response = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                raise ValueError("key must be a string at line 1 column 28")

        final_message = SimpleNamespace(content=[], stop_reason="end_turn")
        good_stream = MagicMock()
        good_stream.__enter__ = MagicMock(return_value=good_stream)
        good_stream.__exit__ = MagicMock(return_value=False)
        good_stream.__iter__ = MagicMock(return_value=iter([]))
        good_stream.get_final_message.return_value = final_message

        agent._anthropic_client = MagicMock()
        agent._anthropic_client.messages.stream.side_effect = [
            _BadStream(),
            good_stream,
        ]
        agent._create_request_anthropic_client = lambda *a, **k: agent._anthropic_client  # noqa: E731

        # Spy the silent-retry signal: the fixed gate takes the in-stream
        # retry (emits one drop, mid_tool_call=False); without the fix the
        # error bypasses the streaming retry entirely, so no drop is emitted
        # and recovery — if any — comes from a different layer.
        drops = []
        orig_emit_drop = agent._emit_stream_drop

        def _spy_drop(**kwargs):
            drops.append(kwargs)
            return orig_emit_drop(**kwargs)

        monkeypatch.setattr(agent, "_emit_stream_drop", _spy_drop)

        response = agent._interruptible_streaming_api_call({})

        assert response is final_message
        assert agent._anthropic_client.messages.stream.call_count == 2
        assert len(drops) == 1
        assert drops[0].get("mid_tool_call") is False
