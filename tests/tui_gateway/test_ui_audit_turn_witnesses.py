"""Bounded audit witnesses for live-turn ownership, no real provider/host calls."""
import sys
import threading
from unittest.mock import Mock
from tui_gateway import server
from tui_gateway.host_supervisor import HostSupervisor


def test_busy_rejection_must_not_settle_another_live_turn_or_disable_stop(monkeypatch):
    record = {'session_key':'stored-audit','agent':None,'running':True,'history':[],
              'history_lock':threading.Lock(),'_compute_host_active':True,
              'inflight_turn':{'request_id':'actual-live','started_at':1.0}}
    emit = Mock()
    supervisor = Mock()
    monkeypatch.setattr(server,'_session_uses_compute_host',lambda s: True)
    monkeypatch.setattr(server,'_get_compute_host_supervisor',lambda:supervisor)
    monkeypatch.setattr(server,'_emit',emit)
    monkeypatch.setattr(server,'_session_info',lambda *a:{'running':record['running']})
    monkeypatch.setattr(server,'_drain_queued_prompt',Mock())
    monkeypatch.setattr(server,'_clear_pending',Mock())
    # Host refused another submission because the actual-live request still runs.
    server._on_compute_host_turn_done('rejected-submit','runtime',record,
        {'type':'turn.error','sid':'runtime','request_id':'rejected-submit','message':'session busy'})
    server._interrupt_session_turn('runtime',record,request_id='stop')
    print({'running_after_unrelated_rejection':record['running'],
           'host_interrupt_count':supervisor.interrupt.call_count,
           'events':[a.args[0] for a in emit.call_args_list]})
    assert record['running'], 'a rejected competing submit must not make the actual live turn idle'
    supervisor.interrupt.assert_called_once()


def test_rejected_competing_turn_must_not_consume_recovered_owner_observer(tmp_path):
    supervisor = HostSupervisor(registry_path=tmp_path/'host.json',argv=[sys.executable,'-c',''],autostart=False)
    rejected_done, live_done = threading.Event(), threading.Event()
    live_callback = Mock(side_effect=lambda frame: live_done.set())
    rejected_callback = Mock(side_effect=lambda frame: rejected_done.set())
    supervisor.observe_session('runtime',live_callback, request_id='actual-live')
    supervisor._pending_turns['rejected-submit'] = ('runtime',rejected_callback,supervisor.boot_id)
    supervisor._complete_turn({'type':'turn.error','sid':'runtime','request_id':'rejected-submit','message':'session busy'})
    assert rejected_done.wait(2), 'rejected request callback did not settle'
    print({'live_observer_calls':live_callback.call_count,'observer_still_registered':'runtime' in supervisor._session_observers,
           'rejected_callback_calls':rejected_callback.call_count})
    assert not live_callback.called, 'session-only observer accepted another request terminal frame'
    assert 'runtime' in supervisor._session_observers
    supervisor._complete_turn({'type':'turn.end','sid':'runtime','request_id':'actual-live'})
    assert live_done.wait(2), 'the real owner terminal must still reach its observer'
    live_callback.assert_called_once()
    rejected_callback.assert_called_once()
    assert 'runtime' not in supervisor._session_observers
