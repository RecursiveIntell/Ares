"""Native-only retirement through real refresh, resolver, constructor and DB.

Only external discovery/client effects are inert. Native session.close is real,
with a fake owned transport; no native binary, account or network is used.
"""
from types import SimpleNamespace

import pytest

from tests.tui_gateway.test_prompt_recovery_contract import turn_env  # noqa: F401
from tests.tui_gateway.test_bot_capability_refresh import env, SID, KEY  # noqa: F401
from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import runtime_provider as rp
from run_agent import AIAgent
from tui_gateway import server

REAL_AGENT = AIAgent
REAL_RESOLVE = server._resolve_runtime_with_fallback


class OwnedTransport:
    def __init__(self):
        self.closes = 0
        self.native = None
        self.alive = True

    def close(self):
        assert not server._sessions_lock._is_owned(), 'close blocked under registry lock'
        assert self.native._active_turn_lock.acquire(blocking=False)
        self.native._active_turn_lock.release()
        self.closes += 1
        self.alive = False

    def is_alive(self):
        return self.alive


def native_session(client=None):
    native = CodexAppServerSession(model='offline-model', cwd='/tmp')
    client = client or OwnedTransport()
    if client.native is None:
        client.native = native
    native._client = client
    native._thread_id = 'offline-thread'
    return native, client


@pytest.fixture(params=['openai', 'openai-codex'])
def native_env(env, monkeypatch, request):
    provider = request.param
    config = {'model': {'provider': provider, 'default': 'offline-model',
                        'api_mode': 'codex_app_server'},
              'memory': {'memory_enabled': False, 'user_profile_enabled': False}}
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: config)
    monkeypatch.setattr(server, '_resolve_runtime_with_fallback', REAL_RESOLVE)
    monkeypatch.setattr('run_agent.AIAgent', REAL_AGENT)
    monkeypatch.setenv('HERMES_IGNORE_RULES', '1')
    monkeypatch.setattr('run_agent.get_tool_definitions', lambda *a, **k: [])
    monkeypatch.setattr('agent.model_metadata.query_ollama_num_ctx', lambda *a, **k: None)
    monkeypatch.setattr('agent.agent_init.query_ollama_num_ctx', lambda *a, **k: None)
    monkeypatch.setattr('hermes_cli.model_normalize.normalize_model_for_provider', lambda model, provider: model)
    def forbidden(*a, **k):
        pytest.fail('refresh attempted generic discovery or hard/resource teardown')
    monkeypatch.setattr(rp, 'resolve_provider', forbidden)
    monkeypatch.setattr(rp, 'load_pool', forbidden)
    monkeypatch.setattr(rp, 'resolve_codex_runtime_credentials', forbidden)
    monkeypatch.setattr(REAL_AGENT, '_get_transport', forbidden)
    monkeypatch.setattr(REAL_AGENT, '_create_openai_client', forbidden)
    env.old.model = 'offline-model'
    env.old.provider = provider
    env.old.api_mode = 'codex_app_server'
    env.old.api_key = env.old.base_url = ''
    env.old.reasoning_config = None
    env.old.service_tier = ''
    server._sync_bot_capabilities(SID, env.session)
    predecessor = env.session['agent']
    assert isinstance(predecessor, REAL_AGENT)
    native, client = native_session()
    predecessor._codex_session = native
    tool_state = ['shared-tool-state']
    predecessor._session_messages = tool_state
    monkeypatch.setattr('tools.bot_mode_probe.capability_fingerprint', lambda home: 'next-caps')
    with monkeypatch.context() as guard:
        guard.setattr(REAL_AGENT, 'close', forbidden)
        guard.setattr(env.target, 'close', forbidden)
        guard.setattr(env.target, 'end_session', forbidden)
        guard.setattr(env.launch, 'close', forbidden)
        yield SimpleNamespace(env=env, predecessor=predecessor, native=native,
                              client=client, monkeypatch=monkeypatch, tool_state=tool_state,
                              real_make=server._make_agent)


def assert_store_and_tools(owner):
    agent = owner.env.session['agent']
    assert agent._session_db is owner.env.target
    assert agent.session_id == KEY
    assert agent._owns_session_db
    assert owner.env.target.get_session(KEY)['ended_at'] is None
    assert owner.env.launch.get_session(KEY) is None
    assert owner.predecessor._session_messages is owner.tool_state


def assert_not_retired(owner):
    assert owner.env.session['agent'] is owner.predecessor
    assert owner.predecessor._codex_session is owner.native
    assert not owner.native._closed
    assert owner.client.closes == 0
    assert owner.env.session['bot_caps_seen'] == 'new-caps'
    assert '_bot_native_retirement' not in owner.env.session
    assert_store_and_tools(owner)


