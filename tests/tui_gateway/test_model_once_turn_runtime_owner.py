"""One-turn selection ownership through the real gateway turn driver."""
import copy
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.config import load_config
from hermes_state import SessionDB
from tests.hermes_cli.test_model_switch_route_contract import routes  # noqa: F401
from tests.hermes_cli.test_model_switch_runtime_boundary import agent, assert_route  # noqa: F401
from tests.tui_gateway.test_failed_turn_retention import _session, turn_env  # noqa: F401
from tests.tui_gateway.test_model_switch_target_session_contract import gateway  # noqa: F401
from tests.run_agent.test_switch_model_rollback import _make_agent_openrouter
from tui_gateway import server

_REAL_THREAD = threading.Thread


@pytest.fixture
def live_turn(routes, gateway, turn_env, monkeypatch):
    session, events = gateway
    value = session['agent']
    value.session_id = 'stored-once'
    value.platform = 'tui'
    value.compression = SimpleNamespace(update_model=lambda *a, **kw: None)
    session.update(_session(value, session_key=value.session_id, running=True,
                            profile_home=str(routes.home), model_override={'model': 'model-a', 'provider': 'endpoint-a'}))
    monkeypatch.setattr(server, '_load_cfg', load_config)
    monkeypatch.setattr(server, '_emit', lambda name, sid, payload=None: events.append((name, sid, payload)))
    monkeypatch.setattr(server, '_sync_agent_compression_with_config', lambda *a: None)
    monkeypatch.setattr(server, '_voice_tts_enabled', lambda: False)
    monkeypatch.setattr(server, '_load_interim_assistant_messages', lambda: False)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *a: (threading.Event(), SimpleNamespace(join=lambda *a, **kw: None)))
    monkeypatch.setattr(server, '_notify_session_boundary', lambda *a: None)
    monkeypatch.setattr(server, 'record_turn_start', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_retire_turn_marker', lambda *a: None)
    return session, events


def once(session):
    result = server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --once',
                                      confirm_expensive_model=True, persist_override=False)
    assert result['scope'] == 'once'


@pytest.mark.parametrize('outcome', ['complete', 'returned_error', 'raised_error', 'interrupted'])
def test_active_once_metadata_and_restoration_through_real_turn(live_turn, monkeypatch, outcome):
    session, events = live_turn
    value = session['agent']
    pin = copy.deepcopy(session['model_override'])
    once(session)
    session['_metadata_mirror'] = {'model': 'model-a', 'provider': 'endpoint-a', 'model_ready': False}
    observations = []

    def offline(*args, **kwargs):
        info = server._session_info(value, session)
        observations.append((value.model, value.provider, info['model'], info['provider']))
        assert 'one_turn_model_restore' not in session
        assert_route(value, 'http://b.invalid/v1', 'synthetic-key-b')
        if outcome == 'raised_error':
            raise RuntimeError('inert conversation error')
        if outcome == 'returned_error':
            return {'final_response': '', 'error': 'inert returned error', 'completed': False, 'api_calls': 0}
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0,
                'interrupted': outcome == 'interrupted'}

    monkeypatch.setattr(value, 'run_conversation', offline)
    server._run_prompt_submit('once-request', 'selected', session, 'offline input')
    assert observations == [('chosen-model', 'endpoint-b', 'chosen-model', 'endpoint-b')]
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['model_override'] == pin
    info = server._session_info(value, session)
    assert (info['model'], info['provider']) == ('model-a', 'endpoint-a')
    assert not session.get('_one_turn_model_runtime')
    assert not session.get('one_turn_model_restore')
    assert events


def test_setup_error_restores_once_and_retires_owned_state(live_turn, monkeypatch):
    session, _ = live_turn
    once(session)

    def fail(*a, **kw):
        raise RuntimeError('inert turn setup error')

    monkeypatch.setattr(server, '_wire_callbacks', fail)
    server._run_prompt_submit('once-request', 'selected', session, 'offline input')
    assert_route(session['agent'], 'http://a.invalid/v1', 'synthetic-key-a')
    assert not session.get('_one_turn_model_runtime')
    assert not session.get('one_turn_model_restore')


