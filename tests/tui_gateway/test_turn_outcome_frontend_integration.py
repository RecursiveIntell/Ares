"""Fresh backend wire results are consumed by the actual whole-plugin collector."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tui_gateway import server
from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
from tests.tui_gateway.test_prompt_recovery_contract import _session, turn_env
from tests.tui_gateway.test_turn_outcome_projection import (
    host_owner,
    projection_env,
    terminal,
    test_real_dispatch_emit_terminal_resume_survives_194_to_15_shrink as generate_dispatch_wire,
)

BRIDGE = Path(__file__).resolve().parents[2] / "apps/desktop/src/plugins/hermes-bots/tests/group-turn-backend-bridge.mjs"


def run_frontend(packet, tmp_path):
    path = tmp_path / "fresh-backend-wire.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    node = shutil.which("node")
    assert node, "Node is required for the frontend/backend integration gate"
    result = subprocess.run([node, str(BRIDGE), str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["submits"] == 1
    assert receipt["accepted_turn"] == packet["submit_ack"]["result"]["accepted_turn"]


def test_actual_compute_dispatch_shrink_terminal_resume_to_frontend(tmp_path, monkeypatch, turn_env, projection_env):
    # Reuse the real dispatch/provider/terminal serialization path, not the frozen
    # recorded fixture. Its output carries this run's admission and boot IDs.
    generate_dispatch_wire(tmp_path, monkeypatch, turn_env, projection_env)
    packet = json.loads((tmp_path / "wire-contract.json").read_text(encoding="utf-8"))
    packet["expected"] = {"state": "complete", "text": "owned new answer"}
    run_frontend(packet, tmp_path)


@pytest.mark.parametrize("state", ["error", "interrupted", "unavailable"])
def test_actual_host_terminal_or_restart_to_frontend(tmp_path, monkeypatch, projection_env, state):
    session = _session(running=True, history=[{
        "role": "assistant", "content": "tempting stale transcript", "row_id": 999999,
        "timestamp": 999999,
    }])
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    if state == "unavailable":
        # Process-local projection disappears after restart; the old accepted
        # identity and attractive transcript cannot authorize a new reply.
        session.pop("_turn_outcomes")
        session["running"] = False
    else:
        host._complete_turn(terminal(ref, [{"text": "partial", "status": state,
            "error": "controlled provider failure"}], state=state))
        wait_for_terminal_projection(host)
    packet = {"submit_ack": host._test_submit_ack,
        "resume_result": server._live_session_payload("s", session, omit_messages=False),
        "expected": {"state": state}}
    run_frontend(packet, tmp_path)
