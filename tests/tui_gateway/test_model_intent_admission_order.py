"""Model intent ordering through real gateway admission and inert runtimes."""
import copy
import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes_cli import model_switch as ms
from hermes_state import SessionDB
from tests.hermes_cli.test_model_switch_route_contract import routes  # noqa: F401
from tests.hermes_cli.test_model_switch_runtime_boundary import agent, assert_route  # noqa: F401
from tests.tui_gateway.test_failed_turn_retention import turn_env  # noqa: F401
from tests.tui_gateway.test_model_once_turn_runtime_owner import live_turn  # noqa: F401
from tests.tui_gateway.test_model_switch_target_session_contract import gateway  # noqa: F401
from tests.tui_gateway.test_config_dispatch_responsiveness import Transport
from tui_gateway import server
from tui_gateway.transport import StdioTransport

REAL_THREAD = threading.Thread
REAL_CONFIG_SYNC = server._sync_agent_model_with_config
REAL_RESTART = server._restart_slash_worker


@pytest.fixture
def intent_turn(live_turn, routes, monkeypatch):
    session, events = live_turn
    session['running'] = False
    session['transport'] = Transport()
    routes.config['providers']['endpoint-c'] = {
        'name': 'Endpoint C', 'base_url': 'http://c.invalid/v1',
        'api_key': 'synthetic-key-c', 'default_model': 'model-c'}
    routes.write()
    db = SessionDB(routes.home / 'state.db')
    db.create_session(session['session_key'], source='tui')
    session['agent']._session_db = db
    monkeypatch.setattr(server, '_db', db)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a, **kw: False)
    monkeypatch.setattr(server, '_voice_mode_enabled', lambda: False)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_sync_bot_capabilities', lambda *a: None)
    yield session, events
    db.close()


def select(model, provider, *, once=False, confirmed=True):
    raw = f'{model} --provider {provider} ' + ('--once' if once else '--session')
    return {'id': 'pick-' + model, 'method': 'config.set', 'params': {
        'session_id': 'selected', 'key': 'model', 'value': raw,
        'confirm_expensive_model': confirmed}}


def run_turn(session, monkeypatch, rid='offline-turn'):
    observations = []
    value = session['agent']

    def offline(*a, **kw):
        info = server._session_info(value, session)
        observations.append((value.model, value.provider, info['model'], info['provider']))
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    monkeypatch.setattr(value, 'run_conversation', offline)
    session['running'] = True
    server._run_prompt_submit(rid, 'selected', session, 'offline input')
    session['_run_thread'].join(5)
    assert not session['_run_thread'].is_alive()
    return observations