@pytest.mark.parametrize('flag', ['_closing', '_turn_cancel_requested'])
def test_cancel_before_turn_does_not_consume_queued_once(live_turn, monkeypatch, flag):
    session, _ = live_turn
    once(session)
    snapshot = session['one_turn_model_restore']
    session[flag] = True
    assert server._run_prompt_submit('cancelled-request', 'selected', session, 'offline input') is False
    assert session['one_turn_model_restore'] is snapshot
    state = session.get('_one_turn_model_runtime')
    assert not state or not state.get('active')


@pytest.mark.parametrize('replacement_kind', ['agent', 'session'])
def test_retired_once_owner_does_not_restore_replacement(live_turn, monkeypatch, replacement_kind):
    session, _ = live_turn
    value = session['agent']
    once(session)
    replacement = _make_agent_openrouter()
    replacement.quiet_mode = True
    replacement._create_openai_client = lambda kwargs, **kw: SimpleNamespace(kwargs=kwargs.copy())
    replacement.switch_model('replacement-model', 'replacement-provider', 'synthetic-replacement-key', 'http://replacement.invalid/v1', 'chat_completions')
    new_record = _session(replacement, model_override={'model': 'replacement-model', 'provider': 'replacement-provider'})

    restore_side_effects = []
    monkeypatch.setattr(server, '_restart_slash_worker', lambda *a: restore_side_effects.append('restart'))
    monkeypatch.setattr(server, '_persist_live_session_runtime', lambda *a: restore_side_effects.append('persist-runtime'))
    monkeypatch.setattr(server, '_persist_live_session_system_prompt', lambda *a: restore_side_effects.append('persist-prompt'))

    def replace_before_dispatch(*a):
        session['agent'] = replacement
        session['model_override'] = new_record['model_override']

    def offline(*a, **kw):
        if replacement_kind == 'session':
            server._sessions['selected'] = new_record
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    if replacement_kind == 'agent':
        monkeypatch.setattr(server, '_sync_bot_capabilities', replace_before_dispatch)
        monkeypatch.setattr(replacement, 'run_conversation', offline)
    else:
        monkeypatch.setattr(value, 'run_conversation', offline)
    server._run_prompt_submit('once-request', 'selected', session, 'offline input')
    assert (replacement.model, replacement.provider) == ('replacement-model', 'replacement-provider')
    assert_route(replacement, 'http://replacement.invalid/v1', 'synthetic-replacement-key')
    assert not session.get('_one_turn_model_runtime')
    assert not restore_side_effects


def test_deferred_once_is_consumed_and_restored_on_its_first_turn(live_turn, monkeypatch):
    session, _ = live_turn
    session['pending_model_switch'] = {
        'raw': 'chosen-model --provider endpoint-b --once', 'confirm_expensive_model': True,
        'display_model': 'chosen-model', 'display_provider': 'endpoint-b'}
    value = session['agent']
    observations = []

    def offline(*a, **kw):
        info = server._session_info(value, session)
        observations.append((value.model, value.provider, info['model'], info['provider']))
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    monkeypatch.setattr(value, 'run_conversation', offline)
    server._run_prompt_submit('queued-once-request', 'selected', session, 'offline input')
    assert observations == [('chosen-model', 'endpoint-b', 'chosen-model', 'endpoint-b')]
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')
    assert not session.get('one_turn_model_restore')
    assert not session.get('_one_turn_model_runtime')


