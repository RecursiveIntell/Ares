"""Audit witnesses for hydration, scope and host-owned reasoning behavior.

The host-control positive case must supply a confirmed boot and owner readback;
a no-boot fixture can only prove a fail-closed refusal, not host delivery.
"""
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tui_gateway import server

@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock())
    monkeypatch.setattr(server, '_enable_gateway_prompts', lambda: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_start_agent_build', Mock())
    monkeypatch.setattr(server, '_maybe_schedule_auto_continue', lambda *a: None)

@pytest.mark.parametrize('host_exists', [False, True])
def test_resumed_record_has_a_reachable_hydration_completion(monkeypatch, host_exists):
    class DB:
        def get_session(self, target): return {'id': target, 'message_count': 1}
        def resolve_resume_session_id(self, target): return target
        def assert_resume_safe(self, target): pass
        def reopen_session(self, target): pass
        def get_resume_conversations(self, target):
            return ([{'role':'user','content':'audit fixture'}], [{'role':'user','content':'audit fixture'}])
        def get_ancestor_display_prefix(self, target): return []
    class Supervisor:
        def lookup_session_key(self, key):
            return {'session_id':'host-owner','running':False,'session_info':{'model':'fixture','provider':'fixture','reasoning_effort':'medium'}} if host_exists else None
        def observe_session(self, sid, callback): pass
    monkeypatch.setattr(server, '_get_db', lambda: DB())
    monkeypatch.setattr(server, '_turn_isolation_enabled', lambda *a: True)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: Supervisor())
    response = server.handle_request({'id':'audit-resume','method':'session.resume','params':{'session_id':'stored-audit','source':'desktop','defer_history':True,'omit_messages':True}})
    assert 'error' not in response, response
    record = server._sessions[response['result']['session_id']]
    completed = record['resume_history_ready'].wait(0.4)
    print({'host_exists':host_exists,'returned_sid':response['result']['session_id'],'hydrating':record['resume_hydrating'],'stage':record.get('resume_hydration_stage','unknown'),'history_ready':completed,'build_started':server._start_agent_build.call_count})
    assert completed, 'adopted compute-host record has an unset history event and no scheduled hydrator'


def test_stale_reasoning_target_must_not_change_profile_default(monkeypatch):
    writes = Mock()
    monkeypatch.setattr(server, '_write_config_key', writes)
    response = server.handle_request({'id':'stale-reasoning','method':'config.set','params':{'key':'reasoning','session_id':'retired-runtime','value':'high'}})
    print({'response':response,'global_writes':writes.call_args_list})
    assert not writes.called, 'stale session reasoning write escaped into profile config'
    assert response['error']['code'] == 4001


def test_reasoning_change_must_reach_compute_host_owner(monkeypatch):
    parent = SimpleNamespace(model='fixture', provider='fixture', reasoning_config={'enabled':True,'effort':'medium'})
    record = {'session_key':'stored-audit','agent':parent,'_compute_host_active':True,'running':False,'history':[], 'history_lock':threading.Lock(), '_metadata_mirror':{'reasoning_effort':'medium'}}
    server._sessions['live'] = record
    control = Mock(return_value={
        'type': 'control.ack', 'sid': 'live', 'route_name': 'config.set.reasoning',
        '_host_boot_id': 'boot-audit',
        'result': {'key': 'reasoning', 'value': 'high'},
        'session_info': {'reasoning_effort': 'high'},
    })
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: True)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: SimpleNamespace(boot_id='boot-audit'))
    monkeypatch.setattr(server, '_send_compute_host_control', control)
    response = server.handle_request({'id':'host-reasoning','method':'config.set','params':{'key':'reasoning','session_id':'live','value':'high'}})
    assert isinstance(response, dict) and 'error' not in response, response
    assert control.call_count == 1
    assert parent.reasoning_config == {'enabled': True, 'effort': 'medium'}
    assert record['create_reasoning_override'] == {'enabled': True, 'effort': 'high'}
    assert record['_metadata_mirror']['reasoning_effort'] == 'high'


def test_session_info_reports_owner_reasoning_not_stale_parent(monkeypatch):
    parent = SimpleNamespace(model='fixture', provider='fixture', reasoning_config={'enabled':True,'effort':'low'})
    record = {'session_key':'stored-audit','agent':parent,'_compute_host_active':True,'_metadata_mirror':{'model':'fixture','provider':'fixture','reasoning_effort':'high'},'running':False}
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: True)
    info = server._session_info(parent, record)
    print({'owner':'high','parent':'low','reported':info['reasoning_effort']})
    assert info['reasoning_effort'] == 'high', 'session.info discards host reasoning authority'
