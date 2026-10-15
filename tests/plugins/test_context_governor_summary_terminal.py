"""Compression-summary admission requires explicit provider terminal evidence."""
from types import SimpleNamespace

import pytest

from agent.auxiliary_client import (
    _ChatStreamAccumulator,
    _CodexCompletionsAdapter,
    _recover_aux_response_message,
    _validate_llm_response,
)
from plugins.context_engine._context_governor import ContextGovernorEngine


def _chat_response(
    finish_reason,
    *,
    status=None,
    terminal_observed=None,
    content="a usable fixture summary",
    refusal=None,
    error=None,
):
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=None, refusal=refusal),
        finish_reason=finish_reason,
    )
    if terminal_observed is not None:
        choice.terminal_observed = terminal_observed
    response = SimpleNamespace(choices=[choice])
    if status is not None:
        response.status = status
    if error is not None:
        response.error = error
    return response


@pytest.mark.parametrize(
    "response",
    [
        _chat_response("length"),
        _chat_response("content_filter"),
        _chat_response("tool_calls"),
        _chat_response(None),
        _chat_response("stop", status="incomplete"),
        _chat_response("stop", status="failed"),
        _chat_response("stop", error={"code": "refusal"}),
        _chat_response("stop", refusal="fixture refusal"),
        _chat_response("stop", terminal_observed=False),
    ],
)
def test_compression_task_rejects_unfinished_or_nonfinal_terminal(response):
    with pytest.raises(RuntimeError, match="completed|terminal|incomplete"):
        _validate_llm_response(response, "compression")


def test_noncompression_call_retains_legacy_partial_length_response():
    response = _chat_response("length")
    assert _validate_llm_response(response, "web_extract") is response


def test_responses_recovery_requires_status_or_explicit_completion_reason_for_compression():
    incomplete = SimpleNamespace(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output_text="partial fixture summary",
    )
    recovered_incomplete = _recover_aux_response_message(
        incomplete, require_completed_terminal=True
    )
    assert recovered_incomplete.status == "incomplete"
    assert recovered_incomplete.incomplete_details["reason"] == "max_output_tokens"
    assert recovered_incomplete.choices[0].message.content == "partial fixture summary"
    with pytest.raises(RuntimeError, match="completed|terminal"):
        _validate_llm_response(recovered_incomplete, "compression")

    completed = SimpleNamespace(
        status="completed", output_text="complete fixture summary"
    )
    recovered = _recover_aux_response_message(
        completed, require_completed_terminal=True
    )
    assert recovered.status == "completed"
    assert recovered.choices[0].finish_reason == "stop"
    assert recovered.choices[0].terminal_observed is True

    missing = SimpleNamespace(output_text="text but no completion evidence")
    recovered_missing = _recover_aux_response_message(
        missing, require_completed_terminal=True
    )
    assert recovered_missing.choices[0].finish_reason is None
    assert recovered_missing.choices[0].terminal_observed is False
    with pytest.raises(RuntimeError, match="completed|terminal"):
        _validate_llm_response(recovered_missing, "compression")


def test_stream_eof_without_terminal_is_not_summary_completion():
    accumulator = _ChatStreamAccumulator(model="fixture-model")
    accumulator.feed(
        SimpleNamespace(
            id="fixture-stream",
            model="fixture-model",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(content="partial fixture", tool_calls=[]),
                )
            ],
        )
    )
    partial = accumulator.finish()
    # Preserve the established ordinary chat-loop sentinel; the summary task
    # separately rejects the absent provider terminal proof.
    assert partial.choices[0].finish_reason == "stop"
    assert partial.choices[0].terminal_observed is False
    with pytest.raises(RuntimeError, match="completed|terminal"):
        _validate_llm_response(partial, "compression")
    assert _validate_llm_response(partial, "web_extract") is partial


