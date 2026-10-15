"""Server-observed Desktop origin is evidence, never a replay authorization."""
import asyncio
from dataclasses import asdict
import json
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_context_media_custody import origin
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport
from tui_gateway.ws import WSTransport


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', source='desktop')
    db.create_session('other', source='desktop', profile_name='other-profile')
    loop = asyncio.new_event_loop()
    transport = WSTransport(SimpleNamespace(), loop)
    agent = SimpleNamespace(session_id='owned', platform='desktop',
        _session_db=db, context_rebase_enabled=True)
    session = {'session_key': 'owned', 'agent': agent, 'history_lock': threading.Lock(),
        'running': False, 'transport': transport, 'source': 'desktop'}
    monkeypatch.setattr(server, '_sessions', {'runtime': session})
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    token = bind_transport(transport)
    try:
        yield db, session, transport, loop
    finally:
        reset_transport(token)
        transport.close()
        loop.close()
        db.close()


def send(session, event='input', text='Frozen input', **kwargs):
    with session['history_lock']:
        return server._accept_tui_context_input(session, text, event_id=event, **kwargs)


def status(text='Frozen input', **extra):
    return server._methods['prompt.input_status']('status', {
        'session_id': 'runtime', 'input_event_id': 'input', 'text': text, **extra})


def test_new_desktop_text_preserves_v1_payload_with_versioned_origin(desktop):
    db, session, transport, _ = desktop
    receipt = send(session)
    assert receipt.schema == 'SessionDBContextInputV1'
    assert receipt.content == 'Frozen input'
    witness = db.read_context_input_origin('owned', receipt)
    assert witness is not None
    assert witness['schema'] == 'SessionDBDesktopInputOriginV1'
    assert witness['runtime_session_id'] == 'runtime'
    assert witness['stored_session_id'] == 'owned'
    assert witness['profile_name'] == 'default'
    assert witness['connection_id'] is None
    assert witness['transport_epoch'] == transport.input_epoch
    assert witness['stop_revision'] == 0
    result = status()['result']
    assert result['origin_status'] == 'recorded'
    assert result['can_manual_retry'] is True
    assert result['execution_started'] is False


def test_same_id_readback_and_new_transport_manual_recovery_preserve_original(desktop):
    db, session, transport, loop = desktop
    receipt = send(session)
    frozen = asdict(receipt)
    witness = db.read_context_input_origin('owned', receipt)
    replacement = WSTransport(SimpleNamespace(), loop)
    session['transport'] = replacement
    token = bind_transport(replacement)
    try:
        assert replacement.input_epoch != transport.input_epoch
        # Readback is explicitly manual, not an automatic lost-ACK resend.
        assert status()['result']['can_manual_retry'] is True
        assert send(session, manual_recovery=True) == receipt
        assert db.read_context_input_origin('owned', receipt) == witness
        assert asdict(db.read_context_input('owned', source='desktop', event_id='input')) == frozen
        assert db.read_pending_context_inputs('owned') == (receipt,)
        # Ordinary same-ID re-delivery must not silently adopt a new epoch.
        with pytest.raises(ContextContinuationError, match='ORIGIN_COLLISION'):
            send(session)
    finally:
        reset_transport(token)
        replacement.close()


def test_legacy_missing_origin_is_unknown_not_manual_recoverable(desktop):
    db, session, _, _ = desktop
    receipt = db.accept_context_input('owned', source='desktop', event_id='input', content='Frozen input')
    result = status()['result']
    assert result['durable_input_accepted'] is True
    assert result['can_manual_retry'] is False
    assert result['origin_status'] == 'missing'
    with pytest.raises(ContextContinuationError, match='ORIGIN_MISSING'):
        send(session, manual_recovery=True)
    assert db.read_context_input_origin('owned', receipt) is None
    assert db.read_pending_context_inputs('owned') == (receipt,)


