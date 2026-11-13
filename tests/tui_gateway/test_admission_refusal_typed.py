"""An admission refusal must carry a retryable classification.

`turn.error` with message "session busy" is what the desktop used to render as
a fatal turn failure ("Turn failed" notification + error card + toast) for a
message that was never attempted. The host now marks the frame as a retryable
admission refusal so clients can reconcile instead of failing the turn.
"""

import tui_gateway.compute_host as compute_host


def _host(admitted: bool):
    host = object.__new__(compute_host.ComputeHost)
    host._sessions = {}
    frames: list[dict] = []
    host.emit = frames.append
    host._admit_real_turn = lambda frame: (admitted, "req-1")

    return host, frames


def test_refused_admission_is_classified_retryable():
    host, frames = _host(admitted=False)

    host._handle_turn_start({"sid": "s1", "text": "hello"})

    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "turn.error"
    assert frame["message"] == "session busy"
    assert frame["reason"] == "admission_refused_busy"
    assert frame["error_surface"] == {
        "layer": "admission",
        "code": "session_busy",
        "retryable": True,
    }


def test_admitted_turn_emits_no_refusal():
    host, frames = _host(admitted=True)

    host._handle_turn_start({"sid": "s1", "text": "hello"})

    assert frames == []
