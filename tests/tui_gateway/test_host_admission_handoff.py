"""Host admission belongs to an exact request, not a transient running flag."""
import io
import copy
import json
import threading
import types

from tui_gateway import server

import pytest

from tui_gateway.compute_host import ComputeHost


def frames(output):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def test_second_request_refused_while_first_worker_owns_session(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    entered = threading.Event()
    release = threading.Event()
    starts = []

    def run(frame):
        starts.append(frame["request_id"])
        entered.set()
        release.wait(3)

    monkeypatch.setattr(host, "_run_real_turn", run)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert entered.wait(2)
        host._handle_turn_start({"sid": "s", "request_id": "B"})
        assert starts == ["A"]
        refused = [f for f in frames(output) if f.get("request_id") == "B"]
        assert len(refused) == 1
        assert refused[0]["reason"] == "admission_refused_busy"
    finally:
        release.set()
        host.close()


def test_terminal_allows_successor_before_prior_worker_retires(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    a_terminal = threading.Event()
    release_a = threading.Event()
    b_started = threading.Event()
    release_b = threading.Event()

    def run(frame):
        if frame["request_id"] == "A":
            host._emit_real_turn_terminal({"type": "turn.end", **frame})
            a_terminal.set()
            release_a.wait(3)
        else:
            b_started.set()
            release_b.wait(3)
            host._emit_real_turn_terminal({"type": "turn.end", **frame})

    monkeypatch.setattr(host, "_run_real_turn", run)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert a_terminal.wait(2)
        with host._turn_futures_lock:
            a_future = next(iter(host._turn_futures))
        host._handle_turn_start({"sid": "s", "request_id": "B"})
        assert b_started.wait(2)
        release_a.set()
        a_future.result(timeout=2)
        assert host._active_request_ids["s"] == "B"
        host._handle_turn_start({"sid": "s", "request_id": "C"})
        assert any(f.get("request_id") == "C" and f.get("reason") == "admission_refused_busy"
                   for f in frames(output))
    finally:
        release_a.set()
        release_b.set()
        host.close()


def test_terminal_output_does_not_hold_admission_lock(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    entered = threading.Event()
    release = threading.Event()
    def blocked_emit(frame):
        entered.set()
        release.wait(3)
    monkeypatch.setattr(host, "emit", blocked_emit)
    host._active_request_ids["s"] = "A"
    worker = threading.Thread(target=host._emit_real_turn_terminal,
                              args=({"type": "turn.end", "sid": "s", "request_id": "A"},))
    worker.start()
    try:
        assert entered.wait(2)
        acquired = host._turn_futures_lock.acquire(timeout=0.5)
        assert acquired, "terminal I/O must not monopolize host admission bookkeeping"
        if acquired:
            host._turn_futures_lock.release()
    finally:
        release.set()
        worker.join(3)
        host.close()
    assert not worker.is_alive()


def test_executor_refusal_does_not_leave_phantom_owner(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    def fail(*args, **kwargs):
        raise RuntimeError("executor unavailable")
    monkeypatch.setattr(host._executor, "submit", fail)
    try:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert not host._active_request_ids
    finally:
        host.close()


# The actual serving admission and actual child driver use separate registries.
from tests.tui_gateway.test_model_once_turn_runtime_owner import live_turn  # noqa: E402,F401
from tests.tui_gateway.test_model_once_turn_runtime_owner import routes, gateway, turn_env, agent  # noqa: E402,F401
from tests.tui_gateway.test_model_once_turn_runtime_owner import _REAL_THREAD, REAL_COMPRESS_SYNC  # noqa: E402

REAL_EMIT = server._emit
REAL_WRITE_JSON = server.write_json


@pytest.mark.parametrize('parent_generation,reanchored_child', [(1, False), (0, True), (0, False)])
def test_serving_queue_executes_once_across_independent_child_generation(live_turn, monkeypatch, parent_generation, reanchored_child):
    parent, _ = live_turn
    predecessor = parent['agent']
    agent = copy.copy(predecessor)  # distinct inert constructor result
    assert agent is not predecessor
    parent.update(running=False, _queued_prompt_generation=parent_generation,
                  queued_prompt={'text': 'one queued input'})
    parent_registry = server._sessions
    child_registry = {}
    runs, terminals = [], []
    parent_runs = []
    monkeypatch.setattr(predecessor, 'run_conversation', lambda *a, **kw: parent_runs.append(a))
    monkeypatch.setattr(agent, 'run_conversation', lambda *a, **kw: (
        runs.append(a[0]), {'final_response': 'offline', 'completed': False, 'api_calls': 0})[1])
    monkeypatch.setattr(server, '_make_agent', lambda *a, **kw: agent)
    monkeypatch.setattr(server, '_start_notification_poller', lambda *a: None)
    monkeypatch.setattr(server, '_schedule_mcp_late_refresh', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    monkeypatch.setattr(server, '_persist_branch_seed', lambda *a: None)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(host, 'emit', lambda frame: terminals.append(frame) if frame['type'] in {'turn.end', 'turn.error'} else None)
    supervisor = types.SimpleNamespace(boot_id=host._boot_id)
    monkeypatch.setattr(server, '_compute_host_supervisor', supervisor)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: supervisor)
    child = None

    def submit(frame, on_complete):
        nonlocal child
        frame['_admitted_host_boot_id'] = host._boot_id
        monkeypatch.setattr(server, '_sessions', child_registry)
        try:
            child = host._ensure_server_session(server, frame)
            assert int(child.get('_queued_prompt_generation', 0)) == 0
            if reanchored_child:
                agent.session_id = 'child-continuation'
                monkeypatch.setattr(server, '_transfer_active_session_slot', lambda *a, **kw: True)
                REAL_COMPRESS_SYNC('selected', child, restart_slash_worker=False)
                assert child['_queued_prompt_generation'] == 1
            host._active_request_ids['selected'] = frame['request_id']
            host._run_real_turn(frame)
        finally:
            monkeypatch.setattr(server, '_sessions', parent_registry)
        assert len(terminals) == 1, terminals
        on_complete({**terminals[0], '_host_boot_id': host._boot_id})

    supervisor.submit_turn = submit
    try:
        assert server._drain_queued_prompt('queued-rpc', 'selected', parent)
        assert runs == ['one queued input'], terminals
        assert not parent_runs
        assert parent.get('queued_prompt') is None
        assert not parent.get('_compute_host_active_request_id')
        assert parent['running'] is False
        assert child['running'] is False
    finally:
        host.close()


def test_parent_host_admission_rejects_stale_stop_cut_before_physical_send(live_turn, monkeypatch):
    session, _ = live_turn
    session.update(_queued_prompt_generation=1, _last_stop_queue_generation=1, _turn_cancel_requested=True)
    sent = []
    supervisor = types.SimpleNamespace(submit_turn=lambda *a, **kw: sent.append(True))
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: supervisor)
    result = server._submit_prompt_to_compute_host('stale', 'selected', session, 'pre-cut', queued_prompt_generation=0)
    assert result['error']['data']['delivery'] == 'stale_claim'
    assert not sent
    assert not session.get('_compute_host_active_request_id')


@pytest.mark.parametrize('boundary', ['stop', 'reanchor', 'successor'])
def test_queue_cut_between_snapshot_and_host_claim_cannot_dispatch_or_clear_successor(live_turn, monkeypatch, boundary):
    session, _ = live_turn
    queued = {'text': 'claimed pre-boundary input'}
    session.update(running=False, queued_prompt=queued)
    sent = []
    supervisor = types.SimpleNamespace(submit_turn=lambda *a, **kw: sent.append(True), boot_id='inert-boot')
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    def boundary_before_claim(*a):
        with session['history_lock']:
            session['_queued_prompt_generation'] = 1
            if boundary == 'stop':
                session['_last_stop_queue_generation'] = 1
                session['_turn_cancel_requested'] = True
            elif boundary == 'successor':
                server._start_inflight_turn(session, 'successor')
                server._begin_turn_outcome(session, 'selected', 'B', 'inline')
                session['running'] = True
        return supervisor
    monkeypatch.setattr(server, '_get_compute_host_supervisor', boundary_before_claim)
    assert server._drain_queued_prompt('cut-rpc', 'selected', session)
    if boundary == 'successor':
        assert not sent
        assert session['running'] and session['inflight_turn']['user'] == 'successor'
        assert session.get('queued_prompt') is None
    elif boundary == 'reanchor':
        assert sent == [True] and session['running']
        assert session.get('queued_prompt') is None and session.get('_compute_host_active_request_id')
    else:
        assert not sent
        assert not session['running'] and session.get('queued_prompt') is None


@pytest.mark.parametrize('failure', ['uncertain', 'not_sent'])
def test_queued_host_send_failure_preserves_delivery_custody(live_turn, monkeypatch, failure):
    from tui_gateway.host_supervisor import HostSendNotSent
    session, _ = live_turn
    session.update(running=False, queued_prompt={'text': 'queued'})
    sent = []
    def send(frame, **kw):
        sent.append(frame['request_id'])
        if failure == 'not_sent': raise HostSendNotSent('inert prewrite refusal')
        raise OSError('inert uncertain write')
    supervisor = types.SimpleNamespace(submit_turn=send, boot_id='inert-boot')
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: supervisor)
    assert server._drain_queued_prompt('queued-rpc', 'selected', session)
    assert len(sent) == 1
    if failure == 'uncertain':
        assert session['running'] and session['_host_delivery_uncertain']
        assert session['_compute_host_active_request_id'] == sent[0]
    else:
        assert not session['running'] and not session.get('_compute_host_active_request_id')


def test_old_idle_snapshot_cannot_follow_successor_start(live_turn, monkeypatch):
    session, _ = live_turn
    output = []
    transport = types.SimpleNamespace(write=output.append)
    session['transport'] = transport
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    monkeypatch.setattr(server, 'write_json', output.append)
    with session['history_lock']:
        first = server._begin_turn_outcome(session, 'selected', 'A', 'inline')
        session['running'] = False
        idle = {'running': False}
        second = server._begin_turn_outcome(session, 'selected', 'B', 'inline')
        session['running'] = True
    server._emit('message.start', 'selected', {'text': 'B'})
    token = server._turn_outcome_execution.set((session, 'selected', first['request_id']))
    try:
        server._emit('session.info', 'selected', idle)
    finally:
        server._turn_outcome_execution.reset(token)
    assert [frame['params']['type'] for frame in output] == ['message.start']
    with session['history_lock']:
        session['running'] = False
    token = server._turn_outcome_execution.set((session, 'selected', second['request_id']))
    try:
        server._emit('session.info', 'selected', {'running': False})
    finally:
        server._turn_outcome_execution.reset(token)
    assert output[-1]['params']['payload']['running'] is False


@pytest.mark.parametrize('payload', [None, {'model': 'legacy'}, {'running': False}])
def test_uncorrelated_idle_legacy_projection_remains_visible(live_turn, monkeypatch, payload):
    session, _ = live_turn
    session['running'] = False
    output = []
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    monkeypatch.setattr(server, 'write_json', output.append)
    server._emit('session.info', 'selected', payload)
    assert len(output) == 1
    assert output[0]['params'].get('payload') == payload


def test_lifecycle_transport_write_does_not_hold_history_lock(live_turn, monkeypatch):
    session, _ = live_turn
    monkeypatch.setattr(server.threading, 'Thread', _REAL_THREAD)
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    entered, release, successor_started = threading.Event(), threading.Event(), threading.Event()
    output = []
    with session['history_lock']:
        first = server._begin_turn_outcome(session, 'selected', 'A', 'inline')
        session['running'] = False
    def write(frame):
        if frame['params']['type'] == 'session.info':
            entered.set()
            assert release.wait(2)
        output.append(frame)
    session['transport'] = types.SimpleNamespace(write=write)
    monkeypatch.setattr(server, 'write_json', write)
    def old():
        token = server._turn_outcome_execution.set((session, 'selected', first['request_id']))
        try: server._emit('session.info', 'selected', {'running': False})
        finally: server._turn_outcome_execution.reset(token)
    def successor():
        with session['history_lock']:
            server._begin_turn_outcome(session, 'selected', 'B', 'inline')
            session['running'] = True
        successor_started.set()
        server._emit('message.start', 'selected', {'text': 'B'})
    a = _REAL_THREAD(target=old)
    b = _REAL_THREAD(target=successor)
    a.start()
    try:
        assert entered.wait(2)
        b.start()
        assert successor_started.wait(1), 'history lock held through output'
    finally:
        release.set()
        a.join(3)
        if b.ident is not None: b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert [frame['params']['type'] for frame in output] == ['session.info', 'message.start']


def _real_transport(session, monkeypatch, callback=None):
    output = []
    def write(frame):
        acquired = session['history_lock'].acquire(timeout=0.5)
        assert acquired, 'transport entered while history lock is held'
        session['history_lock'].release()
        output.append(frame)
        if callback: callback(frame)
    transport = types.SimpleNamespace(write=write)
    session['transport'] = transport
    monkeypatch.setattr(server, '_emit', REAL_EMIT)
    monkeypatch.setattr(server, 'write_json', write)
    return output


def test_registered_prompt_initialization_failure_releases_real_lock_and_accepts_successor(live_turn, monkeypatch):
    session, _ = live_turn
    original = session['agent']
    session.update(agent=None, running=False, agent_ready=threading.Event(), agent_error=None)
    monkeypatch.setattr(server.threading, 'Thread', _REAL_THREAD)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    monkeypatch.setattr(server, '_persist_branch_seed', lambda *a: None)
    monkeypatch.setattr(server, '_start_notification_poller', lambda *a: None)
    monkeypatch.setattr(server, '_schedule_mcp_late_refresh', lambda *a: None)
    monkeypatch.setattr(server, '_accept_tui_context_input', lambda *a, **kw: None)
    def fail(*a, **kw): raise RuntimeError('inert constructor failure')
    monkeypatch.setattr(server, '_make_agent', fail)
    output = _real_transport(session, monkeypatch)
    result = server._methods['prompt.submit']('init-A', {'session_id': 'selected', 'text': 'first'})
    assert not result.get('error'), result
    worker = session['_run_thread']
    worker.join(2)
    try:
        assert not worker.is_alive(), 'registered initialization failure self-deadlocked'
        assert not session['running']
        assert any(f['params']['type'] == 'session.info' and f['params']['payload']['running'] is False for f in output)
        assert any(f['params']['type'] == 'message.complete' and f['params']['payload']['error_surface']['code'] == 'agent_init_failed' for f in output)
        acquired = session['history_lock'].acquire(timeout=0.2)
        assert acquired
        session['history_lock'].release()
        session.update(agent=original, agent_error=None)
        original.session_id = session['session_key']
        ran = []
        monkeypatch.setattr(original, 'run_conversation', lambda *a, **kw: (ran.append(a[0]), {'final_response': 'ok', 'completed': False, 'api_calls': 0})[1])
        result = server._methods['prompt.submit']('init-B', {'session_id': 'selected', 'text': 'later'})
        assert not result.get('error'), result
        # The real driver publishes/starts its replacement worker under this
        # registry lock. Capture it with the same lock before joining it.
        with server._sessions_lock:
            later_worker = session['_run_thread']
        later_worker.join(2)
        assert not later_worker.is_alive() and ran == ['later'] and not session['running']
    finally:
        if worker.is_alive():
            # Rejected-source containment only: detach and unblock its poisoned
            # fixture AFTER the bounded deadlock assertion. This never counts
            # as successful settlement and is never used on the green path.
            server._sessions.pop('selected', None)
            if session['history_lock'].locked(): session['history_lock'].release()
            worker.join(2)


@pytest.mark.parametrize('interleave', ['none', 'successor'])
def test_real_failed_history_settlement_has_no_locked_transport_or_stale_cleanup(live_turn, monkeypatch, interleave):
    session, _ = live_turn
    ready = threading.Event(); ready.set()
    session.update(resume_history_ready=ready, resume_history_error='inert history failure', agent_error='inert history failure')
    with session['history_lock']:
        server._start_inflight_turn(session, 'failed history')
        started = session['inflight_turn']['started_at']
        ref = server._begin_turn_outcome(session, 'selected', 'history-A', 'inline')
    def callback(frame):
        if interleave == 'successor' and frame['params']['type'] == 'message.complete':
            with session['history_lock']:
                server._start_inflight_turn(session, 'B')
                server._begin_turn_outcome(session, 'selected', 'history-B', 'inline')
                session['running'] = True
    output = _real_transport(session, monkeypatch, callback)
    errors = []
    def settle():
        token = server._turn_outcome_execution.set((session, 'selected', ref['request_id']))
        try:
            server._emit_terminal_turn_error('selected', session, 'inert history failure',
                error_surface={'layer': 'runtime', 'code': 'agent_init_failed', 'retryable': True},
                expected_started_at=started, expected_queue_generation=0, settle_running=True,
                terminal_transport=session['transport'])
        except Exception as exc: errors.append(exc)
        finally: server._turn_outcome_execution.reset(token)
    worker = _REAL_THREAD(target=settle, daemon=True); worker.start(); worker.join(1)
    try:
        assert not worker.is_alive(), 'real history error settlement deadlocked'
        assert not errors
        if interleave == 'successor':
            assert session['running'] and session['inflight_turn']['user'] == 'B'
            assert not any(f['params']['type'] == 'session.info' for f in output)
        else:
            assert not session['running']
            assert output[-1]['params']['type'] == 'session.info'
    finally:
        if worker.is_alive():
            server._sessions.pop('selected', None)
            if session['history_lock'].locked(): session['history_lock'].release()
            worker.join(2)


def _finished_predecessor(session):
    with session['history_lock']:
        ref = server._begin_turn_outcome(session, 'selected', 'prior-A', 'inline')
        session['_turn_outcomes'].finish(ref['request_id'])
        session['running'] = False


def test_real_model_switch_publishes_nonce_less_owner_after_finished_turn(live_turn, monkeypatch):
    session, _ = live_turn
    _finished_predecessor(session)
    output = _real_transport(session, monkeypatch)
    result = server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --session',
        confirm_expensive_model=True, persist_override=False)
    assert result['scope'] == 'session'
    infos = [f['params']['payload'] for f in output if f['params']['type'] == 'session.info']
    assert infos and (infos[-1]['model'], infos[-1]['provider']) == ('chosen-model', 'endpoint-b')


@pytest.mark.parametrize('supersede', ['agent', 'generation', 'nonce', 'runtime', 'inflight', 'thread', 'history', 'closing', 'finalized'])
def test_model_snapshot_cannot_publish_after_nonce_less_owner_superseded(live_turn, monkeypatch, supersede):
    session, _ = live_turn
    _finished_predecessor(session)
    output = _real_transport(session, monkeypatch)
    def before_emit(event, sid, payload=None):
        if event == 'session.info':
            with session['history_lock']:
                if supersede == 'agent': session['agent'] = types.SimpleNamespace()
                elif supersede == 'generation': session['_queued_prompt_generation'] = 1
                elif supersede == 'runtime': session['agent']._primary_runtime = {'model': 'successor'}
                elif supersede == 'inflight': server._start_inflight_turn(session, 'B')
                elif supersede == 'thread': session['_run_thread'] = object()
                elif supersede == 'history': session['history_version'] += 1
                elif supersede == 'closing': session['_closing'] = True
                elif supersede == 'finalized': session['_finalized'] = True
                else:
                    server._begin_turn_outcome(session, 'selected', 'successor-B', 'inline')
                    session['_turn_outcomes'].finish('successor-B')
        REAL_EMIT(event, sid, payload)
    monkeypatch.setattr(server, '_emit', before_emit)
    server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --session',
        confirm_expensive_model=True, persist_override=False)
    assert not any(f['params']['type'] == 'session.info' for f in output)