def wrap_constructor(owner, action):
    def construct(*a, **kw):
        new = owner.real_make(*a, **kw)
        action(new)
        return new
    owner.monkeypatch.setattr(server, '_make_agent', construct)


def test_owned_native_transport_closes_once_without_session_teardown(native_env):
    owner = native_env
    server._sync_bot_capabilities(SID, owner.env.session)
    successor = owner.env.session['agent']
    assert successor is not owner.predecessor
    assert owner.predecessor._codex_session is None
    assert owner.native._closed and owner.native._client is None
    assert owner.native._thread_id is None
    assert owner.client.closes == 1
    assert not owner.predecessor._owns_session_db
    assert '_bot_native_retirement' not in owner.env.session
    assert_store_and_tools(owner)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert server._finish_bot_native_retirement(owner.env.session)
    assert owner.client.closes == 1


def test_replacement_exclusive_transport_is_untouched(native_env):
    owner = native_env
    replacement_native, replacement_client = native_session()
    wrap_constructor(owner, lambda new: setattr(new, '_codex_session', replacement_native))
    server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.env.session['agent']._codex_session is replacement_native
    assert owner.client.closes == 1
    assert replacement_client.closes == 0
    assert not replacement_native._closed
    assert not replacement_native._interrupt_event.is_set()
    assert_store_and_tools(owner)


@pytest.mark.parametrize('sharing', ['session', 'transport'])
def test_shared_replacement_handle_refuses_publication(native_env, sharing):
    owner = native_env
    borrowed = owner.native if sharing == 'session' else native_session(owner.client)[0]
    replacement = []
    def share(new):
        new._codex_session = borrowed
        replacement.append(new)
    wrap_constructor(owner, share)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert_not_retired(owner)
    assert replacement[0]._codex_session is borrowed
    assert not borrowed._closed
    assert not replacement[0]._owns_session_db


@pytest.mark.parametrize('sharing', ['session', 'transport'])
def test_other_registered_owner_prevents_exclusive_retirement(native_env, sharing):
    owner = native_env
    borrowed = owner.native if sharing == 'session' else native_session(owner.client)[0]
    server._sessions['other'] = {'agent': SimpleNamespace(_codex_session=borrowed)}
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_SHARED'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert_not_retired(owner)


@pytest.mark.parametrize('timing', ['before', 'during'])
def test_active_native_turn_is_not_retired(native_env, timing):
    owner = native_env
    def active(new=None):owner.native._active_turn_id = 'active-turn'
    if timing == 'before':active()
    else:wrap_constructor(owner, active)
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_BUSY'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert_not_retired(owner)
    owner.native._active_turn_id = None


def test_busy_native_lock_is_refused_without_wait(native_env):
    owner = native_env
    owner.native._active_turn_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_BUSY'):
            server._sync_bot_capabilities(SID, owner.env.session)
    finally:
        owner.native._active_turn_lock.release()
    assert_not_retired(owner)


@pytest.mark.parametrize('change', ['native', 'client'])
def test_changed_predecessor_identity_cannot_retire_captured_resource(native_env, change):
    owner = native_env
    replacement_native, replacement_client = native_session()
    def swap(new):
        if change == 'native':owner.predecessor._codex_session = replacement_native
        else:owner.native._client = replacement_client
    wrap_constructor(owner, swap)
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_OWNER_CHANGED'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.env.session['agent'] is owner.predecessor
    assert owner.client.closes == replacement_client.closes == 0
    assert not owner.native._closed and not replacement_native._closed
    assert_store_and_tools(owner)


@pytest.mark.parametrize('failure', ['constructor', 'runtime', 'transfer'])
def test_failed_construction_or_validation_keeps_predecessor_native(native_env, failure):
    owner = native_env
    if failure == 'constructor':
        def fail(*a, **kw):raise RuntimeError('offline constructor failure')
        owner.monkeypatch.setattr(server, '_make_agent', fail)
    elif failure == 'runtime':wrap_constructor(owner, lambda new: setattr(new, 'model', 'wrong-model'))
    else:owner.monkeypatch.setattr(server, '_transfer_db_to_agent', lambda *a: False)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert_not_retired(owner)


