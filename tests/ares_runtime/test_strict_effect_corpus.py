"""Strict effect-argument corpus gate.

`ARES_STRICT_EFFECT_TOOL_ARGS_V1=1` installs exact-shape validation in front of
effectful tool dispatch: unknown fields, missing required fields, and type
coercion are refused before any effect. Without a corpus this gate is a guess,
so this test fixes a representative payload set per effectful schema and asserts
(a) no legitimate call is rejected and (b) malformed calls are rejected with the
typed code. A negative witness confirms the malformed case is refused *because*
the flag is set, not because the boundary rejects it unconditionally.

Payloads are curated from the schemas in `ares_runtime.collaboration`; contents
are inert and no live tool executes.
"""

from __future__ import annotations

import pytest

from ares_runtime.collaboration import _DEFAULT_EFFECT_SCHEMAS, dispatcher_boundary

CORPUS = {
    "write_file": {"path": "notes/audit.md", "content": "# evidence\n"},
    "patch": {"path": "notes/audit.md", "old_string": "a", "new_string": "b"},
    "terminal": {"command": "git status --short", "timeout": 30},
    "execute_code": {"code": "print('ok')"},
    "browser_exec": {"code": "return document.title"},
    "browser_navigate": {"url": "https://example.com/"},
    "browser_type": {"text": "query"},
    "browser_click": {"element": 3},
    "browser_press": {"keys": "Enter"},
    "browser_scroll": {"direction": "down", "amount": 2},
    "browser_back": {},
    "browser_dialog": {"action": "accept"},
    "browser_cdp": {"code": "Runtime.evaluate"},
    "browser_console": {"level": "error"},
    "send_message": {"recipient": "operator", "content": "status"},
    "computer_use": {"action": "capture", "app": "terminal"},
    "image_generate": {"prompt": "a diagram", "aspect_ratio": "16:9"},
    "bfl_flux3_image_to_video": {"prompt": "pan", "input_image": "frame.png"},
    "bfl_flux3_keyframes_to_video": {"prompt": "pan", "input_images": ["a.png", "b.png"]},
    "bfl_flux3_text_to_video": {"prompt": "waves", "duration": 4},
    "bfl_flux3_video_continuation": {"prompt": "continue", "input_video": "clip.mp4"},
}


def _boundary(tool: str, args: dict):
    return dispatcher_boundary(
        tool,
        args,
        mission_ref=None,
        session_id="strict-corpus",
        authorize_permit=False,
        consume_permit=False,
    )


def test_corpus_covers_every_default_schema():
    assert set(CORPUS) == set(_DEFAULT_EFFECT_SCHEMAS)


def test_every_legitimate_corpus_call_is_admitted_under_strict_mode(monkeypatch):
    monkeypatch.setenv("ARES_STRICT_EFFECT_TOOL_ARGS_V1", "1")
    rejected = {}
    for tool, args in CORPUS.items():
        allowed, code, _ = _boundary(tool, args)
        if not allowed:
            rejected[tool] = code
    assert rejected == {}


def test_unknown_field_is_rejected_under_strict_mode(monkeypatch):
    monkeypatch.setenv("ARES_STRICT_EFFECT_TOOL_ARGS_V1", "1")
    allowed, code, _ = _boundary(
        "write_file", {"path": "a.md", "content": "x", "mode": "append"}
    )
    assert (allowed, code) == (False, "UNKNOWN_FIELD")


def test_missing_required_field_is_rejected_under_strict_mode(monkeypatch):
    monkeypatch.setenv("ARES_STRICT_EFFECT_TOOL_ARGS_V1", "1")
    allowed, code, _ = _boundary("write_file", {"path": "a.md"})
    assert (allowed, code) == (False, "MISSING_FIELD")


def test_coercion_is_rejected_under_strict_mode(monkeypatch):
    monkeypatch.setenv("ARES_STRICT_EFFECT_TOOL_ARGS_V1", "1")
    allowed, code, _ = _boundary("terminal", {"command": "ls", "timeout": "30"})
    assert (allowed, code) == (False, "COERCION_REQUIRED")


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "a.md", "content": "x", "mode": "append"},
        {"path": "a.md"},
        {"path": "a.md", "content": 7},
    ],
)
def test_negative_witness_flag_off_admits_the_same_payloads(monkeypatch, payload):
    """Without the flag (and no canary), the boundary is not the strict gate."""
    monkeypatch.delenv("ARES_STRICT_EFFECT_TOOL_ARGS_V1", raising=False)
    allowed, code, _ = _boundary("write_file", payload)
    assert (allowed, code) == (True, None)