def test_next_real_turn_returns_to_saved_pin(live_turn, monkeypatch):
    session, _ = live_turn
    value = session['agent']
    once(session)
    observations = []

    def offline(*a, **kw):
        info = server._session_info(value, session)
        observations.append((value.model, value.provider, info['model'], info['provider']))
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    monkeypatch.setattr(value, 'run_conversation', offline)
    server._run_prompt_submit('once-request', 'selected', session, 'offline input')
    session['running'] = True
    server._run_prompt_submit('next-request', 'selected', session, 'next offline input')
    assert observations == [('chosen-model', 'endpoint-b', 'chosen-model', 'endpoint-b'),
                            ('model-a', 'endpoint-a', 'model-a', 'endpoint-a')]
    assert_route(value, 'http://a.invalid/v1', 'synthetic-key-a')


def test_restore_failure_is_visible_and_keeps_actual_runtime_metadata(live_turn, monkeypatch):
    session, events = live_turn
    value = session['agent']
    pin = copy.deepcopy(session['model_override'])
    once(session)
    monkeypatch.setattr(value, 'run_conversation', lambda *a, **kw: {
        'final_response': 'offline', 'completed': False, 'api_calls': 0})

    def fail(*a, **kw):
        raise RuntimeError('inert restore failure')

    monkeypatch.setattr(server, '_restore_agent_model_runtime', fail)
    server._run_prompt_submit('once-request', 'selected', session, 'offline input')
    assert_route(value, 'http://b.invalid/v1', 'synthetic-key-b')
    info = server._session_info(value, session)
    assert (info['model'], info['provider']) == ('chosen-model', 'endpoint-b')
    assert session['model_override'] == pin
    assert session['_one_turn_model_runtime']['active'] is False
    assert session['_one_turn_model_runtime']['restore_failed'] is True
    assert any(name == 'error' and 'Could not restore' in payload['message'] for name, _, payload in events)


def test_stale_queued_once_owner_fails_before_dispatch(live_turn, monkeypatch):
    session, events = live_turn
    once(session)
    replacement = _make_agent_openrouter()
    replacement.quiet_mode = True
    session['agent'] = replacement
    dispatched = []
    monkeypatch.setattr(replacement, 'run_conversation', lambda *a, **kw: dispatched.append(True))
    server._run_prompt_submit('stale-once-request', 'selected', session, 'offline input')
    assert not dispatched
    assert not session.get('_one_turn_model_runtime')
    assert not session.get('one_turn_model_restore')
    assert any(name == 'message.complete' and payload.get('status') == 'error' for name, _, payload in events)


@pytest.fixture
def bot_refresh(live_turn, routes, monkeypatch, tmp_path):
    """Real turn/refresh/store/switch boundaries with an inert SDK constructor."""
    session, events = live_turn
    # Native close checks that the registry lock is released. Production
    # executes on a new thread; an inline thread would inherit the caller lock.
    monkeypatch.setattr(server.threading, 'Thread', _REAL_THREAD)
    original = session['agent']
    target = SessionDB(routes.home / 'state.db')
    launch = SessionDB(tmp_path / 'launch-state.db')
    target.create_session(original.session_id, source='tui')
    original._session_db = target
    original._owns_session_db = True
    original._session_title_hint = 'Bot Chat'
    session['bot_caps_seen'] = 'old-caps'
    monkeypatch.setattr(server, '_db', launch)
    monkeypatch.setattr('tools.bot_mode_probe.capability_fingerprint', lambda home: 'new-caps')
    built, observations = [], []

    def offline(*args, **kwargs):
        executing = session['agent']
        info = server._session_info(executing, session)
        observations.append((executing.model, executing.provider, info['model'], info['provider']))
        assert_route(executing, 'http://b.invalid/v1', 'synthetic-key-b')
        assert 'one_turn_model_restore' not in session
        return {'final_response': 'offline', 'completed': False, 'api_calls': 0}

    def construct(sid, key, **kw):
        new = _make_agent_openrouter()
        new.quiet_mode = True
        new._create_openai_client = lambda kwargs, **extra: SimpleNamespace(kwargs=kwargs.copy())
        runtime = kw['model_override']
        new.switch_model(runtime['model'], runtime['provider'], runtime['api_key'],
                         runtime['base_url'], runtime['api_mode'])
        new.session_id = kw['session_id']
        new.platform = 'tui'
        new._session_db = kw['session_db']
        new._owns_session_db = False
        new.reasoning_config = kw['reasoning_config_override']
        new.service_tier = kw['service_tier_override']
        new.context_compressor = SimpleNamespace(update_model=lambda *a, **kw: None)
        new.run_conversation = offline
        built.append(new)
        return new

    monkeypatch.setattr(server, '_make_agent', construct)
    monkeypatch.setattr(original, 'run_conversation', offline)
    yield SimpleNamespace(session=session, original=original, target=target, launch=launch,
                          built=built, observations=observations, events=events, construct=construct)
    server._sessions.clear()
    target.close()
    launch.close()


