"""Offline parent/real child-pipe Stop and immediate follow-up integration.

The child uses the existing explicit synthetic-turn fixture, never a provider.
"""
import contextlib
import json
import os
from pathlib import Path
import threading

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.host_supervisor import HostSupervisor


def test_real_pipe_stop_then_post_cut_followup_runs_once(tmp_path, monkeypatch):
    home = tmp_path / 'fixture-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(home / 'state.db')
    db.create_session('stored', source='desktop')
    session = {'history_lock': threading.Lock(), 'history': [], 'agent': None,
               'session_key': 'stored', 'profile_home': str(home), 'running': True,
               'source': 'desktop', 'cwd': str(tmp_path), 'queued_prompt': None,
               '_compute_host_active': True}
    monkeypatch.setitem(server._sessions, 'fixture', session)
    host = HostSupervisor(registry_path=home / 'host.json', autostart=False,
        rpc_sink=lambda frame: None, heartbeat_secs=0, respawn_max=0,
        env={'HOME': str(tmp_path), 'PATH': os.environ.get('PATH', ''),
             'PYTHONPATH': str(Path(__file__).resolve().parents[2]), 'HERMES_HOME': str(home),
             'HERMES_ISO_CERTIFY_SYNTH_TURN': '1', 'HERMES_COMPUTE_HOST_HEARTBEAT_SECS': '0'})
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: host)
    monkeypatch.setattr(server, '_compute_host_supervisor', host)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_session_db', lambda scope: contextlib.nullcontext(db))
    monkeypatch.setattr(server, '_clear_pending', lambda *a: None)
    monkeypatch.setattr(server, '_clear_inflight_turn', lambda s: s.pop('inflight_turn', None))
    monkeypatch.setattr(server, '_session_info', lambda agent, s, *, lock_held=False: {'running': bool(s.get('running') or s.get('_stop_pending') or s.get('_stop_uncertain'))})
    events = []
    monkeypatch.setattr(server, '_emit', lambda event, sid, payload: events.append((event, payload)))
    started = threading.Event()
    finished = threading.Event()
    admissions = []
    terminals = []
    original_frame = host._handle_host_frame
    original_done = server._on_compute_host_turn_done
    original_interrupt = host.interrupt

    def capture(frame):
        if frame.get('type') == 'turn.started':
            admissions.append(frame['request_id'])
            started.set()
        original_frame(frame)

    def complete(rid, sid, state, frame):
        terminals.append(frame)
        original_done(rid, sid, state, frame)
        if len(terminals) == 2:
            finished.set()

    def stop_and_enqueue(sid, **kwargs):
        ack = original_interrupt(sid, **kwargs)
        assert ack['applied'] is True
        response = server._handle_busy_submit('followup', 'fixture', session,
            json.dumps({'duration_s': 0.05, 'chunk': 100}), None, queued=True)
        assert response['result']['status'] == 'queued'
        return ack

    monkeypatch.setattr(host, '_handle_host_frame', capture)
    monkeypatch.setattr(server, '_on_compute_host_turn_done', complete)
    monkeypatch.setattr(host, 'interrupt', stop_and_enqueue)
    try:
        result = server._submit_prompt_to_compute_host('first', 'fixture', session,
            json.dumps({'duration_s': 5, 'chunk': 100}))
        assert result['result']['status'] == 'streaming'
        assert started.wait(10)
        assert server._interrupt_session_turn('fixture', session, request_id='stop')
        assert finished.wait(10), {
            'admissions': admissions, 'terminals': terminals,
            'state': {key: session.get(key) for key in ('running', '_turn_cancel_requested',
                '_queued_prompt_generation', '_last_stop_queue_generation', 'queued_prompt',
                '_compute_host_active_request_id', '_stop_pending', '_stop_uncertain')},
            'events': events,
        }
        assert len(admissions) == len(set(admissions)) == 2
        assert all(frame['type'] == 'turn.end' for frame in terminals)
        assert terminals[0]['interrupted'] is True
        assert not session.get('_stop_pending') and not session.get('_stop_uncertain')
        assert not session.get('_compute_host_active_request_id')
        assert not session.get('queued_prompt')
        assert session['running'] is False
        assert not any(payload.get('status') == 'error' for _, payload in events)
    finally:
        host.shutdown()
        db.close()
