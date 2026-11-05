"""Private broker witnesses: real SQLite policy, strict frames, no provider work."""
import copy
import json
import socket
import sqlite3
import struct
import tempfile
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent.graph_admission import GraphAdmissionService, GraphAdmissionSocket, call_graph_admission, SCHEMA
from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from hermes_state import SessionDB

VERSION='sha256:'+'a'*64
DIGEST='sha256:'+'b'*64

@pytest.fixture
def ipc_path():
    # The normal pytest cache path exceeds Linux sockaddr_un's 107-byte bound.
    # Use an explicitly short, private test-only directory, not a production
    # relocation/fallback or a mocked transport.
    with tempfile.TemporaryDirectory(prefix='ag-', dir='/tmp') as path:
        yield Path(path)


@pytest.fixture
def owner(tmp_path):
    home=tmp_path/'profile';home.mkdir()
    db=SessionDB(home/'state.db');db.create_session('root','cli',model='main-unchanged')
    db.set_orchestration_policy('root',OrchestrationPolicy(
        secondary_route=SecondaryRoute('openai-codex','Exact-Worker','low')).to_dict(),
        expected_revision=0,origin='operator-command')
    parent=SimpleNamespace(_session_db=db,session_id='root',_delegate_depth=0)
    yield parent
    db.close()


def issue(service,parent,**kwargs):
    return service.issue(parent,graph_id='graph-fixture',graph_version=VERSION,
        request_digest=DIGEST,**kwargs)


def request(scope,stage='launch',**kwargs):
    value={'schema':SCHEMA,'reference':scope.reference,'stage':stage,
        'graph_id':scope.graph_id,'graph_version':scope.graph_version,
        'request_digest':scope.request_digest,'execution_route':scope.execution_route,
        'run_id':'run-fixture'}
    if stage=='provider':value.update(node_id='ask',invocation_ref='invocation-1')
    value.update(kwargs)
    return value


def deny(parent):
    parent._session_db.set_orchestration_policy(parent.session_id,
        OrchestrationPolicy(allow_graph=False).to_dict(),expected_revision=1,
        origin='operator-command')


def count(parent):
    with sqlite3.connect(parent._session_db.db_path) as conn:
        return conn.execute('SELECT count(*) FROM session_orchestration_attempts').fetchone()[0]


def test_issue_uses_host_owner_and_pins_only_nonsecret_route(owner):
    service=GraphAdmissionService()
    scope=issue(service,owner)
    assert scope.execution_route=={'provider':'openai-codex','model':'Exact-Worker','reasoning_effort':'low'}
    assert scope.reference not in repr(scope)
    assert 'session_id' not in scope.metadata()
    assert 'api_key' not in json.dumps(scope.metadata())
    assert count(owner)==0  # issuance is not a provider/launch claim
    assert owner._session_db.get_session('root')['model']=='main-unchanged'


def test_denial_precedes_ticket_creation(owner):
    deny(owner)
    with pytest.raises(OrchestrationError):issue(GraphAdmissionService(),owner)
    assert count(owner)==0


def test_json_parent_cannot_impersonate_a_trusted_host(owner):
    with pytest.raises(OrchestrationError):
        issue(GraphAdmissionService(),{'session_id':'root','_session_db':owner._session_db})


