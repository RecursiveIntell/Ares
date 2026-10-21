"""One real agent turn must produce one settled execution record."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import middleware
from hermes_state import SessionDB
from plugins.dispatch_governor import DispatchGovernor
from tests.ares_runtime.test_physical_http_transport import client, endpoint  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
ORACLE = ROOT / "scripts" / "verify_execution_record.py"


@pytest.fixture(autouse=True)
def _disable_automatic_title_provider(monkeypatch):
    monkeypatch.setattr(
        "agent.title_generator.maybe_auto_title", lambda *_a, **_kw: None
    )


@pytest.fixture
def live_turn(agent, tmp_path, endpoint, monkeypatch):
    from ares_runtime import local_runtime

    url, bodies, statuses = endpoint
    db = SessionDB(db_path=tmp_path / "golden.db")
    db.create_session(agent.session_id, source="cli", profile_name="p1", model="local")
    agent._session_db = db
    agent._session_db_created = True
    agent._cached_system_prompt = "Trusted instructions"
    agent.provider = "openai"
    agent.model = "local"
    agent.api_mode = "chat_completions"
    agent.base_url = url
    agent.context_rebase_enabled = True
    agent.compression_enabled = False
    agent.max_tokens = 1000
    agent.context_compressor.context_length = 256_000
    agent.context_compressor.threshold_tokens = 128_000
    agent.max_iterations = 1
    agent._disable_streaming = True
    agent.api_key = "local-disposable-test-key"
    agent.client = client(url)
    monkeypatch.setattr(
        agent,
        "_create_request_openai_client",
        lambda reason, api_kwargs: agent.client,
    )
    monkeypatch.setattr(
        local_runtime,
        "read_dispatch_source_identity",
        lambda: {
            "source_revision": "ares-source:" + "a" * 40,
            "source_digest": "sha256:" + "b" * 64,
        },
    )
    governor = DispatchGovernor(store_factory=lambda: db, mode="enforce")
    monkeypatch.setattr(
        middleware,
        "_get_middleware_callbacks",
        lambda kind: [governor] if kind == "llm_dispatch" else [],
    )
    try:
        yield agent, db, bodies, statuses
    finally:
        try:
            agent.client.close()
        except Exception:
            pass
        db.close()


def test_one_real_turn_settles_one_execution_record(live_turn):
    agent, db, bodies, statuses = live_turn
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "prefix\n\nok\n\nsuffix", persist_user_message="ok"
        )

    assert result.get("error") is None, result
    assert len(bodies) == 1
    execution_ids = db.list_execution_ids()
    assert len(execution_ids) == 1
    record = db.read_execution_record(execution_ids[0])
    value = record.to_dict()
    assert record.state == "settled"
    assert value["terminal_outcome"] == "ok"
    assert value["wire_request_digest"].startswith("sha256:")
    assert value["wire_bytes"] == len(bodies[0])
    assert value["http_status"] == 200
    assert value["governor_mode"] == "enforce"
    assert value["governor_qualified"] is True
    assert value["dispatch_intent_refs"]

    # The captured body must be the body the record's wire digest describes.
    import hashlib

    assert (
        value["wire_request_digest"]
        == "sha256:" + hashlib.sha256(bodies[0]).hexdigest()
    )
    admission = json.loads(db.get_meta(value["admission_ref"]))
    assert admission["attempt_id"] == execution_ids[0]
    materialization = json.loads(db.get_meta(value["materialization_ref"]))
    assert (
        materialization["context"]["request_digest"] == value["logical_request_digest"]
    )


def test_golden_path_passes_the_independent_oracle(live_turn, tmp_path):
    agent, db, bodies, _statuses = live_turn
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "prefix\n\nok\n\nsuffix", persist_user_message="ok"
        )
    assert result.get("error") is None, result
    capture = tmp_path / "capture.jsonl"
    capture.write_text(
        json.dumps({"body_b64": base64.b64encode(bodies[0]).decode("ascii")}) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ORACLE),
            "--db",
            str(db.db_path),
            "--capture",
            str(capture),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["checked"] == 1