@pytest.mark.parametrize('outcome', ['complete', 'returned_error', 'raised_error', 'interrupted'])
def test_real_turn_canonical_refresh_transfers_once_owner_and_restores(bot_refresh, monkeypatch, outcome):
    owner = bot_refresh
    session = owner.session
    pin = copy.deepcopy(session['model_override'])
    once(session)
    runtime_owner = session['_one_turn_model_runtime']
    actual_construct = owner.construct

    def construct(*a, **kw):
        new = actual_construct(*a, **kw)
        offline = new.run_conversation

        def run(*a, **kw):
            result = offline(*a, **kw)
            assert session['_one_turn_model_runtime'] is runtime_owner
            assert runtime_owner['agent'] is new and runtime_owner['active']
            if outcome == 'raised_error':
                raise RuntimeError('inert refreshed turn error')
            if outcome == 'returned_error':
                return {**result, 'error': 'inert returned error'}
            return {**result, 'interrupted': outcome == 'interrupted'}

        new.run_conversation = run
        return new

    monkeypatch.setattr(server, '_make_agent', construct)
    server._run_prompt_submit('once-refresh-request', 'selected', session, 'offline input')
    session['_run_thread'].join(timeout=10)
    assert not session['_run_thread'].is_alive()
    assert len(owner.built) == 1
    successor = session['agent']
    assert successor is owner.built[0] and successor is not owner.original
    assert owner.observations == [('chosen-model', 'endpoint-b', 'chosen-model', 'endpoint-b')]
    assert (successor.model, successor.provider) == ('model-a', 'endpoint-a')
    assert_route(successor, 'http://a.invalid/v1', 'synthetic-key-a')
    assert session['model_override'] == pin
    assert not session.get('_one_turn_model_runtime')
    assert not session.get('one_turn_model_restore')
    assert successor._owns_session_db and not owner.original._owns_session_db
    assert successor._session_db is owner.target
    assert owner.target.get_session(successor.session_id)['ended_at'] is None
    assert owner.launch.get_session(successor.session_id) is None


