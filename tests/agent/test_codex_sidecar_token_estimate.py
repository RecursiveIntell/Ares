"""F05: replayed Codex reasoning sidecars are charged at their calibrated rate."""
from agent.model_metadata import estimate_messages_tokens_rough


def _assistant(**extra):
    return {"role": "assistant", "content": "done", **extra}


def test_codex_sidecars_are_charged_but_not_at_the_text_rate():
    blob = [{"type": "reasoning", "encrypted_content": "e" * 40_000}]
    base = estimate_messages_tokens_rough([_assistant()])
    with_items = estimate_messages_tokens_rough([_assistant(codex_reasoning_items=blob)])
    text_rate = estimate_messages_tokens_rough([_assistant(reasoning=str(blob))]) - base

    added = with_items - base
    assert added > 0, "replayed encrypted reasoning is billed and must be counted"
    assert added * 3 < text_rate, (added, text_rate)
    assert added * 5 > text_rate, (added, text_rate)


def test_message_items_share_the_sidecar_rate():
    items = [{"type": "message", "content": "m" * 8_000}]
    base = estimate_messages_tokens_rough([_assistant()])
    added = estimate_messages_tokens_rough([_assistant(codex_message_items=items)]) - base
    assert 0 < added < 1_000


def test_plain_text_estimate_is_unchanged():
    msg = {"role": "user", "content": "x" * 4_000}
    assert estimate_messages_tokens_rough([msg]) >= 1_000
