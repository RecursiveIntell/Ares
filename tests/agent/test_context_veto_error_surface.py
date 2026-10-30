"""Task 3.2 — context-veto failures must not surface as retryable.

Observed 2026-10-26 (session 20261025_161506_c06222): the desktop showed
``retryable=true`` for a turn whose compaction was vetoed by
``lineage_integrity_mismatch``; unchanged retries re-vetoed 5 times in 5
minutes. A resend cannot fix an exact-prefix failure — recovery is a
context-recovery action, not a resend.
"""
from __future__ import annotations

from agent.error_surface import (
    build_error_surface_from_exception,
    build_error_surface_from_result,
)


class TestContextVetoNotRetryable:
    def test_result_lineage_veto_not_retryable(self):
        result = {
            "failed": True,
            "error": (
                "context-governor compaction failed; keeping original messages: "
                "context-governor compact-v2 failed [lineage_integrity_mismatch]"
            ),
            "failure_reason": "api_error",
        }
        surface = build_error_surface_from_result(result, provider="openai-codex")
        assert surface is not None
        assert surface["code"] == "lineage_integrity_mismatch"
        assert surface["retryable"] is False

    def test_exception_preflight_exhausted_not_retryable(self):
        class ContextDispatchError(Exception):
            pass

        exc = ContextDispatchError("CONTEXT_PREFLIGHT_EXHAUSTED")
        surface = build_error_surface_from_exception(exc, provider="openai-codex")
        assert surface is not None
        assert surface["code"] == "context_preflight_exhausted"
        assert surface["retryable"] is False

    def test_exception_lineage_veto_not_retryable(self):
        exc = RuntimeError(
            "compact-v2 failed [lineage_integrity_mismatch]"
        )
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["code"] == "lineage_integrity_mismatch"
        assert surface["retryable"] is False

    def test_other_gateway_exceptions_still_retryable(self):
        # Control: the generic non-API fallback keeps its legacy verdict.
        exc = RuntimeError("some unrelated dispatcher bug")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["retryable"] is True
        assert surface["layer"] == "gateway"

    def test_f1_api_like_veto_stays_non_retryable(self):
        # Verifier F1 (2026-10-26): a lineage veto raised inside an API-like
        # exception with a retryable HTTP status used to keep
        # retryable=True because the classifier's verdict overwrote the
        # veto's. The deterministic context verdict must outrank it.
        class FakeApiError(Exception):
            status_code = 429  # classifier says retryable

        exc = FakeApiError(
            "HTTP 429: context-governor compact-v2 failed [lineage_integrity_mismatch]"
        )
        surface = build_error_surface_from_exception(exc, provider="openai-codex")
        assert surface is not None
        assert surface["retryable"] is False, (
            "F1: API-like veto with retryable status must surface non-retryable"
        )
        assert surface.get("context_veto") is True

    def test_f1_api_like_preflight_exhausted_stays_non_retryable(self):
        class FakeApiError(Exception):
            status_code = 503  # classifier says retryable

        exc = FakeApiError("CONTEXT_PREFLIGHT_EXHAUSTED after 429 retries")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["retryable"] is False

    def test_f1_api_like_without_veto_keeps_classifier_verdict(self):
        # Control: the overwrite fix must not blanket-disable retries for
        # ordinary API errors that merely share the transport.
        class FakeApiError(Exception):
            status_code = 429

        exc = FakeApiError("HTTP 429: rate limited, ordinary provider retry")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["retryable"] is True
        assert "context_veto" not in surface

    def test_nonapi_preflight_text_is_non_retryable(self):
        # Verifier run-2 medium finding: a NON-API exception carrying the
        # Hermes-owned exhausted-preflight sentinel (re-raised as a plain
        # RuntimeError by a transport wrapper) must not surface as
        # gateway/retryable=True — same deterministic-resend rule.
        exc = RuntimeError("CONTEXT_PREFLIGHT_EXHAUSTED")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["code"] == "context_preflight_exhausted"
        assert surface["retryable"] is False

    def test_nonapi_lineage_text_is_non_retryable(self):
        exc = RuntimeError("compact-v2 failed [lineage_integrity_mismatch]")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["retryable"] is False