def test_launch_and_provider_are_owner_claims_and_replay_is_not_cached_allow(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    first=service.authorize(request(scope));assert first['ok'] is True
    provider=service.authorize(request(scope,'provider'));assert provider['ok'] is True
    assert first['admission']['attempt_id']!=provider['admission']['attempt_id']
    assert provider['admission']['revision']==1
    assert service.authorize(request(scope))['code']=='graph_admission_replay'
    assert service.authorize(request(scope,'provider'))['code']=='graph_admission_replay'
    assert count(owner)==2


def test_provider_must_follow_launch_and_cannot_change_run(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    assert service.authorize(request(scope,'provider'))['code']=='graph_admission_launch_required'
    assert service.authorize(request(scope))['ok'] is True
    assert service.authorize(request(scope,'provider',run_id='run-other'))['code']=='graph_admission_binding_mismatch'
    assert count(owner)==1


@pytest.mark.parametrize('mutation',['graph_id','graph_version','request_digest','execution_route','policy'])
def test_forged_binding_or_claimed_permission_never_widens_scope(owner,mutation):
    service=GraphAdmissionService();scope=issue(service,owner);value=request(scope)
    if mutation=='execution_route':value[mutation]['model']='Other-Model'
    elif mutation=='policy':value['allow_graph']=True
    elif mutation=='graph_id':value[mutation]='other-graph'
    else:value[mutation]='sha256:'+'c'*64
    result=service.authorize(value)
    assert result['ok'] is False
    assert count(owner)==0


def test_latest_optout_denies_queued_launch_and_provider_start(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    assert service.authorize(request(scope))['ok'] is True
    deny(owner)
    result=service.authorize(request(scope,'provider'))
    assert result['ok'] is False
    assert result['code']=='orchestration_revision_changed'
    assert count(owner)==1
    service.retire(scope)  # cleanup does not require permission


def test_expiry_capacity_and_retirement_are_explicit(owner):
    now=[0.0]
    service=GraphAdmissionService(clock=lambda:now[0],max_scopes=1)
    scope=issue(service,owner,ttl_seconds=1)
    with pytest.raises(OrchestrationError,match='graph_admission_capacity'):
        issue(service,owner)
    now[0]=2.0
    assert service.authorize(request(scope))['code']=='graph_admission_expired'
    fresh=issue(service,owner)
    service.retire(fresh)
    assert service.authorize(request(fresh))['code']=='graph_admission_unknown'


def test_two_concurrent_launches_commit_only_one(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:service.authorize(request(scope)),range(2)))
    assert sum(r['ok'] for r in results)==1
    assert count(owner)==1


def test_ended_or_unavailable_store_blocks_provider(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    service.authorize(request(scope))
    owner._session_db.end_session('root','finished')
    assert service.authorize(request(scope,'provider'))['ok'] is False
    owner._session_db.close()
    assert service.authorize(request(scope,'provider',invocation_ref='invocation-2'))['ok'] is False


def test_nested_workers_and_unqualified_provider_do_not_get_tickets(owner):
    owner._delegate_depth=1
    with pytest.raises(OrchestrationError,match='graph_nested_worker_unqualified'):
        issue(GraphAdmissionService(),owner)
    owner._delegate_depth=0
    owner._session_db.set_orchestration_policy('root',OrchestrationPolicy(
        secondary_route=SecondaryRoute('opencode-go','glm-5.3-flash','high')).to_dict(),
        expected_revision=1,origin='operator-command')
    with pytest.raises(OrchestrationError,match='graph_route_unqualified'):
        issue(GraphAdmissionService(),owner)


def test_private_unix_socket_consumes_real_owner_and_cleans_only_own_endpoint(owner,ipc_path):
    service=GraphAdmissionService();scope=issue(service,owner)
    with GraphAdmissionSocket(service,ipc_path/'private-ipc') as server:
        assert server.endpoint.stat().st_mode & 0o777==0o600
        reply=call_graph_admission(server.endpoint,request(scope))
        assert reply['ok'] is True
        deny(owner)
        denied=call_graph_admission(server.endpoint,request(scope,'provider'))
        assert denied['ok'] is False
        endpoint=server.endpoint
    assert not endpoint.exists()


def test_insecure_directory_and_existing_socket_are_not_repaired_or_overwritten(owner,ipc_path):
    service=GraphAdmissionService();path=ipc_path/'insecure';path.mkdir(mode=0o755)
    with pytest.raises(OrchestrationError,match='graph_admission_directory_unqualified'):
        GraphAdmissionSocket(service,path)
    path=ipc_path/'occupied';path.mkdir(mode=0o700)
    marker=path/'authority.sock';marker.write_text('must remain')
    with pytest.raises(OrchestrationError):GraphAdmissionSocket(service,path)
    assert marker.read_text()=='must remain'


def test_duplicate_json_keys_are_denied_at_socket_boundary(owner,ipc_path):
    service=GraphAdmissionService();scope=issue(service,owner)
    with GraphAdmissionSocket(service,ipc_path/'private-ipc') as server:
        raw=json.dumps(request(scope)).removesuffix('}')+',"stage":"provider"}'
        body=raw.encode()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(2);client.connect(str(server.endpoint))
            client.sendall(struct.pack('!I',len(body))+body)
            header=client.recv(4);length=struct.unpack('!I',header)[0]
            reply=json.loads(client.recv(length))
        assert reply['ok'] is False
        assert reply['code']=='graph_admission_request_invalid'
        assert count(owner)==0


@pytest.mark.parametrize('stage',[None,True,1,[],{},'other'])
def test_invalid_stage_is_typed_denial_not_an_exception(owner,stage):
    service=GraphAdmissionService();scope=issue(service,owner)
    assert service.authorize(request(scope,stage=stage))['ok'] is False
    assert count(owner)==0


def test_scope_route_exports_do_not_mutate_issued_binding(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    exported=scope.metadata();exported['execution_route']['model']='forged'
    assert scope.execution_route['model']=='Exact-Worker'
    assert service.authorize(request(scope))['ok'] is True


def test_expiry_boundary_is_closed(owner):
    now=[0.0];service=GraphAdmissionService(clock=lambda:now[0])
    scope=issue(service,owner,ttl_seconds=1)
    now[0]=1.0
    assert service.authorize(request(scope))['code']=='graph_admission_expired'
    assert count(owner)==0


def test_concurrent_provider_replay_commits_only_one_attempt(owner):
    service=GraphAdmissionService();scope=issue(service,owner)
    assert service.authorize(request(scope))['ok'] is True
    with ThreadPoolExecutor(8) as pool:
        results=list(pool.map(lambda _:service.authorize(request(scope,'provider')),range(16)))
    assert sum(result['ok'] for result in results)==1
    assert count(owner)==2


def test_ambiguous_owner_failure_retires_reference_instead_of_replaying(owner,monkeypatch):
    service=GraphAdmissionService();scope=issue(service,owner)
    original=owner._session_db.claim_orchestration_effect
    def committed_then_lost(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('fixture lost return after owner commit')
    monkeypatch.setattr(owner._session_db,'claim_orchestration_effect',committed_then_lost)
    assert service.authorize(request(scope))['code']=='graph_admission_owner_unavailable'
    assert service.authorize(request(scope))['code']=='graph_admission_unknown'
    assert service.authorize(request(scope,'provider'))['code']=='graph_admission_unknown'
    assert count(owner)==1


def test_invocation_budget_does_not_evict_replay_history(owner):
    service=GraphAdmissionService(max_invocations=1);scope=issue(service,owner)
    assert service.authorize(request(scope))['ok'] is True
    assert service.authorize(request(scope,'provider'))['ok'] is True
    assert service.authorize(request(scope,'provider',invocation_ref='another'))['code']=='graph_admission_capacity'
    assert service.authorize(request(scope,'provider'))['code']=='graph_admission_replay'
    assert count(owner)==2


def test_long_socket_path_is_refused_without_directory_creation(owner,tmp_path):
    path=tmp_path/('x'*108)
    with pytest.raises(OrchestrationError,match='graph_admission_endpoint_too_long'):
        GraphAdmissionSocket(GraphAdmissionService(),path)
    assert not path.exists()


def test_cleanup_does_not_unlink_replacement_file(owner,ipc_path):
    with GraphAdmissionSocket(GraphAdmissionService(),ipc_path/'private-ipc') as server:
        server.endpoint.unlink()
        server.endpoint.write_text('replacement',encoding='utf-8')
    assert server.endpoint.read_text(encoding='utf-8')=='replacement'


def test_unavailable_socket_is_typed_denial(owner,ipc_path):
    service=GraphAdmissionService();scope=issue(service,owner)
    assert call_graph_admission(ipc_path/'missing',request(scope))['code']=='graph_admission_transport_unavailable'
    assert count(owner)==0


def test_real_separate_process_uses_framed_socket_and_observes_revocation(owner,ipc_path):
    service=GraphAdmissionService();scope=issue(service,owner)
    code = ('import json,sys; assert sys.version_info >= (3,14); '
            'from agent.graph_admission import call_graph_admission; '
            'v=json.load(sys.stdin); '
            'print(json.dumps(call_graph_admission(v["endpoint"],v["request"])))')
    home=str(Path(owner._session_db.db_path).parent)
    env={'HOME':home,'HERMES_HOME':home,'PYTHONPATH':str(Path(__file__).resolve().parents[2])}
    with GraphAdmissionSocket(service,ipc_path/'private-ipc') as server:
        def child(value):
            result=subprocess.run([sys.executable,'-c',code],input=json.dumps(
                {'endpoint':str(server.endpoint),'request':value}),text=True,
                capture_output=True,timeout=5,check=True,cwd=home,env=env)
            return json.loads(result.stdout)
        assert child(request(scope))['ok'] is True
        deny(owner)
        result=child(request(scope,'provider'))
        assert result['ok'] is False
        assert result['code']=='orchestration_revision_changed'
    assert count(owner)==1


def test_incomplete_frame_expires_and_does_not_hold_the_listener(owner,ipc_path):
    service=GraphAdmissionService();scope=issue(service,owner)
    with GraphAdmissionSocket(service,ipc_path/'private-ipc') as server:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(str(server.endpoint))
            client.sendall(struct.pack('!I',100)+b'{')
            started=time.monotonic()
            header=client.recv(4);length=struct.unpack('!I',header)[0]
            reply=json.loads(client.recv(length))
            assert time.monotonic()-started < 2.5
            assert reply['ok'] is False
        assert call_graph_admission(server.endpoint,request(scope))['ok'] is True
    assert count(owner)==1