def test_codex_responses_adapter_preserves_incomplete_status_before_admission():
    message = SimpleNamespace(
        type="message",
        status="in_progress",
        content=[SimpleNamespace(type="output_text", text="partial fixture")],
    )
    final = SimpleNamespace(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output=[message],
        usage=None,
        model="fixture-model",
    )
    real_client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: final)
    )
    adapter = _CodexCompletionsAdapter(real_client, "fixture-model")
    adapted = adapter.create(messages=[{"role": "user", "content": "fixture"}])
    assert adapted.status == "incomplete"
    assert adapted.incomplete_details["reason"] == "max_output_tokens"
    assert adapted.choices[0].finish_reason == "length"
    with pytest.raises(RuntimeError, match="completed|terminal|incomplete"):
        _validate_llm_response(adapted, "compression")


def test_codex_responses_adapter_rejects_completed_compatibility_status_without_terminal():
    final = SimpleNamespace(
        status="completed",
        terminal_observed=False,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="partial fixture")],
            )
        ],
        usage=None,
        model="fixture-model",
    )
    real_client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: final)
    )
    adapter = _CodexCompletionsAdapter(real_client, "fixture-model")
    adapted = adapter.create(messages=[{"role": "user", "content": "fixture"}])
    assert adapted.status == "completed"
    assert adapted.choices[0].finish_reason == "stop"
    assert adapted.choices[0].terminal_observed is False
    with pytest.raises(RuntimeError, match="completed|terminal"):
        _validate_llm_response(adapted, "compression")


def test_codex_raw_responses_consumer_keeps_eof_distinct_from_completed_event():
    from agent.codex_runtime import _consume_codex_event_stream

    eof = _consume_codex_event_stream(
        [SimpleNamespace(type="response.output_text.delta", delta="partial")],
        model="fixture-model",
    )
    assert eof.status == "completed"  # compatibility status is unchanged
    assert eof.terminal_observed is False

    complete = _consume_codex_event_stream(
        [
            SimpleNamespace(type="response.output_text.delta", delta="complete"),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(status="completed", usage=None),
            ),
        ],
        model="fixture-model",
    )
    assert complete.status == "completed"
    assert complete.terminal_observed is True


def test_codex_raw_responses_consumer_preserves_incomplete_terminal_status():
    from agent.codex_runtime import _consume_codex_event_stream

    incomplete = _consume_codex_event_stream(
        [
            SimpleNamespace(
                type="response.output_text.delta", delta="partial"
            ),
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    id="fixture-incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                    usage=None,
                ),
            ),
        ],
        model="fixture-model",
    )
    assert incomplete.status == "incomplete"
    assert incomplete.incomplete_details["reason"] == "max_output_tokens"
    assert incomplete.terminal_observed is False


def _summary_engine(monkeypatch, response):
    import agent.auxiliary_client as auxiliary_client

    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine.model = "fixture-model"
    engine.provider = "fixture-provider"
    engine.base_url = ""
    engine.api_key = ""
    engine.api_mode = "chat_completions"
    engine.last_compaction_metrics = {}
    engine._resolve_moa_runtime = lambda: (
        "fixture-model", "fixture-provider", "", ""
    )
    def fake_call_llm(**kwargs):
        kwargs["route_info"].update(
            provider="fixture-provider", model="fixture-model"
        )
        return response

    monkeypatch.setattr(auxiliary_client, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_task_provider_model",
        lambda *args, **kwargs: (
            "fixture-provider", "fixture-model", "", "", None
        ),
    )
    return engine


def test_summary_admission_rechecks_terminal_at_checkpoint_boundary(monkeypatch):
    incomplete = _chat_response("stop", status="incomplete")
    engine = _summary_engine(monkeypatch, incomplete)
    with pytest.raises(RuntimeError, match="completed|terminal|incomplete"):
        engine._call_summary_llm("synthetic prompt", 80)


def test_completed_summary_remains_admissible_with_route_pinning(monkeypatch):
    response = _chat_response("stop", status="completed")
    engine = _summary_engine(monkeypatch, response)
    result = engine._call_summary_llm("synthetic prompt", 80)
    assert result.content == "a usable fixture summary"
    assert result.route.provider == "fixture-provider"
    assert result.route.model == "fixture-model"