@pytest.mark.parametrize('fault', ['unbound', 'different_transport', 'closed', 'unregistered'])
def test_current_owner_or_transport_loss_denies_manual_retry(desktop, fault, monkeypatch):
    db, session, transport, loop = desktop
    receipt = send(session)
    original = db.read_context_input_origin('owned', receipt)
    token = None
    replacement = None
    if fault == 'unbound':
        token = bind_transport(None)
    elif fault == 'different_transport':
        replacement = WSTransport(SimpleNamespace(), loop)
        token = bind_transport(replacement)
    elif fault == 'closed':
        transport.close()
    else:
        monkeypatch.setattr(server, '_sessions', {})
    try:
        response = status()
        assert 'error' in response or response['result']['can_manual_retry'] is False
        with pytest.raises(ContextContinuationError, match='ORIGIN_|OWNER_'):
            send(session, manual_recovery=True)
        assert db.read_context_input_origin('owned', receipt) == original
        assert db.read_pending_context_inputs('owned') == (receipt,)
    finally:
        if token is not None:
            reset_transport(token)
        if replacement is not None:
            replacement.close()


def test_client_supplied_origin_and_connection_labels_never_replace_evidence(desktop):
    db, session, _, _ = desktop
    receipt = send(session)
    witness = db.read_context_input_origin('owned', receipt)
    result = status(desktop_origin=origin(), owner_epoch='forged', connection_id='forged')['result']
    assert result['can_manual_retry'] is True
    assert db.read_context_input_origin('owned', receipt) == witness
    assert witness['connection_id'] is None


def test_stop_invalidates_retry_but_does_not_rewrite_original_origin(desktop):
    db, session, _, _ = desktop
    receipt = send(session)
    witness = db.read_context_input_origin('owned', receipt)
    control = db.record_context_stop('owned')
    assert status()['result']['can_manual_retry'] is False
    with pytest.raises(ContextContinuationError, match='MANUAL_RECOVERY_UNSAFE'):
        send(session, manual_recovery=True)
    later = send(session, event='post-stop')
    assert db.read_context_input_origin('owned', later)['stop_revision'] == control['revision']
    assert db.read_context_input_origin('owned', receipt) == witness


def test_database_owner_requires_desktop_origin_scope_and_stop_revision(desktop):
    db, _, _, _ = desktop
    witness = origin()
    witness['stored_session_id'] = 'owned'
    db.record_context_stop('owned')
    with pytest.raises(ContextContinuationError, match='ORIGIN_STOP_CHANGED'):
        db.accept_context_input('owned', source='desktop', event_id='stale',
            content='Frozen input', desktop_origin=witness)
    assert db.read_context_input('owned', source='desktop', event_id='stale') is None
    with pytest.raises(ContextContinuationError, match='ORIGIN_INVALID|SCOPE_MISMATCH'):
        db.accept_context_input('other', source='desktop', event_id='foreign',
            content='Frozen input', desktop_origin=witness)


def test_text_origin_is_strict_versioned_sidecar_and_survives_db_reopen(desktop):
    db, session, _, _ = desktop
    receipt = send(session)
    witness = db.read_context_input_origin('owned', receipt)
    reopened = SessionDB(db.db_path)
    try:
        assert reopened.read_context_input_origin('owned', receipt) == witness
        assert reopened.read_context_input('owned', source='desktop', event_id='input') == receipt
        with reopened._read_ctx() as conn:
            rows = [json.loads(row[0]) for row in conn.execute(
                "SELECT value FROM state_meta WHERE key LIKE '%:origin'").fetchall()]
        assert rows[0]['schema'] == 'SessionDBContextInputOriginV2'
        malformed = dict(rows[0], extra='forged')
        reopened._execute_write(lambda conn: conn.execute(
            "UPDATE state_meta SET value=? WHERE key LIKE '%:origin'", (json.dumps(malformed),)))
        with pytest.raises(ContextContinuationError, match='ORIGIN_INVALID'):
            reopened.read_context_input_origin('owned', receipt)
        response = status()
        assert 'error' in response or response['result']['can_manual_retry'] is False
    finally:
        reopened.close()


