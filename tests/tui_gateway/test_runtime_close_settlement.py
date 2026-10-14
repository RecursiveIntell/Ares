"""Canonical server close retains exact live owners until actual settlement."""
import threading
import time
from types import SimpleNamespace

import pytest
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', source='desktop')
    db.create_session('sibling', source='desktop')
    finalized = threading.Event()
    closed = []
    agent = SimpleNamespace(session_id='owned', platform='desktop',
        interrupt=lambda *a, **k: None, close=lambda: closed.append('owned'))
    session = {'agent': agent, 'session_key': 'owned', 'history': [],
        'history_lock': threading.RLock(), 'running': True}
    monkeypatch.setattr(server, '_sessions', {'owner': session})
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_notify_session_boundary', lambda *a: finalized.set())
    monkeypatch.setattr(server, '_finish_bot_native_retirement', lambda *a: None)
    monkeypatch.setattr(server, '_flush_sessions_before_exit', lambda: None)
    monkeypatch.setattr(server, '_release_gateway_wake_owner', lambda: None)
    monkeypatch.setattr(server, '_TURN_SETTLE_BEFORE_CLOSE_SECONDS', 0.02)
    yield session, db, finalized, closed
    thread = session.get('_run_thread')
    if thread:
        thread.join(2)
    supervisor = session.get('_close_settlement_thread')
    if supervisor and supervisor.ident is not None:
        supervisor.join(2)
    db.close()


@pytest.mark.parametrize('path', ['close', 'shutdown', 'finalize'])
def test_live_non_custody_owner_finalizes_once_after_worker_exit(runtime, path):
    session, db, finalized, closed = runtime
    release = threading.Event()
    def work():
        release.wait(2)
        # Resources and the durable row are still usable under the live owner.
        db.append_message('owned', 'assistant', 'late tail')
        session['running'] = False
    thread = threading.Thread(target=work, daemon=True)
    session['_run_thread'] = thread
    thread.start()
    try:
        began = time.monotonic()
        if path == 'close':
            server._close_session_by_id('owner')
        elif path == 'shutdown':
            server._shutdown_sessions()
        else:
            server._finalize_session(session)
        assert time.monotonic() - began < 0.5
        assert not session.get('_finalized')
        assert not finalized.is_set()
        assert not closed
        assert db.get_session('owned')['ended_at'] is None
    finally:
        release.set()
        thread.join(2)
    assert finalized.wait(2), 'retained owner must finish late finalization'
    supervisor = session.get('_close_settlement_thread')
    if supervisor:
        supervisor.join(2)
    assert session['_finalized']
    server._teardown_session(session)
    if path != 'finalize':
        assert closed == ['owned']
    assert db.get_session('sibling')['ended_at'] is None
    assert [m['content'] for m in db.get_messages('owned')] == ['late tail']


def test_failed_owned_settlement_keeps_latch_and_resources(runtime):
    session, db, finalized, closed = runtime
    session['running'] = False
    live = threading.Event()
    live.set()
    session['agent']._has_owned_tool_work = live.is_set
    try:
        server._teardown_session(session)
        assert not finalized.is_set()
        assert not session.get('_finalized')
        assert not closed
    finally:
        live.clear()
    assert finalized.wait(2)


def test_source_close_exception_retains_resource_owner(runtime):
    session, db, finalized, closed = runtime
    session['running'] = False
    def fail():
        raise RuntimeError('controlled resource retirement failure')
    session['agent'].close = fail
    server._teardown_session(session)
    assert session.get('_close_cleanup_error') == 'controlled resource retirement failure'
    assert server._retiring_sessions[id(session)] is session
    assert not session.get('_teardown_complete')
    assert db.get_session('sibling')['ended_at'] is None


def test_tail_write_failure_does_not_spend_finalization_latch(runtime):
    session, db, finalized, closed = runtime
    session['running'] = False
    session['agent']._session_messages = [{'role': 'assistant', 'content': 'late tail'}]
    def fail(*a):
        raise OSError('controlled tail write failure')
    session['agent']._persist_session = fail
    server._teardown_session(session)
    assert not session.get('_finalized')
    assert not finalized.is_set()
    assert not closed
    assert server._retiring_sessions[id(session)] is session


def test_close_supervision_start_failure_retains_owner_and_allows_explicit_retry(runtime, monkeypatch):
    session, db, finalized, closed = runtime
    real_thread = server._RealThread
    class RefusedStart(real_thread):
        def start(self):
            raise RuntimeError('controlled supervision start refusal')
    monkeypatch.setattr(server, '_RealThread', RefusedStart)
    monkeypatch.setattr(server.threading, 'Thread', RefusedStart)
    try:
        server._finalize_session(session)
        assert not session.get('_finalized')
        assert not finalized.is_set() and not closed
        assert server._retiring_sessions[id(session)] is session
        assert session.get('_close_settlement_thread') is None
        assert session['_close_supervision_error'] == 'controlled supervision start refusal'
        assert db.get_session('owned')['ended_at'] is None
    finally:
        monkeypatch.setattr(server, '_RealThread', real_thread)
        monkeypatch.setattr(server.threading, 'Thread', real_thread)
        session['running'] = False
    server._finalize_session(session)
    assert finalized.wait(2)
    supervisor = session.get('_close_settlement_thread')
    if supervisor:
        supervisor.join(2)
    assert session['_finalized']
    assert '_close_supervision_error' not in session
