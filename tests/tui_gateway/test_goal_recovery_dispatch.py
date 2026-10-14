"""Real manager/SQLite admission at the Desktop adapter seam; no provider."""
import threading
import pytest
from hermes_cli import goals
from tui_gateway import server

@pytest.fixture
def seam(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'home'))
    (tmp_path/'home').mkdir()
    goals._DB_CACHE.clear()
    goals._get_session_db().create_session('stored', source='desktop')
    mgr = goals.GoalManager('stored'); mgr.set('work')
    mgr.evaluate_after_turn('', turn_outcome=goals.EXECUTION_FAILED)
    session = {'session_key':'stored', 'history_lock':threading.Lock(), 'running':False}
    monkeypatch.setattr(server, '_sessions', {'runtime':session})
    monkeypatch.setattr(server, '_emit', lambda *a, **k: None)
    calls=[]
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **k: calls.append((a,k)))
    return mgr,session,calls

def test_dispatch_consumes_before_render_and_only_once(seam):
    mgr,session,calls=seam
    assert server._dispatch_goal_continuation('rid','runtime',session)
    assert len(calls)==1
    assert goals.GoalManager('stored').state.continuation_pending is False
    session['running']=False
    assert not server._dispatch_goal_continuation('rid','runtime',session)
    assert len(calls)==1

@pytest.mark.parametrize('cut', ['pause','clear','stop','busy'])
def test_control_or_slot_failure_never_dispatches(seam,cut):
    mgr,session,calls=seam
    if cut=='pause': mgr.pause()
    if cut=='clear': mgr.clear()
    if cut=='stop': session['_turn_cancel_requested']=True
    if cut=='busy': session['running']=True
    assert not server._dispatch_goal_continuation('rid','runtime',session)
    assert calls==[]
    if cut in {'stop','busy'}: assert goals.GoalManager('stored').state.continuation_pending

def test_pause_after_consume_cancels_before_adapter_dispatch(seam,monkeypatch):
    mgr,session,calls=seam
    original=goals.GoalManager.start_continuation
    def cut(self,**kwargs):
        admitted=original(self,**kwargs)
        if admitted: goals.GoalManager('stored').pause()
        return admitted
    monkeypatch.setattr(goals.GoalManager,'start_continuation',cut)
    assert server._dispatch_goal_continuation('rid','runtime',session) is False
    assert calls==[]
    assert goals.GoalManager('stored').start_continuation() is False


def test_busy_compute_host_handoff_retains_checkpoint(seam, monkeypatch):
    mgr,session,calls=seam
    session['_compute_host_active_request_id'] = 'existing-owner'
    before=mgr.state.to_json()
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda session: True)
    assert not server._dispatch_goal_continuation('rid','runtime',session)
    assert calls == []
    assert goals.GoalManager('stored').state.to_json() == before
