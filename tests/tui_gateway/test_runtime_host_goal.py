"""Host goal admission through real manager, supervisor and native turn driver.

Only construction/provider responses are controlled; no model requests run.
"""
import copy
import io
import threading

import pytest
from hermes_cli import goals
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor
from tests.tui_gateway.test_model_once_turn_runtime_owner import (  # noqa: F401
    live_turn, routes, gateway, turn_env, agent, _REAL_THREAD,
)


@pytest.fixture
def host_goal(live_turn, tmp_path, monkeypatch):
    parent, events = live_turn
    monkeypatch.setattr(server.threading, 'Thread', _REAL_THREAD)
    parent.update(running=False)
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session(parent['session_key'], source='tui')
    mgr = goals.GoalManager(parent['session_key'])
    mgr.set('finish authorized work')
    mgr.checkpoint_recovery('RETRYABLE_FAILURE')
    value = copy.copy(parent['agent'])
    runs = []
    monkeypatch.setattr(value, 'run_conversation', lambda text, **kwargs: (
        runs.append(text), {'final_response': 'controlled completion', 'completed': True, 'messages': [], 'api_calls': 0})[1])
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('done', 'controlled judge', False, None, False))
    monkeypatch.setattr(server, '_make_agent', lambda *a, **k: value)
    monkeypatch.setattr(server, '_start_notification_poller', lambda *a: None)
    monkeypatch.setattr(server, '_schedule_mcp_late_refresh', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: None)
    monkeypatch.setattr(server, '_persist_branch_seed', lambda *a: None)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    parent_registry = server._sessions
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    supervisor = HostSupervisor(registry_path=tmp_path/'host.json', autostart=False)
    supervisor._hello = {'boot_id': host._boot_id}
    monkeypatch.setattr(supervisor, '_ensure_started_by', lambda *a: None)
    monkeypatch.setattr(server, '_compute_host_supervisor', supervisor)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: supervisor)
    sent = []
    terminals = []
    monkeypatch.setattr(host, 'emit', lambda frame: terminals.append(frame) if frame['type'] in {'turn.end', 'turn.error'} else None)
    def send(frame, **kwargs):
        sent.append(copy.deepcopy(frame))
        monkeypatch.setattr(server, '_sessions', {})
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
        try:
            host._active_request_ids['selected'] = frame['request_id']
            host._run_real_turn(frame)
        finally:
            monkeypatch.setattr(server, '_sessions', parent_registry)
            monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
        assert len(terminals) == 1
        supervisor._complete_turn({**terminals[0], '_host_boot_id': host._boot_id})
        from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
        wait_for_terminal_projection(supervisor)
    monkeypatch.setattr(supervisor, '_send_frame', send)
    yield mgr, parent, runs, sent, terminals
    host.close()
    goals._DB_CACHE.clear()


def test_host_continuation_consumes_and_runs_native_once(host_goal):
    mgr, parent, runs, sent, terminals = host_goal
    binding = {'goal_id': mgr.state.goal_id, 'checkpoint_revision': mgr.state.checkpoint_revision,
               'continuation_token': mgr.state.continuation_token}
    assert server._dispatch_goal_continuation('resume', 'selected', parent, expected=binding)
    assert len(runs) == 1
    assert 'finish authorized work' in runs[0]
    assert sent[0]['display_metadata']['goal_continuation'] == binding
    assert terminals[0]['type'] == 'turn.end'
    assert goals.GoalManager(parent['session_key']).state.turns_used == 1
    assert not server._dispatch_goal_continuation('duplicate', 'selected', parent, expected=binding)
    assert len(runs) == 1


@pytest.mark.parametrize('cut', ['pause', 'clear', 'stop', 'rebind'])
def test_host_postconsume_cut_prevents_physical_dispatch(host_goal, monkeypatch, cut):
    mgr, parent, runs, sent, terminals = host_goal
    original = goals.GoalManager.start_continuation
    def consume(self, **kwargs):
        admitted = original(self, **kwargs)
        if admitted:
            if cut == 'pause': goals.GoalManager(parent['session_key']).pause()
            if cut == 'clear': goals.GoalManager(parent['session_key']).clear()
            if cut == 'rebind': server._sessions['selected'] = {'history_lock': threading.Lock()}
        return admitted
    monkeypatch.setattr(goals.GoalManager, 'start_continuation', consume)
    if cut == 'stop':
        original_render = goals.GoalManager.next_continuation_prompt
        def stop_before_render(self):
            server._interrupt_session_turn('selected', parent)
            return original_render(self)
        monkeypatch.setattr(goals.GoalManager, 'next_continuation_prompt', stop_before_render)
    assert not server._dispatch_goal_continuation('resume', 'selected', parent)
    assert runs == sent == []


