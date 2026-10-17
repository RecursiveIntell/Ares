"""Behavioral coverage for manual compression status messages."""

from types import SimpleNamespace

from agent.manual_compression_feedback import (
    describe_compression_lock_skip,
    summarize_manual_compression,
)


def _messages(count: int) -> list[dict[str, str]]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(count)
    ]


def test_failure_reason_redaction_is_forced_at_ui_boundary(monkeypatch):
    messages = _messages(12)
    fake_secret = "sk-proj-" + "X" * 40
    state = SimpleNamespace(
        _last_compress_aborted=True,
        _last_summary_fallback_used=False,
        _last_summary_error=f"provider rejected OPENAI_API_KEY={fake_secret}",
    )
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False, raising=False)

    feedback = summarize_manual_compression(
        messages,
        list(messages),
        120_000,
        120_000,
        compression_state=state,
    )

    assert fake_secret not in feedback["note"]
    assert "OPENAI_API_KEY=" in feedback["note"]


def test_fallback_compression_reports_dropped_message_count():
    before = _messages(12)
    after = before[:2] + before[-2:]
    state = SimpleNamespace(
        _last_compress_aborted=False,
        _last_summary_fallback_used=True,
        _last_summary_dropped_count=8,
        _last_summary_error="summary provider returned an invalid response",
    )

    feedback = summarize_manual_compression(
        before,
        after,
        120_000,
        40_000,
        compression_state=state,
    )

    assert feedback["aborted"] is False
    assert feedback["fallback_used"] is True
    assert feedback["headline"] == "Compressed with fallback: 12 → 4 messages"
    assert "receipt-backed deterministic compression" in feedback["note"]
    assert "removed 8 message(s)" in feedback["note"]
    assert "invalid response" in feedback["note"]


def test_lineage_integrity_abort_feedback_does_not_claim_summary_failure_or_recovery():
    messages = _messages(12)
    state = SimpleNamespace(
        _last_compress_aborted=True,
        _last_summary_fallback_used=False,
        _last_summary_error="context-governor compact-v2 failed [lineage_integrity_mismatch]",
        last_outcome={
            "kind": "compaction_failed_closed",
            "source_operation": "compact-v2",
            "source_error_code": "lineage_integrity_mismatch",
        },
        automatic_rebase_block_reason=lambda: (
            "CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH"
        ),
    )

    feedback = summarize_manual_compression(
        messages,
        list(messages),
        120_000,
        120_000,
        compression_state=state,
    )

    assert feedback["aborted"] is True
    assert "lineage integrity mismatch" in feedback["note"].lower()
    assert "no compacted transcript was committed" in feedback["note"].lower()
    assert "automatic durable-state rebase is blocked" in feedback["note"].lower()
    assert "Summary generation failed" not in feedback["note"]
    assert "Run /compress to retry" not in feedback["note"]


def test_persistent_veto_does_not_mislabel_later_unrelated_abort():
    messages = _messages(12)
    state = SimpleNamespace(
        _last_compress_aborted=True,
        _last_summary_fallback_used=False,
        _last_summary_error="context-governor compact-v2 timed out",
        last_outcome={
            "kind": "compaction_failed_closed",
            "source_operation": "compact-v2",
            "source_error_code": None,
        },
        automatic_rebase_block_reason=lambda: (
            "CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH"
        ),
    )

    feedback = summarize_manual_compression(
        messages,
        list(messages),
        120_000,
        120_000,
        compression_state=state,
    )

    note = feedback["note"].lower()
    assert "unresolved lineage integrity mismatch remains recorded" in note
    assert "automatic durable-state rebase remains blocked" in note
    assert "this attempt's abort cause is separate" in note
    assert "refused this compression attempt" not in note
    assert "reason: context-governor compact-v2 timed out" in note


def test_generic_abort_feedback_qualifies_context_availability():
    messages = _messages(12)
    state = SimpleNamespace(
        _last_compress_aborted=True,
        _last_summary_fallback_used=False,
        _last_summary_error="summary provider unavailable",
    )

    feedback = summarize_manual_compression(
        messages,
        list(messages),
        120_000,
        120_000,
        compression_state=state,
    )

    assert "no compacted transcript was committed" in feedback["note"].lower()
    assert "subject to model-window admission" in feedback["note"].lower()
    assert "retrying /compress may repeat" in feedback["note"].lower()
    assert "Summary generation failed" not in feedback["note"]
