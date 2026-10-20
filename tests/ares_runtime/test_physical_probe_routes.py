"""Provider probes are inference too; governance failure is not 'unknown tier'."""
import pytest

from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401
from hermes_cli import middleware


def test_gemini_probe_denial_never_sends_or_converts_to_unknown(endpoint,monkeypatch):
    from agent.gemini_native_adapter import probe_gemini_tier
    url,bodies,_=endpoint
    def deny(**kwargs):
        raise middleware.EgressDispatchDenied('UNSUPPORTED_TRANSPORT')
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [deny])
    with pytest.raises(middleware.EgressDispatchDenied):
        probe_gemini_tier('local',base_url=url,model='local')
    assert not bodies


@pytest.mark.parametrize('kind',['deny','unknown'])
def test_bedrock_probe_never_swallows_governance_or_tries_next_tier(endpoint,monkeypatch,kind):
    from types import SimpleNamespace
    from agent import bedrock_adapter
    from tests.ares_runtime.test_bedrock_lease_stream import sdk
    url,bodies,_=endpoint;begins=[]
    client=sdk(url)
    monkeypatch.setattr(bedrock_adapter,'_get_bedrock_runtime_client',lambda _:client)
    monkeypatch.setattr(bedrock_adapter,'_BEDROCK_PROBE_TIERS',[10,20])
    error=middleware.EgressDispatchDenied('DENIED') if kind=='deny' else middleware.EgressOutcomeUnrecorded()
    def govern(**kw):
        begins.append(kw)
        if kind=='deny':
            raise error
        def settle(**kw):
            raise error
        return SimpleNamespace(settle=settle)
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [govern])
    try:
        with pytest.raises(type(error)) as caught:
            bedrock_adapter.probe_bedrock_context_length('local','us-east-1')
    finally:
        client.close()
    assert caught.value is error
    assert len(begins)==1
    assert len(bodies)==(0 if kind=='deny' else 1)
