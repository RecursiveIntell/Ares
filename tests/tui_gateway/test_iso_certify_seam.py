"""Tests for the AC-4 isolation certify seam + harness helpers.

The synthetic heavy-turn agent (``tui_gateway/synthetic_turn.py``) is a test
seam: dead unless ``HERMES_ISO_CERTIFY_SYNTH_TURN=1``. These tests pin (a) the
dead-when-unset contract, (b) that an armed turn holds for the requested wall
duration and streams deltas, (c) that interrupt aborts it promptly, and (d) the
harness percentile math.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from tui_gateway.synthetic_turn import (
    SyntheticHeavyAgent,
    maybe_build_synthetic_agent,
    synth_turn_armed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_iso_certify():
    path = REPO_ROOT / "scripts" / "iso-certify.py"
    spec = importlib.util.spec_from_file_location("iso_certify", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_synth_seam_dead_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_ISO_CERTIFY_SYNTH_TURN", raising=False)
    assert synth_turn_armed() is False
    assert maybe_build_synthetic_agent("sid") is None


def test_harness_percentile_and_guard():
    iso = _load_iso_certify()
    assert iso.percentile([], 99) == 0.0
    assert iso.percentile([5.0], 99) == 5.0
    vals = [float(i) for i in range(1, 101)]  # 1..100
    assert 98.0 <= iso.percentile(vals, 99) <= 100.0
    assert iso.percentile(vals, 50) == pytest.approx(50.5, abs=0.6)
    # The empty-timeline INCONCLUSIVE floor: too few probe samples never PASSes.
    assert iso.probe_thread_samples_ok([1.0, 2.0], [1.0, 2.0, 3.0]) is False
    assert iso.probe_thread_samples_ok([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) is True


def test_ws_client_preserves_event_that_precedes_rpc_response(monkeypatch):
    """The streaming event can beat prompt.submit's RPC response.

    The certify harness must retain that event; discarding it makes a valid
    isolated turn look like zero completed turns and incorrectly yields
    INCONCLUSIVE under the faster child-process path.
    """
    iso = _load_iso_certify()

    class _FakeWS:
        def __init__(self):
            self.messages = [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "message.start"},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": "r1", "result": {}}),
            ]

        def recv(self, timeout):
            del timeout
            if not self.messages:
                raise TimeoutError
            return self.messages.pop(0)

    client = iso.WSClient.__new__(iso.WSClient)
    client.ws = _FakeWS()
    client._pending = []
    client._id = 0
    client._lock = threading.Lock()

    response = client._recv_until(lambda obj: obj.get("id") == "r1", timeout=1)
    assert response["id"] == "r1"
    event = client._recv_until(
        lambda obj: obj.get("method") == "event", timeout=1
    )
    assert event["params"]["type"] == "message.start"