def test_real_queued_completion_invalidates_prior_model_snapshot_without_new_nonce(live_turn, monkeypatch):
    session, _ = live_turn
    _finished_predecessor(session)
    output = _real_transport(session, monkeypatch)
    ran = []
    def offline(text, *, persist_user_event_id=None, **kw):
        ran.append((text, persist_user_event_id))
        return {'final_response': 'B', 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(session['agent'], 'run_conversation', offline)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    released = []
    def before_emit(event, sid, payload=None):
        if event == 'session.info' and not released:
            released.append(True)
            session['queued_prompt'] = {'text': 'queued B', 'context_input_event_id': 'existing-B'}
            assert server._drain_queued_prompt('queued-B', 'selected', session)
            assert not session['running']
        REAL_EMIT(event, sid, payload)
    monkeypatch.setattr(server, '_emit', before_emit)
    server._apply_model_switch('selected', session, 'chosen-model --provider endpoint-b --session',
        confirm_expensive_model=True, persist_override=False)
    assert ran == [('queued B', 'existing-B')]
    assert session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == 'prior-A'
    infos = [f for f in output if f['params']['type'] == 'session.info']
    assert len(infos) == 1 and infos[0]['params']['payload']['running'] is False


@pytest.mark.parametrize('payload', [None, {'model': 'legacy'}, {'running': False}])
def test_captured_none_nonce_remains_distinct_from_finished_turn(live_turn, monkeypatch, payload):
    session, _ = live_turn
    _finished_predecessor(session)
    output = _real_transport(session, monkeypatch)
    token = server._turn_outcome_execution.set((session, 'selected', None))
    try: server._emit('session.info', 'selected', payload)
    finally: server._turn_outcome_execution.reset(token)
    assert len(output) == 1 and output[0]['params'].get('payload') == payload


def test_queued_inline_successor_publishes_its_own_idle_without_borrowing_predecessor(live_turn, monkeypatch):
    session, _ = live_turn
    _finished_predecessor(session)
    output = _real_transport(session, monkeypatch)
    session['queued_prompt'] = {'text': 'queued B', 'context_input_event_id': 'existing-B'}
    ran = []
    def offline(text, *, persist_user_event_id=None, **kw):
        ran.append((text, persist_user_event_id))
        return {'final_response': 'B', 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(session['agent'], 'run_conversation', offline)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    assert server._drain_queued_prompt('rpc-B', 'selected', session)
    assert ran == [('queued B', 'existing-B')] and not session['running']
    assert any(f['params']['type'] == 'session.info' and f['params']['payload']['running'] is False for f in output)
    assert session['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == 'prior-A'


class _AfterRequeueLock:
    """Deterministic interleave after releasing a real non-reentrant lock."""
    def __init__(self, session, head, callback):
        self.lock = session['history_lock']
        self.session, self.head, self.callback = session, head, callback
        self.fired = False
    def acquire(self, *a, **kw): return self.lock.acquire(*a, **kw)
    def release(self): return self.lock.release()
    def locked(self): return self.lock.locked()
    def __enter__(self): self.lock.acquire(); return self
    def __exit__(self, *exc):
        self.lock.release()
        if (not self.fired and self.session.get('queued_prompt') is self.head
                and not self.session.get('running') and self.session.get('_queued_prompt_generation') == 1):
            self.fired = True
            self.callback()


@pytest.mark.parametrize('boundary', ['early', 'late'])
@pytest.mark.parametrize('reanchors', [1, 2])
def test_reanchor_fresh_drain_reaches_real_child_once_in_fifo_order(live_turn, monkeypatch, boundary, reanchors):
    parent, _ = live_turn
    parent.update(running=False, queued_prompt={'text': 'head', 'context_input_event_id': 'existing-head'},
                  queued_prompts=[{'text': 'tail', 'context_input_event_id': 'existing-tail'}])
    parent_registry = server._sessions
    child_registry = {}
    child_agent = copy.copy(parent['agent'])
    runs, parent_runs, frames_sent, terminals, accepts = [], [], [], [], []
    monkeypatch.setattr(parent['agent'], 'run_conversation', lambda *a, **kw: parent_runs.append(a))
    def offline(text, *, persist_user_event_id=None, **kw):
        runs.append((text, persist_user_event_id))
        return {'final_response': text, 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(child_agent, 'run_conversation', offline)
    def construct(*a, **kw):
        child_agent.session_id = kw['session_id']
        return child_agent
    monkeypatch.setattr(server, '_make_agent', construct)
    monkeypatch.setattr(server, '_start_notification_poller', lambda *a: None)
    monkeypatch.setattr(server, '_schedule_mcp_late_refresh', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    monkeypatch.setattr(server, '_persist_branch_seed', lambda *a: None)
    monkeypatch.setattr(server, '_accept_tui_context_input', lambda *a, **kw: accepts.append(True))
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_transfer_active_session_slot', lambda *a, **kw: True)
    monkeypatch.setattr(server, '_inside_compute_host_child', lambda: server._sessions is child_registry)
    remaining = [reanchors]
    def rotate():
        if remaining[0]:
            remaining[0] -= 1
            parent['agent'].session_id = 'rotation-' + str(reanchors - remaining[0])
            REAL_COMPRESS_SYNC('selected', parent, restart_slash_worker=False)
    def uses_host(*a):
        if boundary == 'early' and server._sessions is parent_registry: rotate()
        return True
    monkeypatch.setattr(server, '_session_uses_compute_host', uses_host)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(host, 'emit', lambda frame: terminals.append(frame) if frame['type'] in {'turn.end', 'turn.error'} else None)
    supervisor = types.SimpleNamespace(boot_id=host._boot_id)
    monkeypatch.setattr(server, '_compute_host_supervisor', supervisor)
    def supervisor_for(*a):
        if boundary == 'late': rotate()
        return supervisor
    monkeypatch.setattr(server, '_get_compute_host_supervisor', supervisor_for)
    _real_transport(parent, monkeypatch)
    def submit(frame, on_complete):
        frames_sent.append(frame.copy())
        frame['_admitted_host_boot_id'] = host._boot_id
        before = len(terminals)
        monkeypatch.setattr(server, '_sessions', child_registry)
        try:
            host._active_request_ids['selected'] = frame['request_id']
            host._run_real_turn(frame)
        finally: monkeypatch.setattr(server, '_sessions', parent_registry)
        assert len(terminals) == before + 1 and terminals[-1]['type'] == 'turn.end', terminals
        on_complete({**terminals[-1], '_host_boot_id': host._boot_id})
    supervisor.submit_turn = submit
    try:
        assert server._drain_queued_prompt('reanchor-rpc', 'selected', parent)
        assert remaining == [0] and runs == [('head', 'existing-head'), ('tail', 'existing-tail')]
        assert not parent_runs and not accepts
        assert [f['context_input_event_id'] for f in frames_sent] == ['existing-head', 'existing-tail']
        assert len({f['request_id'] for f in frames_sent}) == 2
        assert not parent['running'] and not child_registry['selected']['running']
        assert not parent.get('queued_prompt') and not parent.get('_compute_host_active_request_id')
    finally: host.close()


@pytest.mark.parametrize('interleave', ['stop', 'successor', 'uncertain'])
def test_fresh_queue_claim_cannot_borrow_stop_successor_or_uncertain_owner(live_turn, monkeypatch, interleave):
    session, _ = live_turn
    head = {'text': 'head', 'context_input_event_id': 'existing-head'}
    session.update(running=False, queued_prompt=head)
    use_host = [True]
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: use_host[0])
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    def after_requeue():
        if interleave == 'stop':
            use_host[0] = False
            try: server._interrupt_session_turn('selected', session, request_id='stop-after-requeue')
            finally: use_host[0] = True
        elif interleave == 'successor':
            with session['history_lock']:
                server._start_inflight_turn(session, 'successor-B')
                server._begin_turn_outcome(session, 'selected', 'B', 'inline')
                session['running'] = True
    lock = _AfterRequeueLock(session, head, after_requeue)
    session['history_lock'] = lock
    sent, cuts = [], [0]
    def send(frame, **kw):
        sent.append(frame['request_id'])
        raise OSError('inert uncertain fresh write')
    supervisor = types.SimpleNamespace(boot_id='inert-boot', submit_turn=send)
    def get_supervisor(*a):
        if cuts[0] == 0:
            cuts[0] += 1
            session['_queued_prompt_generation'] = 1
        return supervisor
    monkeypatch.setattr(server, '_get_compute_host_supervisor', get_supervisor)
    assert server._drain_queued_prompt('cut-rpc', 'selected', session)
    assert lock.fired
    if interleave == 'stop':
        assert not sent and not session.get('queued_prompt') and not session['running']
    elif interleave == 'successor':
        assert not sent and session['running'] and session['inflight_turn']['user'] == 'successor-B'
        assert session['queued_prompt'] is head
    else:
        assert len(sent) == 1 and session['_compute_host_active_request_id'] == sent[0]
        assert session['running'] and session['_host_delivery_uncertain']


def test_repeated_reanchor_exhaustion_has_one_owned_preprovider_failure(live_turn, monkeypatch):
    session, _ = live_turn
    session.update(running=False, queued_prompt={'text': 'head', 'context_input_event_id': 'existing-head'})
    output = _real_transport(session, monkeypatch)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    sent, cuts = [], []
    supervisor = types.SimpleNamespace(boot_id='inert-boot', submit_turn=lambda *a, **kw: sent.append(True))
    def get_supervisor(*a):
        cuts.append(True)
        session['_queued_prompt_generation'] = len(cuts)
        return supervisor
    monkeypatch.setattr(server, '_get_compute_host_supervisor', get_supervisor)
    assert server._drain_queued_prompt('churn-rpc', 'selected', session)
    assert len(cuts) == 4 and not sent
    errors = [f['params']['payload'] for f in output if f['params']['type'] == 'message.complete']
    assert len(errors) == 1 and errors[0]['error_surface']['code'] == 'queue_recovery_failed'
    assert errors[0]['execution_started'] is False and errors[0]['input_event_id'] == 'existing-head'
    assert errors[0]['durable_input_accepted'] is True and session.get('queued_prompt') is None
    assert not session['running'] and not session.get('_compute_host_active_request_id')


def test_terminal_publish_failure_cannot_clear_replacement_owner(monkeypatch):
    state = {"history_lock": threading.Lock(), "history": [], "session_key": "stored",
             "agent": types.SimpleNamespace(session_id="stored"), "running": False}
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(server, "_sessions", {"s": state})
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: state)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    cleared = []
    monkeypatch.setattr(server, "_clear_inflight_turn", cleared.append)

    def emit(frame):
        if frame["type"] == "turn.end":
            # The old computation released admission; a new one owns the SID
            # when publication of the old terminal fails.
            host._active_request_ids["s"] = "B"
            state["running"] = True
            raise OSError("terminal output failed")

    monkeypatch.setattr(host, "emit", emit)
    try:
        host._active_request_ids["s"] = "A"
        host._run_real_turn({"sid": "s", "request_id": "A", "text": "old"})
        assert state["running"] is True
        assert host._active_request_ids["s"] == "B"
        assert cleared == []
    finally:
        host.close()


@pytest.mark.parametrize('route,cut', [
    ('queue', 'successor'), ('agent_init_failed', 'successor'),
    ('one_turn_model_restore_failed', 'successor'), ('resume_history_unavailable', 'successor'),
    ('queue', 'stop'), ('queue', 'replacement'), ('queue', 'write_failure'),
])
def test_error_publication_precedes_actual_successor_start_and_keeps_exact_custody(live_turn, monkeypatch, route, cut):
    from tui_gateway import event_replay
    from collections import OrderedDict
    session, _ = live_turn
    ready = threading.Event(); ready.set()
    session.update(running=False, agent_ready=ready, agent_error=None)
    monkeypatch.setattr(server.threading, 'Thread', _REAL_THREAD)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    monkeypatch.setattr(server, '_persist_branch_seed', lambda *a: None)
    monkeypatch.setattr(server, '_clear_interrupt_waiters', lambda *a: None)
    monkeypatch.setattr(event_replay, '_replay_buffers', OrderedDict())
    monkeypatch.setattr(event_replay, '_replay_next_seq', {})
    accepts, sends, cuts, output, errors = [], [], [], [], []
    def accept(state, text, **kw):
        accepts.append(text)
        return types.SimpleNamespace(event_id='accepted-B')
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    entered = threading.Event(); release_b = threading.Event()
    ran = []
    def conversation(text, *, persist_user_event_id=None, **kw):
        ran.append((text, persist_user_event_id)); entered.set()
        assert release_b.wait(3), 'test did not release successor conversation'
        return {'final_response': 'B complete', 'completed': False, 'api_calls': 0}
    monkeypatch.setattr(session['agent'], 'run_conversation', conversation)
    def write(frame):
        acquired = session['history_lock'].acquire(timeout=0.5)
        assert acquired, 'terminal transport entered under history_lock'
        session['history_lock'].release()
        if (cut == 'write_failure' and frame['params']['type'] == 'message.complete'
                and frame['params'].get('payload', {}).get('error_surface', {}).get('code') == 'queue_recovery_failed'):
            raise OSError('inert uncertain terminal write')
        output.append(frame)
    transport = types.SimpleNamespace(write=write)
    session['transport'] = transport
    monkeypatch.setattr(server, 'write_json', REAL_WRITE_JSON)
    attempt = threading.Event(); publication_decision = []
    def observe(event, sid, payload=None):
        if event == 'message.start':
            current = server._sessions[sid]
            with current['history_lock']:
                lock = current.setdefault('_lifecycle_projection_lock', threading.RLock())
            acquired = lock.acquire(blocking=False)
            if acquired:
                try:
                    result = REAL_EMIT(event, sid, payload)
                    publication_decision.append('published')
                    attempt.set()
                    return result
                finally: lock.release()
            publication_decision.append('blocked')
            attempt.set()
        return REAL_EMIT(event, sid, payload)
    monkeypatch.setattr(server, '_emit', observe)
    barrier = threading.Event(); release_a = threading.Event()
    original_render = server.render_message
    def render(text, cols):
        if str(text).startswith('Error:') and not barrier.is_set():
            barrier.set()
            assert release_a.wait(3), 'test did not release predecessor publication'
        return original_render(text, cols)
    monkeypatch.setattr(server, 'render_message', render)
    if route == 'queue':
        session['queued_prompt'] = {'text': 'head A', 'context_input_event_id': 'existing-A'}
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
        supervisor = types.SimpleNamespace(boot_id='inert-boot', submit_turn=lambda *a, **kw: sends.append(True))
        def get_supervisor(*a):
            cuts.append(True)
            session['_queued_prompt_generation'] = len(cuts)
            return supervisor
        monkeypatch.setattr(server, '_get_compute_host_supervisor', get_supervisor)
        def fail_a(): server._drain_queued_prompt('queue-A', 'selected', session)
    else:
        with session['history_lock']:
            session['running'] = True
            server._start_inflight_turn(session, 'head A')
            started = session['inflight_turn']['started_at']
            ref = server._begin_turn_outcome(session, 'selected', 'terminal-A', 'inline')
        def fail_a():
            token = server._turn_outcome_execution.set((session, 'selected', ref['request_id']))
            try:
                server._emit_terminal_turn_error('selected', session, 'inert pre-provider error',
                    error_surface={'layer': 'runtime', 'code': route, 'retryable': True},
                    expected_started_at=started, expected_queue_generation=0, settle_running=True,
                    terminal_transport=transport, context_input_event_id='existing-A')
            finally: server._turn_outcome_execution.reset(token)
    def worker_a():
        try: fail_a()
        except Exception as exc: errors.append(exc)
    worker = _REAL_THREAD(target=worker_a, daemon=True); worker.start()
    successor = session
    later_worker = None
    try:
        assert barrier.wait(2), 'actual terminal helper did not reach the pre-write barrier'
        assert not session['running'] and session['inflight_turn']['status'] == 'error'
        old_window = session['_turn_outcomes']
        a_nonce = old_window.turns[-1]['accepted_turn']['request_id']
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
        if cut == 'stop':
            server._interrupt_session_turn('selected', session, request_id='stop-A')
        elif cut == 'replacement':
            successor = dict(session)
            successor.update(history_lock=threading.Lock(), running=False, inflight_turn=None)
            for key in ['_turn_outcomes', '_lifecycle_projection_lock', '_run_thread']:
                successor.pop(key, None)
            server._sessions['selected'] = successor
        result = server._methods['prompt.submit']('rpc-B', {'session_id': 'selected', 'text': 'B'})
        assert not result.get('error'), result
        b_nonce = result['result']['accepted_turn']['request_id']
        assert attempt.wait(2), 'actual successor did not attempt message.start'
        b_inflight = successor['inflight_turn']
        b_generation = successor.get('_queued_prompt_generation', 0)
        # The same record may admit B, but a pending A terminal must precede
        # its visible start. A replacement has its own barrier and may start.
        decision = publication_decision[0]
        release_a.set(); worker.join(2)
        assert not worker.is_alive()
        assert entered.wait(2), 'successor did not enter its inert conversation'
        with server._sessions_lock: later_worker = successor['_run_thread']
        assert successor['running'] and successor['inflight_turn'] is b_inflight
        assert successor.get('_queued_prompt_generation', 0) == b_generation
        assert successor['_turn_outcomes'].turns[-1]['accepted_turn']['request_id'] == b_nonce
        assert old_window.find(a_nonce)['state'] == 'error'
        terminal_a = [f for f in output if f['params']['type'] == 'message.complete'
                      and f['params'].get('payload', {}).get('error_surface', {}).get('code')
                      == ('queue_recovery_failed' if route == 'queue' else route)]
        if cut == 'successor':
            assert decision == 'blocked', 'successor visible start overtook the pending error publication'
            assert len(terminal_a) == 1
            assert terminal_a[0]['params']['payload']['request_id'] == a_nonce
            assert output.index(terminal_a[0]) < next(i for i, f in enumerate(output) if f['params']['type'] == 'message.start')
            if route == 'queue':
                assert terminal_a[0]['params']['payload']['input_event_id'] == 'existing-A'
        else:
            assert not terminal_a, 'old terminal projected through Stop/replacement/failed write'
        if cut == 'write_failure':
            assert len(errors) == 1 and isinstance(errors[0], OSError)
            replay = event_replay.events_since('selected', 0)
            a = next(e for e in replay if e['type'] == 'message.complete' and e.get('payload', {}).get('request_id') == a_nonce)
            b = next(e for e in replay if e['type'] == 'message.start')
            assert a['seq'] < b['seq'] and a['payload']['input_event_id'] == 'existing-A'
        else: assert not errors
        assert accepts == ['B'] and ran == [('B', 'accepted-B')] and not sends
        if route == 'queue': assert len(cuts) == 4
        release_b.set(); later_worker.join(2)
        assert not later_worker.is_alive() and not successor['running']
        assert any(f['params']['type'] == 'message.complete'
                   and f['params'].get('payload', {}).get('request_id') == b_nonce for f in output)
    finally:
        release_a.set(); release_b.set(); worker.join(2)
        with server._sessions_lock: handle = successor.get('_run_thread')
        if handle is not None and handle.ident is not None: handle.join(2)


@pytest.mark.parametrize('operation', ['completion', 'provider_error'])
def test_late_concrete_terminal_cannot_mutate_or_complete_started_successor(live_turn, monkeypatch, operation):
    session, _ = live_turn
    output = _real_transport(session, monkeypatch)
    with session['history_lock']:
        a = server._begin_turn_outcome(session, 'selected', 'old-A', 'inline')
        server._start_inflight_turn(session, 'B')
        b = server._begin_turn_outcome(session, 'selected', 'new-B', 'inline')
        inflight = session['inflight_turn']
    token = server._turn_outcome_execution.set((session, 'selected', b['request_id']))
    try: server._emit('message.start', 'selected')
    finally: server._turn_outcome_execution.reset(token)
    token = server._turn_outcome_execution.set((session, 'selected', a['request_id']))
    try:
        if operation == 'completion':
            server._emit('message.complete', 'selected', {'text': 'old result', 'status': 'error', 'error': 'old failure'})
            assert session['_turn_outcomes'].find('old-A')['finalized'][0]['error'] == 'old failure'
        else:
            before = list(session['history'])
            session['agent']._session_messages = [{'role': 'assistant', 'content': 'old A'}]
            assert server._restore_agent_history_after_turn_error(session, session['agent']) is False
            assert session['history'] == before
            assert server._emit_terminal_turn_error('selected', session, 'old provider failure') is False
    finally: server._turn_outcome_execution.reset(token)
    assert session['running'] and session['inflight_turn'] is inflight and not inflight.get('error')
    assert session['_turn_outcomes'].find('new-B')['state'] == 'running'
    assert [f['params']['type'] for f in output] == ['message.start']
