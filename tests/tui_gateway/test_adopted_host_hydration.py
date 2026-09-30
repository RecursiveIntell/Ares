"""Adopted host mirrors hydrate display history without constructing an agent."""
import threading
from unittest.mock import Mock

import pytest
from tui_gateway import server
from tests.tui_gateway.test_session_resume_db_ownership import _RecordingDB


@pytest.fixture
def adoption(monkeypatch, tmp_path):
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_enable_gateway_prompts', lambda: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_maybe_schedule_auto_continue', Mock())
    monkeypatch.setattr(server, '_stored_session_runtime_overrides', lambda _: {})
    monkeypatch.setattr(server, '_profile_home', lambda p: tmp_path/'profile' if p else None)
    monkeypatch.setattr(server, '_turn_isolation_enabled', lambda *a: True)
    class Host:
        def lookup_session_key(self, key):
            return {'session_id':'host-owner','running':True,'request_id':'live-turn',
                    'session_info':{'model':'fixture','provider':'fixture'}}
        def observe_session(self, *a, **kw):pass
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: Host())
    builder = Mock(side_effect=AssertionError('adopted mirror constructed a parent agent'))
    monkeypatch.setattr(server, '_start_agent_build', builder)
    return builder


@pytest.mark.parametrize('fail', [False, True])
def test_adopted_profile_hydration_owns_db_and_keeps_live_controls(adoption, monkeypatch, fail):
    opened = []
    class DB(_RecordingDB):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.rows['stored']={'id':'stored','message_count':1}
            self.finished = threading.Event()
            opened.append(self)
        def get_resume_conversations(self, target):
            if fail:raise OSError('fixture history unavailable')
            return ([{'role':'user','content':'history'}],[{'role':'user','content':'history'}])
        def close(self):super().close();self.finished.set()
    # Resume hydration and metadata may open independent profile handles.
    # Do not alias those owners to one fake connection.
    monkeypatch.setattr('hermes_state.SessionDB', DB)
    response = server.handle_request({'id':'resume','method':'session.resume','params':{
        'session_id':'stored','profile':'other','source':'desktop','defer_history':True,'omit_messages':True}})
    assert 'error' not in response, response
    db = opened[0]
    record=server._sessions['host-owner']
    assert record['session_id']=='host-owner'
    assert record['resume_history_ready'].wait(2)
    assert db.finished.wait(2) and db.closed==1
    assert all(handle.closed == 1 for handle in opened)
    assert server._sessions['host-owner'] is record
    assert record['running'] is True and record['_compute_host_active_request_id']=='live-turn'
    assert record['agent'] is None
    adoption.assert_not_called()
    server._maybe_schedule_auto_continue.assert_not_called()
    if fail:
        assert record['resume_history_error']
        assert not record.get('agent_error')
    else:
        assert record['history']==[{'role':'user','content':'history'}]


def test_incidental_mirror_resolution_never_builds_or_waits_for_parent(monkeypatch):
    ready=threading.Event()
    record={'session_key':'stored','agent':None,'agent_ready':ready,'history_lock':threading.Lock(),
            '_compute_host_active':True}
    monkeypatch.setitem(server._sessions,'host-owner',record)
    build=Mock(side_effect=AssertionError('phantom parent build'))
    monkeypatch.setattr(server,'_make_agent',build)
    monkeypatch.setattr('tui_gateway.entry.ensure_mcp_discovery_started',lambda:None)
    resolved,error=server._sess({'session_id':'host-owner'},'incidental')
    assert resolved is record and error is None
    build.assert_not_called()
    assert not record.get('agent_build_started')


def test_hydrator_is_bound_at_scheduling_not_thread_start(monkeypatch):
    old={'session_key':'old','history':[],'history_lock':threading.Lock(),
         'resume_history_ready':threading.Event(),'agent_ready':threading.Event(),'resume_hydrating':True}
    replacement={'session_key':'new','history':[],'history_lock':threading.Lock(),
                 'resume_history_ready':threading.Event(),'agent_ready':threading.Event()}
    monkeypatch.setitem(server._sessions,'same',old)
    targets=[]
    class Thread:
        def __init__(self, target, **kw):targets.append(target)
        def start(self):pass
    db=_RecordingDB();reads=Mock(return_value=([{'role':'user','content':'wrong'}],[]))
    db.get_resume_conversations=reads
    monkeypatch.setattr(server.threading,'Thread',Thread)
    monkeypatch.setattr(server,'_emit',Mock())
    monkeypatch.setattr(server,'_start_agent_build',Mock())
    monkeypatch.setattr(server,'_maybe_schedule_auto_continue',Mock())
    server._schedule_resume_hydration('same','old',db,close_db=True)
    server._sessions['same']=replacement
    targets[0]()
    assert replacement['history']==[]
    assert not replacement['resume_history_ready'].is_set()
    assert old['resume_history_ready'].is_set() and old['resume_history_error']
    reads.assert_not_called()
    assert db.closed==1