@pytest.mark.parametrize('pause', ['admitted', 'executing', 'steered'])
def test_slow_idle_selection_cannot_repoint_admitted_send(intent_turn, monkeypatch, pause):
    session, _ = intent_turn
    value = session['agent']
    transport = session['transport']
    entered, resolve_release = threading.Event(), threading.Event()
    turn_entered, turn_release = threading.Event(), threading.Event()
    real_switch = ms.switch_model
    samples = {}
    monkeypatch.setattr(server.threading, 'Thread', REAL_THREAD)

    def slow(*a, **kw):
        entered.set()
        assert resolve_release.wait(5)
        return real_switch(*a, **kw)

    def ready(*a, **kw):
        if pause != 'executing':
            turn_entered.set()
            assert turn_release.wait(5)
        return None

    def offline(*a, **kw):
        samples['start'] = (value.model, value.provider, value.client)
        if pause == 'executing':
            turn_entered.set()
            assert turn_release.wait(5)
        samples['end'] = (value.model, value.provider, value.client)
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    monkeypatch.setattr(ms, 'switch_model', slow)
    monkeypatch.setattr(server, '_wait_agent_for_prompt', ready)
    monkeypatch.setattr(value, 'run_conversation', offline)
    original = (value.model, value.provider, value.client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(server, '_pool', pool)
        assert server.dispatch(select('model-b', 'endpoint-b'), transport) is None
        try:
            assert entered.wait(5)
            ack = server.dispatch({'id': 'send-a', 'method': 'prompt.submit', 'params': {
                'session_id': 'selected', 'text': 'offline input'}}, transport)
            assert not ack.get('error'), ack
            assert ack['result']['status'] == 'streaming'
            assert turn_entered.wait(5)
            assert session['running']
            resolve_release.set()
            assert transport.done.wait(5)
            if pause == 'steered':
                admitted_snapshot = session['inflight_turn']
                correction = server.dispatch({'id': 'steer-a', 'method': 'session.steer',
                    'params': {'session_id': 'selected', 'text': 'offline correction'}}, transport)
                assert correction['result']['status'] == 'queued'
                assert session['inflight_turn'] is not admitted_snapshot
                assert session['inflight_turn']['started_at'] is admitted_snapshot['started_at']
        finally:
            resolve_release.set()
            turn_release.set()
            thread = session.get('_run_thread')
            while thread is not None:
                thread.join(5)
                assert not thread.is_alive()
                successor = session.get('_run_thread')
                if successor is thread:
                    break
                thread = successor
    assert not session['_run_thread'].is_alive()
    assert samples == {'start': original, 'end': original}
    result = transport.frames[0]['result']
    assert result['deferred'] is True and result['confirm_required'] is False
    assert session['pending_model_switch']['display_model'] == 'model-b'
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    # The intent becomes eligible only after this admission has settled.
    monkeypatch.setattr(ms, 'switch_model', real_switch)
    assert run_turn(session, monkeypatch, 'next-b') == [
        ('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]
    assert_route(value, 'http://b.invalid/v1', 'synthetic-key-b')


def test_live_swap_and_once_publication_share_admission_lock(intent_turn, monkeypatch):
    session, _ = intent_turn
    value = session['agent']
    real_switch = value.switch_model
    lock_observations = []

    def switch(*a, **kw):
        acquired = session['history_lock'].acquire(blocking=False)
        if acquired:
            session['history_lock'].release()
        lock_observations.append(acquired)
        return real_switch(*a, **kw)

    monkeypatch.setattr(value, 'switch_model', switch)
    response = server.handle_request(select('model-b', 'endpoint-b', once=True))
    assert not response.get('error'), response
    assert lock_observations == [False]
    snapshot, runtime = server._consume_one_turn_model_runtime(session, value)
    assert snapshot['model'] == 'model-a' and runtime['active']
    assert_route(value, 'http://b.invalid/v1', 'synthetic-key-b')


def test_new_idle_choice_retires_older_deferred_choice(intent_turn, monkeypatch, routes):
    session, _ = intent_turn
    other_pending = {'raw': 'other --session', 'display_model': 'other'}
    server._sessions['other'] = {'pending_model_switch': other_pending}
    before = (routes.home / 'config.yaml').read_bytes()
    session['running'] = True
    queued = server.handle_request(select('model-b', 'endpoint-b'))
    assert queued['result']['deferred'] is True
    session['running'] = False
    chosen = server.handle_request(select('model-c', 'endpoint-c'))
    assert not chosen.get('error'), chosen
    assert chosen['result']['value'] == 'model-c'
    assert 'pending_model_switch' not in session
    info = server._session_info(session['agent'], session)
    assert (info['model'], info['provider']) == ('model-c', 'endpoint-c')
    assert run_turn(session, monkeypatch) == [('model-c', 'endpoint-c', 'model-c', 'endpoint-c')]
    assert_route(session['agent'], 'http://c.invalid/v1', 'synthetic-key-c')
    assert server._sessions['other']['pending_model_switch'] is other_pending
    assert (routes.home / 'config.yaml').read_bytes() == before


@pytest.mark.parametrize('failure', ['disabled', 'client', 'unconfirmed'])
def test_unsuccessful_idle_choice_preserves_deferred_intent(intent_turn, routes, monkeypatch, failure):
    session, _ = intent_turn
    value = session['agent']
    session['running'] = True
    assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
    pending = session['pending_model_switch']
    session['running'] = False
    old_client = value.client
    if failure == 'disabled':
        routes.config['providers']['endpoint-c']['enabled'] = False
        routes.write()
    elif failure == 'client':
        create = value._create_openai_client

        def fail(kwargs, **kw):
            if kwargs['base_url'] == 'http://c.invalid/v1':
                raise RuntimeError('offline client construction failed')
            return create(kwargs, **kw)

        monkeypatch.setattr(value, '_create_openai_client', fail)
    else:
        monkeypatch.setattr('hermes_cli.model_selection_guards.combined_selection_warning',
                            lambda *a, **kw: SimpleNamespace(message='offline consent required'))
    response = server.handle_request(select('model-c', 'endpoint-c', confirmed=failure != 'unconfirmed'))
    if failure == 'unconfirmed':
        assert response['result']['confirm_required'] is True
    else:
        assert response['error']['code'] == 5001
    assert session['pending_model_switch'] is pending
    assert value.client is old_client
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['history_lock'].acquire(blocking=False)
    session['history_lock'].release()
    assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


def test_replacing_unused_once_returns_to_original_runtime(intent_turn, monkeypatch, routes):
    session, _ = intent_turn
    pin = copy.deepcopy(session['model_override'])
    before = (routes.home / 'config.yaml').read_bytes()
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    original_restore = session['one_turn_model_restore']
    original_lease = session['_one_turn_model_runtime']
    assert not server.handle_request(select('model-c', 'endpoint-c', once=True)).get('error')
    assert session['one_turn_model_restore'] is original_restore
    assert session['_one_turn_model_runtime'] is not original_lease
    assert run_turn(session, monkeypatch) == [('model-c', 'endpoint-c', 'model-c', 'endpoint-c')]
    value = session['agent']
    assert (value.model, value.provider) == ('model-a', 'endpoint-a')
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['model_override'] == pin
    info = server._session_info(value, session)
    assert (info['model'], info['provider']) == ('model-a', 'endpoint-a')
    assert not session.get('_one_turn_model_runtime')
    assert (routes.home / 'config.yaml').read_bytes() == before


def test_durable_choice_supersedes_unused_once_baseline(intent_turn, monkeypatch):
    session, _ = intent_turn
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    assert not server.handle_request(select('model-c', 'endpoint-c')).get('error')
    assert not session.get('one_turn_model_restore') and not session.get('_one_turn_model_runtime')
    assert run_turn(session, monkeypatch) == [('model-c', 'endpoint-c', 'model-c', 'endpoint-c')]
    assert session['model_override']['model'] == 'model-c'
    assert_route(session['agent'], 'http://c.invalid/v1', 'synthetic-key-c')


@pytest.mark.parametrize('newer_choice', [False, True])
def test_publication_error_does_not_discard_or_resurrect_old_intent(intent_turn, monkeypatch, newer_choice):
    session, _ = intent_turn
    session['running'] = True
    assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
    pending = session['pending_model_switch']
    session['running'] = False
    collect = server._emit

    class FailingStream:
        def write(self, text):
            raise OSError(errno.EIO, 'offline event publication failed')

    failed_transport = StdioTransport(lambda: FailingStream(), threading.Lock())

    def publish(name, sid, payload=None):
        if name == 'session.info' and payload['model'] == 'model-c':
            if newer_choice:
                server._mirror_slash_side_effects('selected', session,
                    '/model model-d --provider endpoint-c --session')
            return failed_transport.write(server._event_frame(name, sid, payload))
        return collect(name, sid, payload)

    monkeypatch.setattr(server, '_emit', publish)
    response = server.handle_request(select('model-c', 'endpoint-c'))
    monkeypatch.setattr(server, '_emit', collect)
    assert response['error']['code'] == 5001
    assert session['history_lock'].acquire(blocking=False)
    session['history_lock'].release()
    if newer_choice:
        assert not session.get('pending_model_switch')
        assert session['model_override']['model'] == 'model-d'
        assert run_turn(session, monkeypatch) == [('model-d', 'endpoint-c', 'model-d', 'endpoint-c')]
    else:
        assert session['pending_model_switch'] is pending
        # The suffix error does not claim to roll back the already working C
        # client. B remains the previously acknowledged next-turn intent.
        assert session['agent'].model == 'model-c'
        assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


def test_internal_one_shot_restore_keeps_user_deferred_pick(intent_turn, monkeypatch):
    session, _ = intent_turn
    restore = {'override': session['model_override'], 'model': 'model-a', 'provider': 'endpoint-a'}
    # Use an inert custom runtime for the one-shot; its actual finally branch
    # restores through the same helper used by the MoA one-shot producer.
    server._apply_model_switch('selected', session, 'model-c --provider endpoint-c --session',
                               confirm_expensive_model=True, persist_override=False)
    session['moa_one_shot_restore'] = restore
    pending = []

    def offline(*a, **kw):
        assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
        pending.append(session['pending_model_switch'])
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    monkeypatch.setattr(session['agent'], 'run_conversation', offline)
    session['running'] = True
    server._run_prompt_submit('internal-one-shot', 'selected', session, 'offline input')
    session['_run_thread'].join(5)
    assert not session['_run_thread'].is_alive()
    assert session['agent'].model == 'model-a'
    assert session['pending_model_switch'] is pending[0]
    assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


def test_publication_error_after_once_completion_keeps_old_intent(intent_turn, monkeypatch):
    session, _ = intent_turn
    session['running'] = True
    assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
    pending = session['pending_model_switch']
    session['running'] = False
    collect = server._emit

    class FailingStream:
        def write(self, text):
            raise OSError(errno.EIO, 'offline once publication failed')

    failed_transport = StdioTransport(lambda: FailingStream(), threading.Lock())

    def publish(name, sid, payload=None):
        if name == 'session.info' and payload['model'] == 'model-c':
            monkeypatch.setattr(server, '_emit', collect)
            assert run_turn(session, monkeypatch, 'during-once-publication') == [
                ('model-c', 'endpoint-c', 'model-c', 'endpoint-c')]
            assert session['agent'].model == 'model-a'
            assert not session.get('_one_turn_model_runtime')
            restored = server._session_info(session['agent'], session)
            assert (restored['model'], restored['provider']) == ('model-a', 'endpoint-a')
            return failed_transport.write(server._event_frame(name, sid, payload))
        return collect(name, sid, payload)

    monkeypatch.setattr(server, '_emit', publish)
    response = server.handle_request(select('model-c', 'endpoint-c', once=True))
    monkeypatch.setattr(server, '_emit', collect)
    assert response['error']['code'] == 5001
    assert session['pending_model_switch'] is pending
    assert_route(session['agent'], 'http://a.invalid/v1', 'synthetic-key-a')
    assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


@pytest.mark.parametrize('ordering', ['once_success', 'c_failure_first', 'd_failure_first'])
def test_publication_error_after_newer_once_completion_does_not_restore_old_intent(intent_turn, monkeypatch, ordering):
    session, _ = intent_turn
    transport = session['transport']
    session['running'] = True
    assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
    old_pending = session['pending_model_switch']
    session['running'] = False
    entered, release = threading.Event(), threading.Event()
    turn_entered, turn_release = threading.Event(), threading.Event()
    turn_samples = []
    publisher = []
    newer_publisher = threading.get_ident()
    collect = server._emit
    monkeypatch.setattr(server.threading, 'Thread', REAL_THREAD)

    class InertWorker:
        def __init__(self, block=False):
            self._closed = False
            self.block = block

        def close(self):
            if self._closed:
                return
            self._closed = True
            if self.block:
                publisher.append(threading.get_ident())
                entered.set()
                assert release.wait(5)

    class FailingStream:
        def write(self, text):
            raise OSError(errno.EIO, 'offline late publication failed')

    failed_transport = StdioTransport(lambda: FailingStream(), threading.Lock())
    initial_worker = InertWorker(block=True)
    session['slash_worker'] = initial_worker
    monkeypatch.setattr(server, '_SlashWorker', lambda *a, **kw: InertWorker())
    monkeypatch.setattr(server, '_restart_slash_worker', REAL_RESTART)

    def publish(name, sid, payload=None):
        if name == 'session.info' and threading.get_ident() in publisher:
            return failed_transport.write(server._event_frame(name, sid, payload))
        if (name == 'session.info' and payload['model'] == 'model-d'
                and threading.get_ident() == newer_publisher and ordering != 'once_success'):
            if ordering == 'c_failure_first':
                release.set()
                assert transport.done.wait(5)
            return failed_transport.write(server._event_frame(name, sid, payload))
        return collect(name, sid, payload)

    monkeypatch.setattr(server, '_emit', publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(server, '_pool', pool)
        assert server.dispatch(select('model-c', 'endpoint-c'), transport) is None
        try:
            assert entered.wait(5) and initial_worker._closed
            pin = session['model_override']
            if ordering == 'once_success':
                server._mirror_slash_side_effects('selected', session,
                    '/model model-d --provider endpoint-c --once')
                assert session['agent'].model == 'model-d'
                assert run_turn(session, monkeypatch, 'newer-once') == [
                    ('model-d', 'endpoint-c', 'model-d', 'endpoint-c')]
                assert session['agent'].model == 'model-c'
                assert session['model_override'] is pin
                assert not session.get('_one_turn_model_runtime')
            else:
                output = server._mirror_slash_side_effects('selected', session,
                    '/model model-d --provider endpoint-c --session')
                assert 'publication failed' in output
                if ordering == 'd_failure_first':
                    # C still owns an unfinished suffix: its old B cannot
                    # become eligible merely because D released its claim.
                    server._apply_pending_model_switch('selected', session)
                    assert session['agent'].model == 'model-d'
                    value = session['agent']
                    admitted_runtime = (value.model, value.provider, value.client)

                    def ready(*a, **kw):
                        turn_entered.set()
                        assert turn_release.wait(5)
                        return None

                    def offline(*a, **kw):
                        turn_samples.append((value.model, value.provider, value.client))
                        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

                    monkeypatch.setattr(server, '_wait_agent_for_prompt', ready)
                    monkeypatch.setattr(value, 'run_conversation', offline)
                    ack = server.dispatch({'id': 'send-d-between-failures', 'method': 'prompt.submit',
                        'params': {'session_id': 'selected', 'text': 'offline input'}}, transport)
                    assert not ack.get('error'), ack
                    assert ack['result']['status'] == 'streaming'
                    assert turn_entered.wait(5) and session['running']
                    release.set()
                    assert transport.done.wait(5)
                    assert old_pending['after_inflight_turn'] is session['inflight_turn']
        finally:
            release.set()
            turn_release.set()
            thread = session.get('_run_thread')
            while thread is not None:
                thread.join(5)
                assert not thread.is_alive()
                successor = session.get('_run_thread')
                if successor is thread:
                    break
                thread = successor
        assert transport.done.wait(5)
    monkeypatch.setattr(server, '_emit', collect)
    assert transport.frames[0]['error']['code'] == 5001
    if ordering == 'once_success':
        assert not session.get('pending_model_switch')
        assert run_turn(session, monkeypatch) == [('model-c', 'endpoint-c', 'model-c', 'endpoint-c')]
    else:
        assert session['pending_model_switch'] is old_pending
        if ordering == 'd_failure_first':
            assert turn_samples == [admitted_runtime]
            assert_route(session['agent'], 'http://c.invalid/v1', 'synthetic-key-c')
            monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a, **kw: None)
        assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


def test_config_adoption_keeps_pick_fenced_after_current_admission(intent_turn, routes, monkeypatch):
    session, _ = intent_turn
    session.pop('model_override')
    routes.config['model'].update(default='model-c', provider='endpoint-c')
    routes.write()
    monkeypatch.setattr(server, '_sync_agent_model_with_config', REAL_CONFIG_SYNC)
    session['running'] = True
    with session['history_lock']:
        server._start_inflight_turn(session, 'already admitted')
    assert server.handle_request(select('model-b', 'endpoint-b'))['result']['deferred']
    pending = session['pending_model_switch']
    assert run_turn(session, monkeypatch) == [('model-c', 'endpoint-c', 'model-b', 'endpoint-b')]
    assert session['pending_model_switch'] is pending
    assert run_turn(session, monkeypatch) == [('model-b', 'endpoint-b', 'model-b', 'endpoint-b')]


@pytest.mark.parametrize('replacement', ['record', 'agent', 'transport'])
def test_delayed_idle_selection_refuses_changed_owner(intent_turn, monkeypatch, replacement):
    session, events = intent_turn
    value = session['agent']
    before = copy.deepcopy(session['model_override'])
    entered, release = threading.Event(), threading.Event()
    transport = session['transport']
    real_switch = ms.switch_model
    monkeypatch.setattr(server.threading, 'Thread', REAL_THREAD)

    def slow(*a, **kw):
        entered.set()
        assert release.wait(5)
        return real_switch(*a, **kw)

    monkeypatch.setattr(ms, 'switch_model', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(server, '_pool', pool)
        assert server.dispatch(select('model-b', 'endpoint-b'), transport) is None
        try:
            assert entered.wait(5)
            if replacement == 'record':
                server._sessions['selected'] = {'agent': SimpleNamespace(model='replacement')}
            elif replacement == 'agent':
                session['agent'] = SimpleNamespace(model='replacement')
            else:
                session['transport'] = Transport()
        finally:
            release.set()
        assert transport.done.wait(5)
    assert transport.frames[0]['error']['code'] == 5001
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['model_override'] == before
    assert not session.get('_config_set_pending')
    assert not events