def test_attachments_still_refuse_before_any_text_origin_ack(desktop):
    db, session, _, _ = desktop
    session['attached_images'] = ['immutable-media-not-yet-qualified']
    with pytest.raises(ContextContinuationError, match='TEXT_ROUTE_REQUIRED'):
        send(session)
    assert db.read_context_input('owned', source='desktop', event_id='input') is None
    assert session['attached_images'] == ['immutable-media-not-yet-qualified']


from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: E402,F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: E402,F401
from tests.tui_gateway.test_prompt_recovery_contract import _session  # noqa: E402


@pytest.mark.parametrize('invalidate', [None, 'transport', 'Stop'])
@pytest.mark.parametrize('topology', ['inline', 'host'])
def test_real_turn_driver_uses_original_receipt_and_current_execution_fence(
        durable_agent, monkeypatch, invalidate, topology):
    """Real native admission/projection/dispatch; only provider/telemetry inert."""
    native, db = durable_agent
    native.platform = 'desktop'
    loop = asyncio.new_event_loop()
    transport = WSTransport(SimpleNamespace(), loop)
    session = _session(agent=native, session_key=native.session_id, source='desktop',
        transport=transport)
    monkeypatch.setattr(server, '_sessions', {'runtime': session})
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    events = []
    monkeypatch.setattr(server, '_emit', lambda event, sid, payload=None: events.append((event, payload)))
    for name in ('_wire_callbacks', '_sync_agent_model_with_config', '_sync_agent_compression_with_config',
                 '_register_session_cwd', '_sync_session_key_after_compress', '_tts_stream_begin'):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, '_load_cfg', lambda: {})
    monkeypatch.setattr(server, '_get_usage', lambda _: {})
    monkeypatch.setattr(server, '_load_interim_assistant_messages', lambda: False)
    monkeypatch.setattr(server, '_voice_tts_enabled', lambda: False)
    monkeypatch.setattr(server, '_session_info', lambda *a, **k: {})
    monkeypatch.setattr('agent.title_generator.maybe_auto_title', lambda *a, **k: None)
    native.client.chat.completions.create.return_value = _mock_response(content='Done', finish_reason='stop')
    token = bind_transport(transport)
    run = native.run_conversation
    captured = []
    execution_transport = transport
    def inspect_handoff(*args, **kwargs):
        captured.append(kwargs)
        if invalidate == 'transport':
            if topology == 'inline':
                transport.close()
            else:
                session['transport'] = object()
        elif invalidate == 'Stop':
            db.record_context_stop(native.session_id)
            session['_turn_cancel_requested'] = True
        return run(*args, **kwargs)
    # Preserve the inspected public signature used by the actual driver.
    import functools
    monkeypatch.setattr(native, 'run_conversation', functools.wraps(run)(inspect_handoff))
    try:
        receipt = send(session)
        witness = db.read_context_input_origin(native.session_id, receipt)
        if topology == 'host':
            from tui_gateway.compute_host import _HostTransport
            monkeypatch.setenv('HERMES_COMPUTE_HOST_CHILD', '1')
            execution_transport = _HostTransport(lambda frame: None)
            session['transport'] = execution_transport
            session['_host_turn_request_id'] = 'turn'
        session['running'] = True
        server._start_inflight_turn(session, receipt.content)
        server._begin_turn_outcome(session, 'runtime', 'turn',
            'compute_host' if topology == 'host' else 'inline', 'owned-boot' if topology == 'host' else None)
        server._run_prompt_submit('turn', 'runtime', session, receipt.content,
            context_input_event_id=receipt.event_id, turn_transport=execution_transport)
        worker = session.get('_run_thread')
        assert worker is not None
        worker.join(timeout=30)
        assert not worker.is_alive(), events
        assert captured and captured[0]['persist_user_input_receipt'] == receipt
        assert callable(captured[0]['persist_user_input_authorizer'])
        assert db.read_context_input_origin(native.session_id, receipt) == witness
        assert db.read_context_input(native.session_id, source='desktop', event_id=receipt.event_id) == receipt
        if invalidate:
            native.client.chat.completions.create.assert_not_called()
        else:
            assert native.client.chat.completions.create.call_count == 1, events
            assert [row['content'] for row in db.get_messages(native.session_id) if row['role'] == 'user'] == [receipt.content]
            assert db.read_context_input_work(native.session_id)['receipts'] == ()
            assert captured[0]['persist_user_input_authorizer'](native.session_id, (receipt,)) is False
    finally:
        reset_transport(token)
        transport.close()
        loop.close()


