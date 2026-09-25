"""Cold native inbox recovery through real adapter and turn boundaries."""
import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from gateway.context_input import accepted_input, turn_input_kwargs, validate_gateway_input
from gateway.context_input_recovery import schedule_input_recovery
from gateway.platforms.base import BasePlatformAdapter
from hermes_state_continuity import ContextContinuationError
from tests.gateway.test_context_input import ingress, event  # noqa: F401
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def prepare(runner, adapter):
    adapter.bot_token = "local-test-transport-identity"
    runner._background_tasks = set()
    runner._startup_restore_tasks = []


async def drain(runner):
    tasks = tuple(runner._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_acceptance_origin_survives_reopen_and_cannot_be_backfilled(ingress):
    from hermes_state import SessionDB
    runner, adapter, db, _ = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    db2 = SessionDB(db.db_path)
    try:
        origin = db2.read_context_input_origin(bound.session_id, bound.receipt)
        assert origin["route"]["user_id"] == "user"
        assert origin["cold_recoverable"] is True
        assert "role_authorized" not in origin["route"]
        with pytest.raises(ContextContinuationError, match="ORIGIN_COLLISION"):
            db2.accept_context_input(bound.session_id, source=bound.receipt.source,
                event_id=bound.receipt.event_id, content=bound.receipt.content)
        old = db2.accept_context_input(bound.session_id, source="telegram", event_id="old", content="Legacy")
        with pytest.raises(ContextContinuationError, match="ORIGIN_COLLISION"):
            db2.accept_context_input(bound.session_id, source="telegram", event_id="old", content="Legacy", gateway_origin=origin)
        assert db2.read_context_input_origin(bound.session_id, old) is None
    finally:
        db2.close()


@pytest.mark.asyncio
async def test_startup_recovers_native_input_without_resume_marker_or_new_human(ingress):
    runner, adapter, db, started = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    entry = runner.session_store.lookup_by_session_key(bound.session_key)
    assert not entry.resume_pending
    started.clear()
    assert runner._schedule_resume_pending_sessions() == 1
    assert runner._schedule_resume_pending_sessions() == 0
    await drain(runner)
    assert len(started) == 1
    wake = started[0][0]
    assert accepted_input(wake).receipt == bound.receipt
    assert not wake.internal and wake.text == bound.receipt.content
    assert wake.metadata["gateway_session_strict"]
    assert db.read_pending_context_inputs(bound.session_id) == (bound.receipt,)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["schedule", "turn"])
async def test_every_actor_in_shared_chat_batch_is_reauthorized(ingress, durable_agent, boundary):
    runner, adapter, db, started = ingress
    prepare(runner, adapter)
    runner.config.group_sessions_per_user = False
    adapter.config.extra["group_sessions_per_user"] = False
    first, second = event(event_id="m1"), event("Another request", "m2")
    second.source.user_id = "second-user"
    await adapter.handle_message(first)
    await adapter.handle_message(second)
    assert accepted_input(first).session_id == accepted_input(second).session_id
    runner._is_user_authorized = lambda source, **kwargs: source.user_id != "user"
    started.clear()
    if boundary == "schedule":
        assert schedule_input_recovery(runner)[0] == 0
    else:
        agent, _ = durable_agent
        bound = accepted_input(second)
        agent._session_db, agent.session_id = db, bound.session_id
        with pytest.raises(ContextContinuationError, match="ACTOR_REVOKED"):
            agent.run_conversation(second.text, **turn_input_kwargs(bound))
        agent.client.chat.completions.create.assert_not_called()
    assert len(db.read_pending_context_inputs(accepted_input(first).session_id)) == 2
    assert started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["credential", "adapter", "legacy", "suspended", "route", "actor"])
async def test_changed_recovery_authority_parks_without_losing_input(ingress, change):
    runner, adapter, db, started = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    if change == "credential":
        adapter.bot_token = "different-transport"
    elif change == "adapter":
        runner.adapters.clear()
    elif change == "legacy":
        db.accept_context_input(bound.session_id, source="telegram", event_id="legacy", content="Unknown sender")
    elif change == "suspended":
        runner.session_store.lookup_by_session_key(bound.session_key).suspended = True
    elif change == "route":
        runner._profile_name_for_source = lambda source: "different-profile"
    else:
        runner._is_user_authorized = lambda source, **kwargs: False
    started.clear()
    assert schedule_input_recovery(runner)[0] == 0
    assert db.read_context_input(bound.session_id, source=bound.receipt.source, event_id=bound.receipt.event_id) == bound.receipt
    assert started == []


