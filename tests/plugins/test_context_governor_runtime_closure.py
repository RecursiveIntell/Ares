import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.context_engine._context_governor import (
    ContextGovernorActivationError,
    ContextGovernorEngine,
)
from plugins.context_engine._context_governor.protocol import (
    ContextGovernorCancellationIndeterminate,
    ContextGovernorProtocolError,
)


def _capabilities():
    return {
        "failure_envelope": {
            "schema": "ContextGovernorFailureV1",
            "flag": "--failure-envelope-v1",
            "stream": "stderr",
        }
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group witness")
def test_timeout_kills_descendant_group_before_return(tmp_path: Path):
    sentinel = tmp_path / "survived"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal,time,pathlib,sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[1]).write_text('escaped')\n"
        "time.sleep(5)\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(5)\n"
    )
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine.binary = sys.executable
    engine.timeout_sec = 0.15
    with pytest.raises(subprocess.TimeoutExpired):
        engine._run_json([str(parent), str(child), str(sentinel)], {})
    time.sleep(1.0)
    assert not sentinel.exists()


def test_certified_windows_is_rejected_before_key_binding(monkeypatch):
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine._unsafe_configured_hmac_path = ""
    engine.binary = sys.executable
    engine.timeout_sec = 1
    engine._key_state = SimpleNamespace(
        active_binding=lambda: pytest.fail("key binding touched")
    )
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ContextGovernorActivationError, match="/proc/self/fd"):
        engine.probe_activation()


def _activation_engine():
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    engine._pending_admission = {
        "receipt_id": "ctxr_test",
        "pending_info": {
            "expected_compacted_messages": [{"role": "user", "content": "kept"}],
            "generation": 1,
        },
        "savings_pct": 50.0,
        "exact_fallback_available": True,
    }
    engine.store_dir = Path("/tmp/unused-cg-test-store")
    engine.compression_count = 0
    engine.last_receipt_id = None
    engine.last_compaction_metrics = {}
    engine._llm_checkpoint_count = 0
    engine._ineffective_compression_count = 0
    engine._last_compression_savings_pct = 100.0
    engine.last_error = None
    engine.set_activation_status = lambda **kwargs: None
    engine._committed_pending_projection = lambda committed, expected: expected
    return engine


def test_activation_timeout_replays_exact_request_once_and_clears_pending():
    engine = _activation_engine()
    calls = []

    def run(args, payload):
        calls.append((list(args), json.loads(json.dumps(payload))))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 1)
        return {
            "schema": "ReceiptActivationResultV2",
            "receipt_id": "ctxr_test",
            "activated": True,
            "verified": True,
            "already_activated": True,
        }

    engine._run_certified_json = run
    assert engine.commit_pending_compression([{"role": "user", "content": "kept"}])
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert engine._pending_admission is None
    assert engine.last_compaction_metrics["activation_replayed_after_timeout"] is True


def test_failed_activation_replay_retains_pending_for_reconciliation():
    engine = _activation_engine()
    calls = 0

    def run(args, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ContextGovernorCancellationIndeterminate("uncertain")
        raise ContextGovernorProtocolError("replay failed")

    engine._run_certified_json = run
    with pytest.raises(ContextGovernorProtocolError, match="replay failed"):
        engine.commit_pending_compression([{"role": "user", "content": "kept"}])
    assert calls == 2
    assert engine._pending_admission["receipt_id"] == "ctxr_test"
