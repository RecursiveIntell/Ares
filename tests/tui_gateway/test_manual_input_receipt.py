"""Manual readback reuses canonical inbox receipts; no provider or new store."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport
from tui_gateway.ws import WSTransport

@pytest.fixture
def inbox(tmp_path, monkeypatch):
    db=SessionDB(tmp_path/'state.db'); db.create_session('stored', source='desktop')
    agent=SimpleNamespace(session_id='stored', platform='desktop', _session_db=db, context_rebase_enabled=True)
    loop = asyncio.new_event_loop()
    transport = WSTransport(SimpleNamespace(), loop)
    session={'session_key':'stored', 'agent':agent, 'history_lock':threading.Lock(), 'running':False,
             'transport':transport}
    monkeypatch.setattr(server, '_sessions', {'runtime':session})
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    token = bind_transport(transport)
    try:
        yield db,session
    finally:
        reset_transport(token)
        transport.close()
        loop.close()
        db.close()

def status(text='frozen', event_id='input'):
    return server._methods['prompt.input_status'](1, {'session_id':'runtime', 'text':text, 'input_event_id':event_id})

def test_exact_accepted_not_started_receipt_is_reviewable(inbox):
    db,session=inbox
    receipt=server._accept_tui_context_input(session, 'frozen', event_id='input')
    result=status()['result']
    assert result['durable_input_accepted'] is True
    assert result['execution_started'] is False
    assert result['can_manual_retry'] is True
    assert result['payload_digest']==receipt.payload_digest
    assert db.read_pending_context_inputs('stored')==(receipt,)
    assert 'error' in status('edited')

def test_absent_receipt_is_unknown_and_stop_never_replays(inbox):
    db,session=inbox
    assert status()['result']['durable_input_accepted'] is None
    db.accept_context_input('stored',source='desktop',event_id='input',content='frozen')
    session['_turn_cancel_requested']=True
    result=status()['result']
    assert result['execution_started'] is None
    assert result['can_manual_retry'] is False

def test_dispatched_receipt_cannot_be_manual_replayed(inbox):
    db,session=inbox
    receipt=db.accept_context_input('stored',source='desktop',event_id='input',content='frozen')
    db.try_acquire_session_turn_lease('stored','holder',ttl_seconds=300)
    db.begin_context_input_turn('stored',receipt=receipt,turn_lease_holder='holder')
    db.append_messages_batch('stored',[{'role':'user','content':receipt.content,
        'timestamp':receipt.timestamp,'_context_input':{'conversation_root':receipt.conversation_root,
        'sequence':receipt.sequence,'payload_digest':receipt.payload_digest}}],turn_lease_holder='holder')
    db.admit_context_dispatch('stored',turn_lease_holder='holder',attempt_id='attempt',
        expected_snapshot_digest=db.read_context_rebase_snapshot('stored').digest,
        payload_digest='sha256:'+'a'*64,route_ref='fixture')
    result=status()['result']
    assert result['execution_started'] is True
    assert result['can_manual_retry'] is False


def test_host_pending_receipt_uses_canonical_store_not_dormant_mirror(inbox, monkeypatch):
    db, session = inbox
    session['source'] = 'desktop'
    session['agent'].context_rebase_enabled = False
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_load_cfg', lambda: {'compression': {'context_rebase_enabled': True}})
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    receipt = server._accept_tui_context_input(session, 'frozen', event_id='input')
    assert server._accept_tui_context_input(session, 'frozen', event_id='input', manual_recovery=True) == receipt
    assert db.read_pending_context_inputs('stored') == (receipt,)
    session['_turn_cancel_requested'] = True
    with pytest.raises(Exception, match='MANUAL_RECOVERY_UNSAFE'):
        server._accept_tui_context_input(session, 'frozen', event_id='input', manual_recovery=True)


def test_durable_acceptance_fault_never_returns_receipt(inbox, monkeypatch):
    db, session = inbox
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    def fail(*a, **k):
        raise OSError('controlled durable write refusal')
    monkeypatch.setattr(db, 'accept_context_input', fail)
    with pytest.raises(OSError, match='durable write refusal'):
        server._accept_tui_context_input(session, 'frozen', event_id='fault')
    assert db.read_context_input('stored', source='desktop', event_id='fault') is None


def test_host_unknown_receipt_never_grants_manual_execution(inbox):
    db, session = inbox
    db.accept_context_input('stored', source='desktop', event_id='input', content='frozen')
    session['_host_delivery_uncertain'] = True
    assert status()['result']['can_manual_retry'] is False
    with pytest.raises(Exception, match='MANUAL_RECOVERY_UNSAFE'):
        server._accept_tui_context_input(session, 'frozen', event_id='input', manual_recovery=True)


from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: E402,F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: E402,F401


def test_host_manual_receipt_reuses_real_native_admission_once(durable_agent, monkeypatch):
    from unittest.mock import patch
    native, db = durable_agent
    native.platform = 'cli'
    session = {'session_key': native.session_id, 'source': native.platform or 'cli',
               'agent': SimpleNamespace(context_rebase_enabled=False),
               'history_lock': threading.Lock(), 'running': False}
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_load_cfg', lambda: {'compression': {'context_rebase_enabled': True}})
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    receipt = db.accept_context_input(native.session_id, source=session['source'],
                                      event_id='manual-native', content='Exact pending request')
    admitted = server._accept_tui_context_input(session, receipt.content,
        event_id=receipt.event_id, manual_recovery=True)
    assert admitted == receipt
    native.client.chat.completions.create.return_value = _mock_response(content='Done', finish_reason='stop')
    with patch.object(native, '_save_trajectory'), patch.object(native, '_cleanup_task_resources'):
        result = native.run_conversation(receipt.content, persist_user_event_id=receipt.event_id)
    assert result['completed'] and not result['failed'], result
    assert native.client.chat.completions.create.call_count == 1
    assert [row['content'] for row in db.get_messages(native.session_id) if row['role'] == 'user'] == [receipt.content]
    assert db.read_context_input(native.session_id, source=receipt.source, event_id=receipt.event_id) == receipt
    with pytest.raises(Exception, match='MANUAL_RECOVERY_UNSAFE'):
        server._accept_tui_context_input(session, receipt.content, event_id=receipt.event_id, manual_recovery=True)