@pytest.mark.parametrize('retirement', ['exited', 'unresolved', 'shared', 'busy'])
def test_real_once_refresh_preserves_native_retirement_ownership(bot_refresh, monkeypatch, retirement):
    from tests.tui_gateway.test_bot_native_retirement import native_session

    owner = bot_refresh
    session = owner.session
    once(session)
    native, transport = native_session()
    owner.original._codex_session = native
    closes = []
    if retirement == 'unresolved':
        monkeypatch.setattr(transport, 'close', lambda: closes.append('attempt'))
    elif retirement == 'shared':
        server._sessions['other'] = {'agent': SimpleNamespace(_codex_session=native)}
    elif retirement == 'busy':
        native._active_turn_id = 'offline-active-turn'
    server._run_prompt_submit('once-native-refresh-request', 'selected', session, 'offline input')
    session['_run_thread'].join(timeout=10)
    assert not session['_run_thread'].is_alive()
    successor = session['agent']
    assert (successor.model, successor.provider) == ('model-a', 'endpoint-a')
    assert_route(successor, 'http://a.invalid/v1', 'synthetic-key-a')
    assert not session.get('_one_turn_model_runtime')
    if retirement in ('shared', 'busy'):
        assert successor is owner.original
        assert not owner.built and not owner.observations
        assert owner.original._codex_session is native and not native._closed
        assert not transport.closes and '_bot_native_retirement' not in session
        assert any(name == 'message.complete' and payload.get('status') == 'error'
                   for name, _, payload in owner.events)
    else:
        assert successor is owner.built[0]
        assert owner.observations == [('chosen-model', 'endpoint-b', 'chosen-model', 'endpoint-b')]
        assert owner.original._codex_session is None and native._closed
        assert successor._session_db is owner.target and successor._owns_session_db
        if retirement == 'exited':
            assert transport.closes == 1 and not transport.is_alive()
            assert '_bot_native_retirement' not in session
        else:
            pending = session['_bot_native_retirement']
            assert pending['native'] is native and pending['client'] is transport
            assert pending['status'] == 'unresolved' and closes == ['attempt']
            monkeypatch.setattr('tools.bot_mode_probe.capability_fingerprint', lambda home: 'later-caps')
            with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_RETIREMENT_PENDING'):
                server._sync_bot_capabilities('selected', session)
            assert closes == ['attempt']


@pytest.mark.parametrize('change', ['state', 'agent', 'registry'])
def test_canonical_refresh_refuses_changed_once_owner_during_constructor(bot_refresh, monkeypatch, change):
    owner = bot_refresh
    session = owner.session
    once(session)
    state = session['_one_turn_model_runtime']
    actual_construct = owner.construct

    def construct(*a, **kw):
        new = actual_construct(*a, **kw)
        if change == 'state':
            session['_one_turn_model_runtime'] = dict(state)
        elif change == 'agent':
            session['agent'] = SimpleNamespace()
        else:
            server._sessions['selected'] = dict(session)
        return new

    monkeypatch.setattr(server, '_make_agent', construct)
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_OWNER_CHANGED'):
        server._sync_bot_capabilities('selected', session)
    assert session['agent'] is not owner.built[0]
    assert state['agent'] is owner.original
    assert not owner.built[0]._owns_session_db


@pytest.mark.parametrize('one_turn', [True, False])
def test_once_publication_and_clear_hold_consumer_lock(live_turn, one_turn):
    original, _ = live_turn
    transitions = []

    class CheckedSession(dict):
        def _check_transition(self, key):
            if key in {'one_turn_model_restore', '_one_turn_model_runtime'}:
                acquired = self['history_lock'].acquire(blocking=False)
                if acquired:
                    self['history_lock'].release()
                assert not acquired, 'consumer can observe an incomplete ownership transition'
                transitions.append(key)

        def __setitem__(self, key, value):
            self._check_transition(key)
            return super().__setitem__(key, value)

        def pop(self, key, *default):
            self._check_transition(key)
            return super().pop(key, *default)

    session = CheckedSession(original)
    server._sessions['selected'] = session
    suffix = '--once' if one_turn else '--session'
    result = server._apply_model_switch('selected', session,
                                      'chosen-model --provider endpoint-b ' + suffix,
                                      confirm_expensive_model=True, persist_override=False)
    assert result['scope'] == ('once' if one_turn else 'session')
    assert transitions == ['one_turn_model_restore', '_one_turn_model_runtime']
    if one_turn:
        snapshot, runtime = server._consume_one_turn_model_runtime(session, session['agent'])
        assert snapshot['model'] == 'model-a'
        assert runtime['active'] and server._owns_one_turn_model_runtime(session, session['agent'], runtime)
    else:
        assert 'one_turn_model_restore' not in session
        assert '_one_turn_model_runtime' not in session
