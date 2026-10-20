"""Composition witnesses for Stop ownership and reliability close/recovery."""
import contextlib
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import context_stop_store_scope
from tui_gateway import server


@pytest.mark.parametrize('field', ['_stop_pending', '_stop_uncertain'])
def test_unresolved_stop_remains_an_owned_cleanup_barrier(field):
    session = {'agent': None, 'running': False,
               field: {'operation_id': 'original-stop'}}
    assert server._session_cleanup_unsettled(session) is True
    session.pop(field)
    assert server._session_cleanup_unsettled(session) is False


@pytest.mark.parametrize('terminal_type', ['turn.end', 'turn.error'])
def test_unknown_terminal_cannot_discharge_original_stop_or_take_a_new_cut(
    tmp_path, monkeypatch, terminal_type,
):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    host = SimpleNamespace(boot_id='boot')
    token = {
        'operation_id': 'original-stop',
        'store_scope': context_stop_store_scope(db.db_path),
        'host_supervisor': host,
        'session_key': 'stored', 'profile_home': None,
        'host_boot_id': 'boot', 'target_request_id': 'owned-turn',
        'request_id': 'original-control', 'input_release': threading.Event(),
        'terminal': {'type': terminal_type, 'sid': 'runtime',
                     'request_id': 'owned-turn', '_host_boot_id': 'boot',
                     'outcome': 'unknown'},
    }
    session = {'history_lock': threading.Lock(), 'session_key': 'stored',
               'profile_home': None, '_stop_uncertain': token,
               '_compute_host_active_request_id': 'owned-turn',
               '_host_delivery_uncertain': True, 'running': True}
    monkeypatch.setitem(server._sessions, 'runtime', session)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: host)
    monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(db))
    try:
        with pytest.raises(RuntimeError, match='pending or requires owner reconciliation'):
            server._interrupt_owned_compute_host_turn('runtime', session, 'new-control')
        assert session['_stop_uncertain'] is token
        assert session['_compute_host_active_request_id'] == 'owned-turn'
        assert session['running'] is True
        assert not token['input_release'].is_set()
        assert db.read_context_stop_operation('stored', operation_id='original-stop') is None
        assert db.read_context_stop('stored') is None
    finally:
        db.close()
