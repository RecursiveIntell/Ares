"""Receipt handoff may not bypass the native text-only dispatch contract.

Custody acceptance is not provider admission. Neutral native SQLite/images and
recording provider fixtures only; unknown persisted versions remain untouched.
"""
import json

import pytest

from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_context_media_custody import png, origin
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def witness(native):
    value = origin()
    value['stored_session_id'] = native.session_id
    value['profile_name'] = 'p1'
    return value


def handoff(native, receipt):
    return native.run_conversation('Neutral original intent',
        persist_user_message=receipt.content, persist_user_event_id=receipt.event_id,
        persist_user_input_receipt=receipt,
        persist_user_input_authorizer=lambda key, receipts: key == native.session_id)


@pytest.mark.parametrize('selected', [False, True])
def test_media_receipt_handoff_refuses_before_any_projection_or_dispatch(durable_agent, selected):
    native, db = durable_agent
    native.platform = 'desktop'
    native.context_rebase_enabled = selected
    receipt = db.accept_context_media_input(native.session_id, source='desktop', event_id='image',
        text='Neutral original intent',
        media=[{'data': png(), 'mime_type': 'image/png', 'display_name': 'neutral.png'}],
        desktop_origin=witness(native))
    before = db.read_pending_context_inputs(native.session_id)
    native.client.chat.completions.create.return_value = _mock_response(content='Forbidden', finish_reason='stop')
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_TEXT_ROUTE_REQUIRED'):
        response = handoff(native, receipt)
        pytest.fail('unqualified media handoff returned: ' + json.dumps({
            'completed': response.get('completed'), 'failed': response.get('failed'),
            'error': response.get('error'),
            'provider_calls': native.client.chat.completions.create.call_count,
            'message_shapes': [(row['role'], type(row['content']).__name__) for row in db.get_messages(native.session_id)],
            'phase_state': (db.read_context_input_work(native.session_id)['phase'] or {}).get('state')}))
    native.client.chat.completions.create.assert_not_called()
    assert db.get_messages(native.session_id) == []
    assert db.read_context_input_work(native.session_id)['phase'] is None
    assert db.read_pending_context_inputs(native.session_id) == before
    assert db.read_context_input_media(native.session_id, receipt) == (png(),)
    assert db.read_context_input_origin(native.session_id, receipt) == witness(native)


def test_supported_text_receipt_handoff_still_dispatches_once(durable_agent):
    native, db = durable_agent
    native.platform = 'desktop'
    receipt = db.accept_context_input(native.session_id, source='desktop', event_id='text',
        content='Neutral original intent', desktop_origin=witness(native))
    native.client.chat.completions.create.return_value = _mock_response(content='Controlled', finish_reason='stop')
    result = handoff(native, receipt)
    assert result['completed'] and not result['failed'], result
    assert native.client.chat.completions.create.call_count == 1
    assert [row['content'] for row in db.get_messages(native.session_id) if row['role'] == 'user'] == [receipt.content]
    assert db.read_context_input(native.session_id, source='desktop', event_id='text') == receipt


@pytest.mark.parametrize('selected', [False, True])
def test_queued_media_cannot_enter_a_later_text_turn_before_qualification(durable_agent, selected):
    native, db = durable_agent
    native.platform = 'desktop'
    native.context_rebase_enabled = selected
    media = db.accept_context_media_input(native.session_id, source='desktop', event_id='image',
        text='Neutral original intent',
        media=[{'data': png(), 'mime_type': 'image/png', 'display_name': 'neutral.png'}],
        desktop_origin=witness(native))
    text = db.accept_context_input(native.session_id, source='desktop', event_id='text',
        content='Neutral original intent', desktop_origin=witness(native))
    before = db.read_pending_context_inputs(native.session_id)
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_TEXT_ROUTE_REQUIRED'):
        handoff(native, text)
    native.client.chat.completions.create.assert_not_called()
    assert db.get_messages(native.session_id) == []
    assert db.read_context_input_work(native.session_id)['phase'] is None
    assert db.read_pending_context_inputs(native.session_id) == before == (media, text)
    assert db.read_context_input_media(native.session_id, media) == (png(),)


@pytest.mark.parametrize('version', ['SessionDBContextInputV999', [], None])
def test_unknown_receipt_schema_is_parked_without_lossy_projection(durable_agent, version):
    native, db = durable_agent
    native.platform = 'desktop'
    receipt = db.accept_context_input(native.session_id, source='desktop', event_id='text',
        content='Neutral original intent', desktop_origin=witness(native))
    rows = []
    def corrupt(conn):
        key, raw = conn.execute("SELECT key,value FROM state_meta WHERE key LIKE '%:event:1'").fetchone()
        value = json.loads(raw)
        value['schema'] = version
        replacement = json.dumps(value)
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (replacement, key))
        rows.append((key, replacement))
    db._execute_write(corrupt)
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_RECORD_INVALID'):
        handoff(native, receipt)
    native.client.chat.completions.create.assert_not_called()
    assert db.get_messages(native.session_id) == []
    assert db.get_meta(rows[0][0]) == rows[0][1]


@pytest.mark.parametrize('version', ['SessionDBContextInputOriginV999', [], None])
def test_unknown_origin_schema_is_parked_before_projection(durable_agent, version):
    native, db = durable_agent
    native.platform = 'desktop'
    receipt = db.accept_context_input(native.session_id, source='desktop', event_id='text',
        content='Neutral original intent', desktop_origin=witness(native))
    rows = []
    def corrupt(conn):
        key, raw = conn.execute("SELECT key,value FROM state_meta WHERE key LIKE '%:origin'").fetchone()
        value = json.loads(raw)
        value['schema'] = version
        replacement = json.dumps(value)
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (replacement, key))
        rows.append((key, replacement))
    db._execute_write(corrupt)
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_ORIGIN_INVALID'):
        handoff(native, receipt)
    native.client.chat.completions.create.assert_not_called()
    assert db.get_messages(native.session_id) == []
    assert db.get_meta(rows[0][0]) == rows[0][1]
