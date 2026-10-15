"""Atomic selector capture and freshness use the canonical history lock."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from tui_gateway import server


def prepare(monkeypatch, record):
    monkeypatch.setattr(server, '_sessions', {'r': record})
    monkeypatch.setattr(server, '_load_cfg', lambda: {'model': {'default': 'A', 'provider': 'pA'}})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: False)
    monkeypatch.setattr(server, '_display_session_cwd', lambda s: '/synthetic')
    monkeypatch.setattr(server, '_git_branch_for_cwd', lambda cwd: '')
    monkeypatch.setattr(server, '_project_info_for_cwd', lambda cwd: None)
    monkeypatch.setattr(server, '_session_live_title', lambda *a: '')
    monkeypatch.setattr(server, '_session_usage_snapshot', lambda s: {})
    monkeypatch.setattr(server, '_probe_credentials', lambda a: None)
    import hermes_cli.banner
    monkeypatch.setattr(hermes_cli.banner, 'get_update_result', lambda **kw: None)


def test_writer_cannot_split_model_provider_capture(monkeypatch):
    lock = threading.Lock()
    captured = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    tuple_state = {'model': 'A', 'provider': 'pA'}
    observed_lock = []
    reader_errors = []
    result = []

    class Agent:
        reasoning_config = {'enabled': True, 'effort': 'low'}
        service_tier = None
        @property
        def model(self):
            observed_lock.append(lock.locked())
            captured.set()
            assert release.wait(5)
            return tuple_state['model']
        @property
        def provider(self):
            return tuple_state['provider']

    agent = Agent()
    record = {'session_key': 'stored', 'agent': agent, 'history_lock': lock, 'history': [], 'running': False}
    prepare(monkeypatch, record)

    def reader():
        try:
            result.append(server._session_info(agent, record))
        except BaseException as error:
            reader_errors.append(error)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert captured.wait(5)
    # Exact nonblocking lock witness, not a negative timing/sleep assertion.
    assert lock.acquire(blocking=False) is False

    def writer():
        attempted.set()
        with lock:
            tuple_state.update(model='B', provider='pB')
    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    assert attempted.wait(5)
    release.set()
    thread.join(5); worker.join(5)
    assert not thread.is_alive() and not worker.is_alive() and not reader_errors
    assert all(observed_lock)
    assert (result[0]['model'], result[0]['provider']) == ('A', 'pA')
    assert tuple_state == {'model': 'B', 'provider': 'pB'}


def test_mutation_without_another_read_also_revokes_old_snapshot(monkeypatch):
    agent = SimpleNamespace(model='A', provider='pA', reasoning_config={'enabled': True, 'effort': 'low'}, service_tier=None)
    record = {'session_key': 'stored', 'agent': agent, 'history_lock': threading.Lock(), 'history': [], 'running': False}
    prepare(monkeypatch, record)
    old = server._session_info(agent, record)
    with record['history_lock']:
        agent.model = 'B'; agent.provider = 'pB'
    published = Mock()
    monkeypatch.setattr(server, '_emit_unserialized', published)
    server._emit('session.info', 'r', old)
    published.assert_not_called()
    new = server._session_info(agent, record)
    assert new['selector_version']['revision'] > old['selector_version']['revision']
    server._emit('session.info', 'r', new)
    published.assert_called_once()


def test_known_locked_caller_does_not_reacquire_plain_lock(monkeypatch):
    agent = SimpleNamespace(model='A', provider='pA', reasoning_config={}, service_tier=None)
    record = {'session_key': 'stored', 'agent': agent, 'history_lock': threading.Lock(), 'history': [], 'running': False}
    prepare(monkeypatch, record)
    with record['history_lock']:
        result = server._session_info(agent, record, lock_held=True)
    assert result['model'] == 'A'
