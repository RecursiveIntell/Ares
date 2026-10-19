"""Owner projections, not adapter-created graph/policy/source references."""
from types import SimpleNamespace

import pytest

from ares_runtime.managed_calls import ManagedCallContextV2, produce_dispatch_materialization
from ares_runtime.collaboration import ContractError
from tests.ares_runtime.test_continuity_input import db, accept  # noqa: F401
from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401


def test_sessiondb_materialization_uses_input_owner_and_readback(db):
    receipt = accept(db)
    phase = db.begin_context_input_turn('s', receipt=receipt, turn_lease_holder='holder')
    source = {'source_revision':'ares-source:'+'a'*40, 'source_digest':'sha256:'+'b'*64}
    # This direct owner fixture tests SessionDB linkage, not installed source admission.
    record = db.record_model_dispatch_materialization('s', turn_lease_holder='holder',
        source=source, request_digest='sha256:'+'c'*64, purpose='conversation',
        provider_identity='openai', model_ref='local', api_mode='chat_completions')
    assert db.read_model_dispatch_materialization(record['materialization_ref']) == record
    context = record['context']
    assert context['session_ref'] == 's'
    assert context['session_generation'] == 0
    assert context['turn_ref'] == phase['phase_id']
    assert context['input_digest'] == 'sha256:' + phase['receipts_digest']
    assert 'Correction' not in str(record)
    with pytest.raises(Exception, match='LEASE|OWNER'):
        db.record_model_dispatch_materialization('s', turn_lease_holder='wrong',
            source=source, request_digest='sha256:'+'c'*64, purpose='conversation',
            provider_identity='openai', model_ref='local', api_mode='chat_completions')


def test_source_tuple_requires_actual_release_binding_and_clean_source(tmp_path):
    import subprocess
    import json
    from pathlib import Path
    from ares_runtime.local_runtime import read_dispatch_source_identity, LOCAL_LIFECYCLE_CONTRACT
    repo = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'], text=True).strip()
    source = tmp_path / revision / 'source'
    subprocess.run(['git','clone','--shared','--quiet',str(repo),str(source)], check=True, timeout=30)
    descriptor = source.parent / 'release.json'
    with pytest.raises(ContractError, match='SOURCE_REVISION_UNAVAILABLE'):
        read_dispatch_source_identity(source)
    descriptor.write_text(json.dumps({'revision':revision,'runtime_binding':{
        'schema':'AresLocalRuntimeBindingV1', 'source':str(source), 'controller_contract':LOCAL_LIFECYCLE_CONTRACT}}))
    witness = read_dispatch_source_identity(source)
    assert witness['source_revision'] == 'ares-source:' + revision
    (source / 'ares_runtime/managed_calls.py').write_text('# changed source')
    with pytest.raises(ContractError, match='SOURCE_REVISION_UNAVAILABLE'):
        read_dispatch_source_identity(source)



def test_no_session_is_explicitly_unqualified():
    result = produce_dispatch_materialization(None, {'model': 'local'}, purpose='conversation')
    assert result == {'materialization': None, 'qualification_error': 'SESSION_IDENTITY_UNAVAILABLE'}


def test_unpersisted_session_is_not_a_session_owner():
    result = produce_dispatch_materialization(SimpleNamespace(session_id='not-persisted'), {}, purpose='conversation')
    assert result['materialization'] is None
    assert result['qualification_error'] == 'SESSION_OWNER_UNAVAILABLE'


def test_v2_is_closed_and_never_claims_v1_integrations():
    values = dict(call_ref='ares-call:test', session_ref='s', session_generation=0,
                  turn_ref='turn', input_digest='sha256:'+'a'*64,
                  source_revision='source:test', source_digest='sha256:'+'b'*64,
                  provider_identity='openai', model_ref='model:test', api_mode='chat_completions',
                  purpose='conversation', request_digest='sha256:'+'c'*64)
    artifact = ManagedCallContextV2.create(values)
    assert ManagedCallContextV2.parse(artifact.to_dict()) == artifact
    assert artifact.to_dict()['integration_claims'] == 'not_claimed'
    for key, value in [('purpose', 'auxiliary:invented'), ('integration_claims', 'verified'),
                       ('session_generation', True), ('extra', 'unknown')]:
        with pytest.raises(ContractError):
            ManagedCallContextV2.create(dict(values, **{key:value}))
    assert 'graph_obligation_ref' not in artifact.to_dict()



def test_persisted_owner_release_input_phase_to_actual_send(db,tmp_path,endpoint,monkeypatch):
    import hashlib,json,subprocess
    from pathlib import Path
    from ares_runtime import local_runtime
    from ares_runtime.physical_dispatch import inference_call_scope
    from tests.ares_runtime.test_physical_http_transport import client
    from tests.ares_runtime.test_transport_lease import lease
    from hermes_cli import middleware
    repo=Path(__file__).resolve().parents[2]
    revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    source=tmp_path/'release'/revision/'source'
    subprocess.run(['git','clone','--shared','--quiet',str(repo),str(source)],check=True,timeout=30)
    descriptor=source.parent/'release.json'
    descriptor.write_text(json.dumps({'revision':revision,'runtime_binding':{
        'schema':'AresLocalRuntimeBindingV1','source':str(source),'controller_contract':local_runtime.LOCAL_LIFECYCLE_CONTRACT}}))
    # Bind the source OWNER to a real disposable installed layout, not a stub
    # source tuple. The exact producer calls read_dispatch_source_identity().
    monkeypatch.setattr(local_runtime,'__file__',str(source/'ares_runtime/local_runtime.py'))
    expected_source=local_runtime.read_dispatch_source_identity()
    receipt=accept(db)
    phase=db.begin_context_input_turn('s',receipt=receipt,turn_lease_holder='holder')
    agent=SimpleNamespace(session_id='s',_session_db=db,_active_session_turn_lease_holder='holder',
        provider='openai',model='local',api_mode='chat_completions')
    observations=[];outcomes=[]
    def enforce(*,payload,dispatch_phase,**kw):
        assert dispatch_phase=='physical_send'
        assert payload['qualification_error'] is None
        materialization=payload['materialization']
        assert db.read_model_dispatch_materialization(materialization['materialization_ref'])==materialization
        observations.append(payload)
        return SimpleNamespace(transport_lease=lease(),settle=lambda **kw:outcomes.append(kw))
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [enforce])
    payload={'model':'local','messages':[{'role':'user','content':'private prompt'}],'max_tokens':7}
    url,bodies,_=endpoint
    with client(url) as sdk,inference_call_scope(agent,payload):
        sdk.chat.completions.create(**payload)
    assert len(bodies)==len(observations)==len(outcomes)==1
    context=observations[0]['materialization']['context']
    assert context['source_revision']==expected_source['source_revision']
    assert context['source_digest']==expected_source['source_digest']
    assert context['turn_ref']==phase['phase_id']
    assert context['session_generation']==0 and context['integration_claims']=='not_claimed'
    assert 'private prompt' not in json.dumps(observations)
    assert 'private prompt' not in str(db.read_model_dispatch_materialization(observations[0]['materialization']['materialization_ref']))
    # Readback survives an actual reopen, not an adapter-local cache.
    from hermes_state import SessionDB
    reopened=SessionDB(db.db_path)
    try:
        assert reopened.read_model_dispatch_materialization(observations[0]['materialization']['materialization_ref'])==observations[0]['materialization']
    finally:
        reopened.close()
