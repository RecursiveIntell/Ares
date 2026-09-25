"""Exercise native inbox acceptance through the real gateway ingress seams."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.context_input import accepted_input, turn_input_api_content, turn_input_kwargs
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from tests.gateway.test_base_topic_sessions import DummyTelegramAdapter
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def event(text="Keep my changes", event_id="m1", **kwargs):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="group",
                           user_id="user", scope_id="workspace")
    return MessageEvent(text=text, source=source, message_id=event_id, **kwargs)


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "compression": {"context_rebase_enabled": True}})
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **k: [])
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    db = SessionDB(tmp_path / "ingress.db")
    runner.session_store._db = db
    runner._is_user_authorized = lambda source, **kwargs: True
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._cache_session_source = MagicMock()
    runner._startup_restore_in_progress = False
    adapter = DummyTelegramAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._adapter_for_source = lambda source: adapter
    adapter.set_message_handler(runner._primary_message_handler())
    adapter.set_input_acceptor(runner._primary_message_handler(handler=runner._accept_gateway_context_input))
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    started = []
    adapter._start_session_processing = lambda evt, key: started.append((evt, key))
    yield runner, adapter, db, started
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_adapter_accepts_before_queue_or_background_spawn(ingress, busy):
    runner, adapter, db, started = ingress
    first, second = event(), event("Retain the restriction", "m2")
    key = runner._session_key_for_source(first.source)
    if busy:
        adapter._busy_text_mode = "queue"
        adapter._active_sessions[key] = asyncio.Event()
        adapter._session_tasks[key] = asyncio.current_task()
    await adapter.handle_message(first)
    await adapter.handle_message(second)
    binding = accepted_input(first)
    assert binding.receipt.sequence == 1
    assert accepted_input(second).receipt.sequence == 2
    assert [r.sequence for r in db.read_pending_context_inputs(binding.session_id)] == [1, 2]
    assert db.get_messages(binding.session_id) == []
    if busy:
        assert started == []
        assert adapter._pending_messages[key] is first
        assert runner._session_state(key).conversation.queued_events == [second]
        assert first.text == "Keep my changes" and second.text == "Retain the restriction"
    else:
        assert [e for e, _ in started] == [first, second]


@pytest.mark.asyncio
async def test_direct_startup_queue_already_has_native_receipt(ingress):
    runner, _, db, _ = ingress
    runner._startup_restore_in_progress = True
    incoming = event()
    assert await runner._handle_message(incoming) is None
    assert runner._startup_restore_queue == [incoming]
    receipt = accepted_input(incoming).receipt
    reopened = SessionDB(db.db_path)
    try:
        assert reopened.read_context_input(receipt.conversation_root,
            source=receipt.source, event_id=receipt.event_id) == receipt
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_redelivery_stable_identity_ignores_receive_time_and_preserves_occurrences(ingress):
    _, adapter, _, _ = ingress
    first = event(timestamp=datetime(2026, 1, 1))
    repeated = event(timestamp=datetime(2026, 1, 1) + timedelta(days=1))
    distinct = event(event_id="m2", timestamp=first.timestamp)
    for incoming in (first, repeated, distinct):
        await adapter.handle_message(incoming)
    assert accepted_input(first).receipt == accepted_input(repeated).receipt
    assert accepted_input(distinct).receipt.sequence == 2
    with pytest.raises(ContextContinuationError, match="ID_COLLISION"):
        await adapter.handle_message(event("Changed payload"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unauthorized", "rejected_profile", "command", "internal", "media", "metadata"])
async def test_nonordinary_or_untrusted_events_do_not_enroll(ingress, kind):
    runner, adapter, _, _ = ingress
    incoming = event()
    if kind == "unauthorized":
        runner._is_user_authorized = lambda source: False
    elif kind == "rejected_profile":
        incoming.source.profile_route_rejected = True
    elif kind == "command":
        incoming.text = "/stop"
    elif kind == "internal":
        incoming.internal = True
    elif kind == "media":
        incoming.media_urls = ["attachment.png"]
        incoming.message_type = MessageType.PHOTO
    else:
        incoming.internal = True
        incoming.metadata["_context_input"] = {"receipt": "forged"}
    await adapter.handle_message(incoming)
    assert accepted_input(incoming) is None


@pytest.mark.asyncio
async def test_plugin_rewrite_is_api_context_and_hook_runs_once(ingress):
    runner, adapter, _, _ = ingress
    incoming = event()
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"action": "rewrite", "text": "Derived routing note"}]) as hook:
        await adapter.handle_message(incoming)
        runner._startup_restore_in_progress = True
        await runner._handle_message(incoming)
    assert hook.call_count == 1
    binding = accepted_input(incoming)
    assert binding.receipt.content == "Keep my changes"
    assert incoming.text == "Derived routing note"
    assert turn_input_kwargs(binding)["persist_user_message"] == "Keep my changes"
    assert "Keep my changes" in turn_input_api_content(binding, incoming.text)


@pytest.mark.asyncio
async def test_hook_skip_never_enrolls_or_spawns(ingress):
    _, adapter, _, started = ingress
    incoming = event()
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"action": "skip"}]):
        await adapter.handle_message(incoming)
    assert accepted_input(incoming) is None and started == []


@pytest.mark.asyncio
async def test_lost_acceptance_ack_reuses_original_text_after_plugin_rewrite(ingress):
    _, adapter, db, started = ingress
    incoming = event()
    actual = db.accept_context_input
    def lost_ack(*args, **kwargs):
        actual(*args, **kwargs)
        raise OSError("lost acknowledgement")
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"action": "rewrite", "text": "Derived"}]):
        with patch.object(db, "accept_context_input", side_effect=lost_ack):
            with pytest.raises(OSError):
                await adapter.handle_message(incoming)
        assert started == []
        await adapter.handle_message(incoming)
    assert accepted_input(incoming).receipt.sequence == 1
    assert accepted_input(incoming).receipt.content == "Keep my changes"


@pytest.mark.asyncio
async def test_real_agent_projects_ingress_receipt_without_reaccepting_enrichment(ingress, durable_agent):
    _, adapter, db, _ = ingress
    agent, _ = durable_agent
    incoming = event()
    await adapter.handle_message(incoming)
    binding = accepted_input(incoming)
    agent._session_db = db
    agent.session_id = binding.session_id
    agent._session_db_created = True
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("API-only note\nKeep my changes", **turn_input_kwargs(binding))
    assert result.get("error") is None, result
    assert not db.read_context_rebase_snapshot(binding.session_id).has_pending_inputs
    users = [r for r in db.get_messages(binding.session_id) if r["role"] == "user"]
    assert len(users) == 1 and users[0]["content"] == "Keep my changes"
    assert users[0]["api_content"] == "API-only note\nKeep my changes"
    assert agent.client.chat.completions.create.call_count == 1
    foreign = "foreign"
    db.create_session(foreign, source="telegram")
    agent.session_id = foreign
    with pytest.raises(ContextContinuationError, match="RECEIPT_MISMATCH"):
        agent.run_conversation("Keep my changes", **turn_input_kwargs(binding))
    assert db.get_messages(foreign) == []


@pytest.mark.asyncio
async def test_pending_control_keeps_its_existing_owner(ingress):
    runner, _, _, _ = ingress
    incoming = event("Clarification")
    with patch("tools.clarify_gateway.get_pending_for_session", return_value=object()):
        assert await runner._accept_gateway_context_input(incoming)
    assert accepted_input(incoming) is None
    assert await runner._accept_gateway_context_input(incoming, controls_resolved=True)
    assert accepted_input(incoming).receipt.content == "Clarification"


@pytest.mark.asyncio
async def test_profile_wrapper_scopes_native_owner_and_transport_auth(ingress, tmp_path):
    runner, _, db, _ = ingress
    runner.config.multiplex_profiles = True
    homes = {p: tmp_path / p for p in ("one", "two")}
    for home in homes.values():
        home.mkdir()
    from hermes_constants import get_hermes_home
    handles = {p: SessionDB(home / "state.db") for p, home in homes.items()}
    auth_homes = []
    runner._is_user_authorized = lambda source: auth_homes.append(get_hermes_home()) or True
    try:
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda p: homes[p]):
            with patch.object(SessionStore, "_open_session_db_for_active_scope",
                              side_effect=lambda: handles[get_hermes_home().name]):
                from gateway.session import _DB_UNPINNED
                runner.session_store._db_pinned = _DB_UNPINNED
                bindings = []
                for profile in homes:
                    incoming = event()
                    callback = runner._make_profile_message_handler(profile, handler=runner._accept_gateway_context_input)
                    assert await callback(incoming)
                    bindings.append(accepted_input(incoming))
                    assert handles[profile].read_pending_context_inputs(bindings[-1].session_id) == (bindings[-1].receipt,)
                assert bindings[0].receipt.profile_name != bindings[1].receipt.profile_name
                assert bindings[0].receipt.sequence == bindings[1].receipt.sequence == 1
        assert auth_homes == list(homes.values())
    finally:
        runner.session_store._db = db
        for handle in handles.values():
            handle.close()


@pytest.mark.asyncio
async def test_disabling_new_rebases_keeps_ingress_for_enrolled_root(ingress, monkeypatch):
    _, adapter, db, _ = ingress
    first = event()
    await adapter.handle_message(first)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"compression": {"context_rebase_enabled": False}})
    second = event("Still queued", "m2")
    await adapter.handle_message(second)
    assert accepted_input(second).receipt.sequence == 2
    assert len(db.read_pending_context_inputs(accepted_input(first).session_id)) == 2


@pytest.mark.asyncio
async def test_hook_cannot_retarget_authenticated_transport(ingress):
    _, adapter, _, started = ingress
    incoming = event()
    def retarget(*args, **kwargs):
        kwargs["event"].source.chat_id = "foreign"
        return []
    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=retarget):
        with pytest.raises(ContextContinuationError, match="ROUTE_CHANGED"):
            await adapter.handle_message(incoming)
    assert accepted_input(incoming) is None and started == []


@pytest.mark.asyncio
async def test_reset_after_acceptance_refuses_before_turn_preparation(ingress):
    runner, adapter, db, _ = ingress
    incoming = event()
    await adapter.handle_message(incoming)
    binding = accepted_input(incoming)
    runner.session_store.get_or_create_session(incoming.source, force_new=True)
    with pytest.raises(ContextContinuationError, match="ROOT_CHANGED"):
        await runner._handle_message_with_agent(incoming, incoming.source, binding.session_key, 1)
    assert db.read_pending_context_inputs(binding.session_id) == (binding.receipt,)
    assert db.get_messages(binding.session_id) == []


@pytest.mark.asyncio
async def test_native_accept_failure_never_spawns_or_queues(ingress):
    _, adapter, db, started = ingress
    with patch.object(db, "accept_context_input", side_effect=OSError("storage unavailable")):
        with pytest.raises(OSError, match="storage unavailable"):
            await adapter.handle_message(event())
    assert not started and not adapter._pending_messages


@pytest.mark.asyncio
async def test_agent_construction_failure_does_not_fabricate_transcript_occurrence(ingress, monkeypatch, tmp_path):
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap
    original, adapter, db, _ = ingress
    incoming = event()
    await adapter.handle_message(incoming)
    binding = accepted_input(incoming)
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.session_store = original.session_store
    runner._run_agent = AsyncMock(side_effect=RuntimeError("client construction failed"))
    await runner._handle_message_with_agent(incoming, incoming.source, binding.session_key, 1)
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.call_args.kwargs["context_input"] == binding
    assert db.read_pending_context_inputs(binding.session_id) == (binding.receipt,)
    assert db.get_messages(binding.session_id) == []