@pytest.mark.parametrize('change', ['cancel', 'profile', 'registration'])
def test_stale_refresh_never_closes_native_resource(native_env, change):
    owner = native_env
    def stale(new):
        if change == 'cancel':owner.env.session['_turn_cancel_requested'] = True
        elif change == 'profile':owner.env.session['profile_home'] = '/wrong-profile'
        else:server._sessions[SID] = {'agent': new}
    wrap_constructor(owner, stale)
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_OWNER_CHANGED'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.predecessor._codex_session is owner.native
    assert owner.client.closes == 0 and not owner.native._closed
    assert owner.predecessor._session_db is owner.env.target
    assert owner.predecessor._owns_session_db


def test_close_failure_has_bounded_owner_and_normal_teardown_cleanup(native_env, caplog):
    owner = native_env
    attempts = []
    real_close = owner.native.close
    def fail():
        attempts.append('attempt')
        raise RuntimeError('offline native close failure')
    owner.monkeypatch.setattr(owner.native, 'close', fail)
    server._sync_bot_capabilities(SID, owner.env.session)
    successor = owner.env.session['agent']
    assert successor is not owner.predecessor
    assert owner.predecessor._codex_session is None
    pending = owner.env.session['_bot_native_retirement']
    assert pending['native'] is owner.native and pending['agent'] is owner.predecessor
    assert pending['status'] == 'failed'
    assert 'native retirement failed' in caplog.text
    assert attempts == ['attempt'] and owner.client.closes == 0
    assert_store_and_tools(owner)
    owner.monkeypatch.setattr('tools.bot_mode_probe.capability_fingerprint', lambda home: 'later-caps')
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_RETIREMENT_PENDING'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert attempts == ['attempt'] and owner.env.session['_bot_native_retirement'] is pending
    # Exercise the existing teardown owner with its unrelated destructive
    # operations inert; only real pending-native cleanup is under test.
    owner.monkeypatch.setattr(owner.native, 'close', real_close)
    owner.monkeypatch.setattr(server, '_finalize_session', lambda *a, **k: None)
    owner.monkeypatch.setattr(server, '_announce_session_reclaimed', lambda *a: None)
    normal_closes = []
    owner.monkeypatch.setattr(successor, 'close', lambda: normal_closes.append('normal-boundary'))
    server._teardown_session(owner.env.session)
    assert owner.client.closes == 1 and owner.native._closed
    assert '_bot_native_retirement' not in owner.env.session
    assert normal_closes == ['normal-boundary']
    assert_store_and_tools(owner)
    server._teardown_session(owner.env.session)
    assert owner.client.closes == 1


def test_pending_handle_shared_with_replacement_is_never_closed(native_env):
    owner = native_env
    def fail():raise RuntimeError('offline native close failure')
    owner.monkeypatch.setattr(owner.native, 'close', fail)
    server._sync_bot_capabilities(SID, owner.env.session)
    successor = owner.env.session['agent']
    successor._codex_session = owner.native
    assert not server._finish_bot_native_retirement(owner.env.session)
    assert owner.client.closes == 0
    assert successor._codex_session is owner.native and not owner.native._closed
    assert_store_and_tools(owner)


def test_real_session_close_does_not_hide_transport_failure(native_env, caplog):
    owner = native_env
    attempts = []
    def fail():
        attempts.append('transport-close')
        raise RuntimeError('offline transport close failure')
    owner.monkeypatch.setattr(owner.client, 'close', fail)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.env.session['agent'] is not owner.predecessor
    assert owner.predecessor._codex_session is None
    assert owner.native._closed and owner.native._client is None
    assert owner.client.is_alive()
    pending = owner.env.session['_bot_native_retirement']
    assert pending['native'] is owner.native and pending['client'] is owner.client
    assert pending['status'] == 'unresolved'
    assert 'native retirement unresolved' in caplog.text
    assert attempts == ['transport-close']
    assert_store_and_tools(owner)


@pytest.mark.parametrize('state', ['alive', 'unknown'])
def test_best_effort_transport_return_requires_exit_evidence(native_env, state):
    owner = native_env
    calls = []
    def attempt():calls.append('attempt')
    owner.monkeypatch.setattr(owner.client, 'close', attempt)
    if state == 'unknown':
        def unknown():raise RuntimeError('offline liveness unknown')
        owner.monkeypatch.setattr(owner.client, 'is_alive', unknown)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.native._closed and owner.native._client is None
    pending = owner.env.session['_bot_native_retirement']
    assert pending['client'] is owner.client and pending['status'] == 'unresolved'
    assert calls == ['attempt']
    owner.monkeypatch.setattr('tools.bot_mode_probe.capability_fingerprint', lambda home: 'later-caps')
    with pytest.raises(RuntimeError, match='BOT_CAPABILITY_NATIVE_RETIREMENT_PENDING'):
        server._sync_bot_capabilities(SID, owner.env.session)
    assert calls == ['attempt']
    assert_store_and_tools(owner)