def test_lost_host_receipt_keeps_consumed_admission_unknown_after_restart(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    supervisor = server._compute_host_supervisor
    def uncertain_write(*a, **k):
        raise OSError('controlled lost transport after offering frame')
    monkeypatch.setattr(supervisor, '_send_frame', uncertain_write)
    assert server._dispatch_goal_continuation('resume', 'selected', parent)
    request = parent['_compute_host_active_request_id']
    assert goals.GoalManager(parent['session_key']).state.migration['continuation_admission']
    supervisor._fail_pending_turns(reason='controlled_parent_loss', message='unknown')
    from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
    wait_for_terminal_projection(supervisor)
    assert not runs
    restarted = goals.GoalManager(parent['session_key'])
    assert restarted.state.migration['continuation_admission']
    assert not restarted.start_continuation()
    assert not server._dispatch_goal_continuation('replay', 'selected', parent)
    assert not runs
    assert request not in supervisor._pending_turns


def test_host_pause_before_consume_preserves_no_execution(host_goal):
    mgr, parent, runs, sent, terminals = host_goal
    mgr.pause()
    assert not server._dispatch_goal_continuation('resume', 'selected', parent)
    assert runs == sent == []


def test_two_consumers_share_one_host_admission(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    supervisor = server._compute_host_supervisor
    original = supervisor._send_frame
    entered, release = threading.Event(), threading.Event()
    def barrier(frame, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(frame, **kwargs)
    monkeypatch.setattr(supervisor, '_send_frame', barrier)
    outcomes = []
    worker = _REAL_THREAD(target=lambda: outcomes.append(
        server._dispatch_goal_continuation('first', 'selected', parent)), daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        assert not server._dispatch_goal_continuation('second', 'selected', parent)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [True]
    assert len(runs) == len(sent) == len(terminals) == 1


def test_host_native_followup_uses_new_checkpoint_inside_same_receipt(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    decisions = iter([('continue', 'controlled next step', False, None, False),
                      ('done', 'controlled verified completion', False, None, False)])
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: next(decisions))
    assert server._dispatch_goal_continuation('automatic', 'selected', parent)
    assert len(runs) == 2
    assert len(sent) == len(terminals) == 1
    current = goals.GoalManager(parent['session_key']).state
    assert current.turns_used == 2
    assert current.continuation_pending is False


def test_unknown_host_owner_cannot_admit_a_fresh_request(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    parent['_host_delivery_uncertain'] = True
    result = server._submit_prompt_to_compute_host('new', 'selected', parent, 'fresh input')
    assert result['error']['data']['delivery'] == 'owner_busy'
    assert not runs and not sent


def test_host_rebind_during_supervisor_resolution_denies_consumed_generation(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    supervisor = server._compute_host_supervisor
    def rebind(*args):
        parent['transport'] = object()
        return supervisor
    monkeypatch.setattr(server, '_get_compute_host_supervisor', rebind)
    assert not server._dispatch_goal_continuation('resume', 'selected', parent)
    assert not runs and not sent


def test_parent_loss_unknown_receipt_keeps_closing_record_owned(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    supervisor = server._compute_host_supervisor
    def lost(*a, **k):
        raise OSError('controlled lost transport')
    monkeypatch.setattr(supervisor, '_send_frame', lost)
    assert server._dispatch_goal_continuation('resume', 'selected', parent)
    request = parent['_compute_host_active_request_id']
    assert server._close_session_by_id('selected')
    supervisor._fail_pending_turns(reason='parent_loss', message='unknown')
    from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
    wait_for_terminal_projection(supervisor)
    try:
        assert parent['_compute_host_active_request_id'] == request
        assert not parent.get('_finalized')
        assert server._retiring_sessions[id(parent)] is parent
        assert not runs
    finally:
        # Isolated fixture has no worker/effects; retire its test observer only.
        parent['running'] = False
        parent.pop('_compute_host_active_request_id', None)
        parent.pop('_host_delivery_uncertain', None)
        thread = parent.get('_close_settlement_thread')
        if thread:
            thread.join(2)


def test_old_host_terminal_preserves_a_newer_checkpoint(host_goal, monkeypatch):
    mgr, parent, runs, sent, terminals = host_goal
    supervisor = server._compute_host_supervisor
    complete = supervisor._complete_turn
    newer = []
    def install_newer_checkpoint(frame):
        current = goals.GoalManager(parent['session_key'])
        current.set('newer authorized task')
        current.checkpoint_recovery('RETRYABLE_FAILURE')
        newer.append((current.state.goal_id, current.state.checkpoint_revision, current.state.continuation_token))
        return complete(frame)
    monkeypatch.setattr(supervisor, '_complete_turn', install_newer_checkpoint)
    assert server._dispatch_goal_continuation('resume', 'selected', parent)
    current = goals.GoalManager(parent['session_key']).state
    assert current.continuation_pending
    assert (current.goal_id, current.checkpoint_revision, current.continuation_token) == newer[0]
    assert len(runs) == 1