def test_cold_runtime_rehome_preserves_original_epoch_and_exact_receipt(desktop, monkeypatch):
    db, session, _, loop = desktop
    receipt = send(session)
    original = db.read_context_input_origin('owned', receipt)
    replacement = WSTransport(SimpleNamespace(), loop)
    successor = dict(session)
    successor['agent'] = SimpleNamespace(session_id='owned', platform='desktop',
        _session_db=db, context_rebase_enabled=True)
    successor['transport'] = replacement
    monkeypatch.setattr(server, '_sessions', {'resumed-runtime': successor})
    token = bind_transport(replacement)
    try:
        response = server._methods['prompt.input_status']('status', {
            'session_id': 'resumed-runtime', 'input_event_id': 'input', 'text': receipt.content})
        assert response['result']['can_manual_retry'] is True
        assert send(successor, manual_recovery=True) == receipt
        assert db.read_context_input_origin('owned', receipt) == original
        current = server._observe_desktop_input_origin(successor, db, 'owned')
        assert current['runtime_session_id'] == 'resumed-runtime'
        assert current['owner_epoch'] != original['owner_epoch']
        assert current['transport_epoch'] != original['transport_epoch']
    finally:
        reset_transport(token)
        replacement.close()


def test_stop_winning_text_receipt_reservation_refuses_without_origin_backfill(desktop, monkeypatch):
    db, session, _, _ = desktop
    other = SessionDB(db.db_path)
    accept = db.accept_context_input
    cuts = []
    def race(*args, **kwargs):
        cuts.append(other.record_context_stop('owned'))
        return accept(*args, **kwargs)
    monkeypatch.setattr(db, 'accept_context_input', race)
    try:
        with pytest.raises(ContextContinuationError, match='ORIGIN_STOP_CHANGED'):
            send(session)
        assert cuts and db.read_context_input('owned', source='desktop', event_id='input') is None
        assert db.read_context_stop('owned')['control'] == cuts[0]
    finally:
        other.close()


def test_real_busy_rpc_records_only_server_origin_despite_forged_params(desktop, monkeypatch):
    db, session, transport, _ = desktop
    session['running'] = True
    monkeypatch.setattr(server, '_emit', lambda *a, **k: None)
    response = server._methods['prompt.submit']('send', {
        'session_id': 'runtime', 'input_event_id': 'input', 'text': 'Frozen input',
        'queued': True, 'desktop_origin': origin(), 'owner_epoch': 'forged',
        'transport_epoch': 'forged', 'connection_id': 'forged'})
    assert 'error' not in response, response
    receipt = db.read_context_input('owned', source='desktop', event_id='input')
    witness = db.read_context_input_origin('owned', receipt)
    assert witness['transport_epoch'] == transport.input_epoch
    assert witness['owner_epoch'] != 'forged'
    assert witness['connection_id'] is None
    assert witness['stored_session_id'] == 'owned'
    assert session['queued_prompt']['context_input_event_id'] == receipt.event_id
    assert db.read_pending_context_inputs('owned') == (receipt,)
