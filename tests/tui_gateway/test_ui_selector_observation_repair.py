"""Read-only runtime imports; synthetic records and stubbed side effects only."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tui_gateway import server

@pytest.fixture
def record(monkeypatch):
    r = {'session_key': 'synthetic-stored', 'agent': None, '_compute_host_active': True,
         'history_lock': threading.Lock(), 'history': [], 'history_version': 1,
         'running': False, '_metadata_mirror': {'model': 'A', 'provider': 'synthetic',
         'reasoning_effort': 'low', 'pending_runtime_options': None}}
    monkeypatch.setattr(server, '_sessions', {'synthetic-live': r})
    monkeypatch.setattr(server, '_load_cfg', lambda: {})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: True)
    monkeypatch.setattr(server, '_display_session_cwd', lambda s: '/synthetic')
    monkeypatch.setattr(server, '_project_info_for_cwd', lambda cwd: None)
    monkeypatch.setattr(server, '_session_live_title', lambda *a: '')
    monkeypatch.setattr(server, '_effective_terminal_backend', lambda: 'local')
    monkeypatch.setattr(server, '_session_usage_snapshot', lambda s: {})
    monkeypatch.setattr(server, '_probe_credentials', lambda a: None)
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock(side_effect=AssertionError('unexpected persistence')))
    monkeypatch.setattr(server, '_write_config_key', Mock(side_effect=AssertionError('unexpected profile write')))
    import hermes_cli.banner
    monkeypatch.setattr(hermes_cli.banner, 'get_update_result', lambda **kw: None)
    return r

def test_real_snapshot_and_publication_can_send_old_selection_after_new(record, monkeypatch):
    captured = threading.Event(); release = threading.Event(); publications = []
    def git_branch(cwd):
        if threading.current_thread().name == 'older-observation':
            captured.set()
            assert release.wait(5)
        return 'synthetic-branch'
    monkeypatch.setattr(server, '_git_branch_for_cwd', git_branch)
    monkeypatch.setattr(server, '_emit_unserialized', lambda event, sid, payload: publications.append(
        {'model': payload['model'], 'effort': payload['reasoning_effort']}))
    errors = []
    def older():
        try:
            server._emit('session.info', 'synthetic-live', server._session_info(None, record))
        except Exception as e:
            errors.append(repr(e))
    t = threading.Thread(target=older, name='older-observation', daemon=True)
    t.start()
    assert captured.wait(5)
    server._apply_compute_host_metadata_mirror(record, {'session_info': {
        'model': 'B', 'provider': 'synthetic', 'reasoning_effort': 'high', 'pending_runtime_options': None}})
    server._emit('session.info', 'synthetic-live', server._session_info(None, record))
    release.set(); t.join(5)
    assert not t.is_alive() and not errors
    print('WITNESS actual _session_info/_emit publication sequence:', json.dumps(publications))
    assert publications[-1] == {'model': 'B', 'effort': 'high'}

def test_combined_runtime_options_ack_must_update_reconstruction_pin(record, monkeypatch):
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_git_branch_for_cwd', lambda cwd: 'synthetic')
    record['create_reasoning_override'] = {'enabled': True, 'effort': 'low'}
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: SimpleNamespace(boot_id='synthetic-boot'))
    monkeypatch.setattr(server, '_send_compute_host_control', Mock(return_value={
        'type': 'control.ack', 'sid': 'synthetic-live', 'route_name': 'config.set.runtime_options',
        '_host_boot_id': 'synthetic-boot', 'result': {'key': 'runtime_options', 'value': {'effort': 'high'}, 'deferred': False},
        'session_info': {'reasoning_effort': 'high'}}))
    response = server._methods['config.set']('synthetic-request', {
        'session_id': 'synthetic-live', 'key': 'runtime_options', 'value': {'effort': 'high'}})
    assert response['result']['deferred'] is False
    snapshot = server._compute_host_turn_frame('synthetic-next', 'synthetic-live', record, '')
    print('WITNESS successful combined option ACK mirror/pin/next snapshot:', json.dumps({
        'mirror': record['_metadata_mirror']['reasoning_effort'],
        'pin': record['create_reasoning_override'], 'next_snapshot': snapshot['reasoning_config_override']}))
    assert snapshot['reasoning_config_override']['effort'] == 'high'
