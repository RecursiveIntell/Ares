"""Standalone custom-endpoint summaries must not retry governance failures."""
from types import SimpleNamespace
import logging

import httpx
import openai
import pytest

from hermes_cli import middleware
from ares_runtime.physical_dispatch import bind_inference_client
from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401


@pytest.mark.parametrize('failure',['deny','unknown'])
def test_raw_trajectory_summary_preserves_typed_no_retry(endpoint,monkeypatch,failure):
    from trajectory_compressor import TrajectoryCompressor,TrajectoryMetrics
    monkeypatch.setattr('trajectory_compressor._effective_temperature_for_model', lambda *args: 0)
    url,bodies,_=endpoint
    error=middleware.EgressDispatchDenied('DENIED') if failure=='deny' else middleware.EgressOutcomeUnrecorded()
    begins=[]
    def govern(*,dispatch_phase,**kw):
        begins.append(dispatch_phase)
        if dispatch_phase=='physical_send':
            if failure=='deny':
                raise error
            def settle(**kw):
                raise error
            return SimpleNamespace(settle=settle)
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [govern])
    value=TrajectoryCompressor.__new__(TrajectoryCompressor)
    value.config=SimpleNamespace(summary_target_tokens=3,max_retries=3,temperature=0,base_url=url,summarization_model='local',retry_delay=0)
    value.logger=logging.getLogger('fixture-summary')
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.Client(trust_env=False))) as sdk:
        value.client=sdk
        with pytest.raises(type(error)) as caught:
            value._generate_summary('private',TrajectoryMetrics())
    assert caught.value is error
    assert begins.count('physical_send')==1
    assert len(bodies)==(0 if failure=='deny' else 1)
