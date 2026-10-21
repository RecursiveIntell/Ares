"""Admission and materialization must share one deterministic execution id."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


@pytest.fixture(autouse=True)
def _disable_automatic_title_provider(monkeypatch):
    monkeypatch.setattr(
        "agent.title_generator.maybe_auto_title", lambda *_a, **_kw: None
    )


@pytest.fixture
def durable_agent(agent, tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "binding.db")
    db.create_session(agent.session_id, source="cli", profile_name="p1", model="test")
    agent._session_db = db
    agent._session_db_created = True
    agent._cached_system_prompt = "Trusted instructions"
    agent.provider = "openai"
    agent.model = "local"
    agent.api_mode = "chat_completions"
    agent.context_rebase_enabled = True
    agent.compression_enabled = False
    agent.max_tokens = 1000
    agent.context_compressor.context_length = 256_000
    agent.context_compressor.threshold_tokens = 128_000
    agent.max_iterations = 1
    try:
        yield agent, db
    finally:
        db.close()


def test_admission_and_materialization_share_execution_id(durable_agent, monkeypatch):
    from ares_runtime import local_runtime

    monkeypatch.setattr(
        local_runtime,
        "read_dispatch_source_identity",
        lambda: {
            "source_revision": "ares-source:" + "a" * 40,
            "source_digest": "sha256:" + "b" * 64,
        },
    )
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(
        content="Done", finish_reason="stop"
    )
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "prefix\n\nok\n\nsuffix", persist_user_message="ok"
        )

    assert result.get("error") is None, result
    execution_ids = db.list_execution_ids()
    assert len(execution_ids) == 1
    execution_id = execution_ids[0]
    record = db.read_execution_record(execution_id)
    value = record.to_dict()

    assert record.state == "materialized"
    assert value["execution_id"] == execution_id
    assert value["call_ref"] == "ares-call:" + execution_id
    assert value["materialization_ref"] is not None
    assert value["logical_request_digest"] is not None
    assert value["admission_ref"] == "context-dispatch:" + execution_id

    admission_raw = db.get_meta(value["admission_ref"])
    assert admission_raw is not None
    admission = json.loads(admission_raw)
    assert admission["schema"] == "SessionDBContextDispatchV2"
    assert admission["attempt_id"] == execution_id
    assert admission["payload_digest"].startswith("sha256:")
    assert value["logical_request_digest"].startswith("sha256:")

    materialization_raw = db.get_meta(value["materialization_ref"])
    assert materialization_raw is not None
    materialization = json.loads(materialization_raw)
    assert materialization["context"]["turn_ref"] == value["turn_ref"]
    assert materialization["context"]["call_ref"] == value["call_ref"]
    assert (
        materialization["context"]["request_digest"] == value["logical_request_digest"]
    )
    assert materialization["context"]["source_revision"] == value["source_revision"]