def test_detached_pending_client_cannot_close_replacement_shared_transport(native_env):
    owner = native_env
    owner.monkeypatch.setattr(owner.client, 'close', lambda: None)
    server._sync_bot_capabilities(SID, owner.env.session)
    pending = owner.env.session['_bot_native_retirement']
    borrowed, _ = native_session(owner.client)
    owner.env.session['agent']._codex_session = borrowed
    attempts = []
    owner.monkeypatch.setattr(owner.client, 'close', lambda: attempts.append('close'))
    assert not server._finish_bot_native_retirement(owner.env.session)
    assert attempts == [] and owner.env.session['_bot_native_retirement'] is pending
    assert owner.env.session['agent']._codex_session is borrowed and not borrowed._closed
    assert_store_and_tools(owner)


def test_captured_client_retry_can_prove_exit_without_reclosing_native(native_env):
    owner = native_env
    owner.monkeypatch.setattr(owner.client, 'close', lambda: None)
    server._sync_bot_capabilities(SID, owner.env.session)
    assert owner.native._closed and owner.native._client is None
    def native_again():pytest.fail('real native close would no-op and lose the captured transport')
    owner.monkeypatch.setattr(owner.native, 'close', native_again)
    owner.monkeypatch.setattr(owner.client, 'close', lambda: setattr(owner.client, 'alive', False))
    assert server._finish_bot_native_retirement(owner.env.session)
    assert '_bot_native_retirement' not in owner.env.session
    assert not owner.client.is_alive()
    assert_store_and_tools(owner)


def test_unresolved_exit_after_normal_teardown_retains_explicit_handle(native_env, caplog):
    owner = native_env
    calls = []
    owner.monkeypatch.setattr(owner.client, 'close', lambda: calls.append('attempt'))
    server._sync_bot_capabilities(SID, owner.env.session)
    pending = owner.env.session['_bot_native_retirement']
    owner.monkeypatch.setattr(server, '_finalize_session', lambda *a, **k: None)
    owner.monkeypatch.setattr(server, '_announce_session_reclaimed', lambda *a: None)
    owner.monkeypatch.setattr(owner.env.session['agent'], 'close', lambda: None)
    server._teardown_session(owner.env.session)
    assert owner.env.session['_bot_native_retirement'] is pending
    assert pending['client'] is owner.client and pending['native'] is owner.native
    assert pending['status'] == 'unresolved' and owner.client.is_alive()
    assert calls == ['attempt', 'attempt']
    assert 'native retirement unresolved' in caplog.text
    assert_store_and_tools(owner)


@pytest.mark.parametrize('child_exits', [True, False])
def test_real_client_close_verifies_child_exit_and_keeps_failed_exit(native_env, child_exits):
    import threading
    from agent.transports.codex_app_server import CodexAppServerClient
    owner = native_env
    class OfflineProcess:
        stdin = None
        def __init__(self):self.alive, self.stops = True, []
        def terminate(self):
            self.stops.append('terminate')
            if not child_exits:raise OSError('offline termination refused')
            self.alive = False
        def kill(self):
            self.stops.append('kill')
            if not child_exits:raise OSError('offline kill refused')
            self.alive = False
        def wait(self, timeout=None):
            if self.alive:raise OSError('offline wait unresolved')
            return 0
        def poll(self):return None if self.alive else 0
    # Constructor would spawn a process. Only real close/is_alive are exercised
    # here against an inert process object; native session/AIAgent stay real.
    client = object.__new__(CodexAppServerClient)
    client._pending_lock = threading.Lock()
    client._write_lock = threading.Lock()
    client._pending = {}
    client._closed = False
    client._proc = OfflineProcess()
    owner.client = client
    owner.native._client = client
    server._sync_bot_capabilities(SID, owner.env.session)
    assert client._closed and owner.native._closed and owner.native._client is None
    assert_store_and_tools(owner)
    if child_exits:
        assert not client.is_alive()
        assert '_bot_native_retirement' not in owner.env.session
        assert client._proc.stops == ['terminate']
    else:
        pending = owner.env.session['_bot_native_retirement']
        assert pending['client'] is client and pending['status'] == 'unresolved'
        assert client.is_alive() and client._proc.stops == ['terminate', 'kill']
        assert not server._finish_bot_native_retirement(owner.env.session)
        assert owner.env.session['_bot_native_retirement'] is pending
        assert client._proc.stops == ['terminate', 'kill']