@pytest.mark.asyncio
async def test_reset_after_wake_admission_does_not_route_old_input_to_new_root(ingress):
    runner, adapter, db, started = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    started.clear()
    assert schedule_input_recovery(runner)[0] == 1
    await drain(runner)
    wake = started[0][0]
    entry = runner.session_store.lookup_by_session_key(bound.session_key)
    db.create_session("new-root", source="telegram")
    changed = replace(entry, session_id="new-root")
    with pytest.raises(ContextContinuationError, match="ROOT_CHANGED"):
        await validate_gateway_input(runner, wake, changed)
    assert db.get_messages("new-root") == []


@pytest.mark.asyncio
async def test_actual_cold_adapter_turn_projects_once_and_records_response(ingress, durable_agent):
    runner, adapter, db, _ = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    agent, _ = durable_agent
    agent._session_db, agent.session_id = db, bound.session_id
    agent.client.chat.completions.create.return_value = _mock_response(content="Recovered response", finish_reason="stop")
    results = []
    async def handle(wake):
        entry = runner.session_store.lookup_by_session_key(bound.session_key)
        await validate_gateway_input(runner, wake, entry)
        result = await asyncio.to_thread(agent.run_conversation, wake.text, **turn_input_kwargs(accepted_input(wake)))
        results.append(result)
        return result["final_response"]
    adapter.set_message_handler(handle)
    adapter._start_session_processing = BasePlatformAdapter._start_session_processing.__get__(adapter)
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        assert runner._schedule_resume_pending_sessions() == 1
        await drain(runner)
    assert len(results) == 1 and results[0]["completed"] and not results[0]["failed"], results
    assert agent.client.chat.completions.create.call_count == 1
    assert db.read_context_input_work(bound.session_id)["phase"]["state"] == "answered"
    assert len([r for r in db.get_messages(bound.session_id) if r["role"] == "user"]) == 1
    assert runner._schedule_resume_pending_sessions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["role_authorized", "delivered_via_upstream_relay"])
async def test_unqualified_delegated_authorization_refuses_before_native_acceptance(ingress, flag):
    runner, adapter, db, started = ingress
    prepare(runner, adapter)
    incoming = event()
    setattr(incoming.source, flag, True)
    with pytest.raises(ContextContinuationError, match="AUTH_POLICY_UNQUALIFIED"):
        await adapter.handle_message(incoming)
    assert accepted_input(incoming) is None and started == []
    assert db._conn.execute("SELECT COUNT(*) FROM state_meta WHERE key LIKE 'context-inbox:%'").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id,user_id", [("-100123456", "1234"), ("!room:example.org", "@user:example.org"), ("mail@example.org", "sender@example.org")])
async def test_platform_ids_remain_opaque_in_native_provenance(ingress, chat_id, user_id):
    runner, adapter, db, _ = ingress
    prepare(runner, adapter)
    incoming = event()
    incoming.source.chat_id, incoming.source.user_id = chat_id, user_id
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    origin = db.read_context_input_origin(bound.session_id, bound.receipt)
    assert origin["route"]["chat_id"] == chat_id and origin["route"]["user_id"] == user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["request", "response"])
async def test_actor_revocation_reaches_real_provider_boundary(ingress, durable_agent, boundary):
    runner, adapter, db, _ = ingress
    prepare(runner, adapter)
    incoming = event()
    await adapter.handle_message(incoming)
    bound = accepted_input(incoming)
    agent, _ = durable_agent
    agent._session_db, agent.session_id = db, bound.session_id
    def revoke():
        runner._is_user_authorized = lambda source, **kwargs: False
    if boundary == "request":
        from ares_runtime.continuity.budget import final_request_upper_bound
        def count(**kwargs):
            result = final_request_upper_bound(**kwargs)
            revoke()
            return result
        mutation = patch("ares_runtime.continuity.budget.final_request_upper_bound", side_effect=count)
    else:
        def response(**kwargs):
            revoke()
            return _mock_response(content="Must not consume this", finish_reason="stop")
        mutation = patch.object(agent.client.chat.completions, "create", side_effect=response)
    with mutation, patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation(incoming.text, **turn_input_kwargs(bound))
        assert result["error"] == "GATEWAY_INPUT_ACTOR_REVOKED", result
    assert not result["completed"]
    assert not any(r["content"] == "Must not consume this" for r in db.get_messages(bound.session_id))
