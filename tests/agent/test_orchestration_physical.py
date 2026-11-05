"""The real physical HTTP seam: no provider request after revoked admission."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from agent.orchestration_runtime import admit_worker
from ares_runtime.physical_dispatch import bind_http_client, inference_call_scope, bind_deferred_result
from hermes_state import SessionDB


@pytest.fixture
def worker(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('parent', 'cli')
    policy = OrchestrationPolicy(secondary_route=SecondaryRoute('openai-codex','exact-worker','low'))
    db.set_orchestration_policy('parent', policy.to_dict(), expected_revision=0, origin='operator-command')
    parent = SimpleNamespace(_session_db=db, session_id='parent')
    admission = admit_worker(parent)
    child = SimpleNamespace(_orchestration_admission=admission, _delegate_depth=1,
        model='exact-worker', provider='openai-codex', api_mode='codex_responses',
        reasoning_config={'enabled':True,'effort':'low'})
    yield db, child
    db.close()


def client_and_calls():
    calls=[]
    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'ok':True})
    return bind_http_client(httpx.Client(transport=httpx.MockTransport(send))), calls


def scope(child, payload):
    return inference_call_scope(child, payload)


def test_physical_send_observes_exact_model_effort_and_records_admission(worker):
    db, child = worker
    client, calls = client_and_calls()
    payload={'model':'exact-worker','reasoning':{'effort':'low'}}
    with scope(child,payload):
        assert client.post('https://provider.invalid/v1/responses',json=payload).status_code == 200
    assert calls == [payload]
    with db._read_ctx() as c:
        row = c.execute('SELECT * FROM session_orchestration_attempts').fetchone()
        assert row['revision'] == 1
        assert json.loads(row['route_json'])['model'] == 'exact-worker'
    client.close()


@pytest.mark.parametrize('mutation',['policy','model','effort'])
def test_last_moment_changes_deny_before_transport(worker,mutation):
    db,child = worker
    client,calls=client_and_calls()
    payload={'model':'exact-worker','reasoning':{'effort':'low'}}
    with scope(child,payload):
        if mutation == 'policy':
            db.set_orchestration_policy('parent', OrchestrationPolicy(allow_delegation=False).to_dict(),
                expected_revision=1,origin='operator-command')
        elif mutation == 'model':
            payload['model']='silent-substitute'
        else:
            payload['reasoning']['effort']='high'
        with pytest.raises(OrchestrationError):
            client.post('https://provider.invalid/v1/responses',json=payload)
    assert calls == []
    client.close()


def test_lazy_generator_restores_binding_and_rechecks_after_scope_exit(worker):
    db,child=worker
    client,calls=client_and_calls()
    payload={'model':'exact-worker','reasoning':{'effort':'low'}}
    def deferred():
        yield client.post('https://provider.invalid/v1/responses',json=payload)
    with scope(child,payload) as basis:
        result=bind_deferred_result(deferred(),basis)
    db.set_orchestration_policy('parent', OrchestrationPolicy(allow_delegation=False).to_dict(),
        expected_revision=1,origin='operator-command')
    with pytest.raises(OrchestrationError):
        next(result)
    assert calls == []
    client.close()


def test_real_sdk_does_not_retry_policy_denial(worker):
    import openai
    from ares_runtime.physical_dispatch import bind_inference_client
    db, child = worker
    http, calls = client_and_calls()
    sdk = bind_inference_client(openai.OpenAI(api_key='offline-test',
        base_url='https://provider.invalid/v1', http_client=http, max_retries=2))
    with scope(child, {'model':'exact-worker'}):
        db.set_orchestration_policy('parent', OrchestrationPolicy(allow_delegation=False).to_dict(),
            expected_revision=1, origin='operator-command')
        with pytest.raises(OrchestrationError):
            sdk.responses.create(model='exact-worker', input='offline witness', reasoning={'effort':'low'})
    assert calls == []
    sdk.close()


def test_missing_worker_binding_fails_before_any_dispatch_scope():
    child=SimpleNamespace(_delegate_depth=1)
    with pytest.raises(OrchestrationError, match='policy_owner_required'):
        with scope(child,{'model':'anything'}):
            pytest.fail('unbound worker admitted')
