"""Unsupported native calls must be visible before the native callback."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_cli import middleware
from agent.transports.ri_llm import RiPipeline


def test_ri_enforce_denies_before_native_call(monkeypatch):
    pipe = object.__new__(RiPipeline)
    pipe._native = SimpleNamespace(call=Mock(return_value='should not run'))
    observed = []
    def enforce(*, payload, dispatch_phase, **kwargs):
        observed.append((payload, dispatch_phase))
        raise middleware.EgressDispatchDenied('UNSUPPORTED_TRANSPORT')
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [enforce])
    with pytest.raises(middleware.EgressDispatchDenied, match='UNSUPPORTED_TRANSPORT'):
        pipe.call('never send')
    pipe._native.call.assert_not_called()
    assert observed[0][1] == 'unsupported_transport'
    assert observed[0][0]['qualification_error'] == 'UNSUPPORTED_TRANSPORT'


@pytest.mark.parametrize('structured',[False,True])
def test_ri_cannot_admit_a_transport_lease(monkeypatch,structured):
    from tests.ares_runtime.test_transport_lease import lease
    pipe=object.__new__(RiPipeline)
    pipe._native=SimpleNamespace(call=Mock(return_value='no'),call_structured=Mock(return_value='no'))
    settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    with pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        if structured:
            pipe.call_structured('never send',{'type':'object'})
        else:
            pipe.call('never send')
    pipe._native.call.assert_not_called();pipe._native.call_structured.assert_not_called()
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'
