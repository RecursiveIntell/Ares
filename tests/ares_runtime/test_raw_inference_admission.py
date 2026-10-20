"""Known raw inference routes must expose precise unsupported admission."""
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_cli import middleware
from tests.ares_runtime.test_transport_lease import lease
from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401


@pytest.fixture
def bounded(monkeypatch):
    settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    return settles


def test_zai_setup_probe_is_not_an_unowned_inference_bypass(endpoint,bounded):
    from hermes_cli.auth import _probe_single_zai_endpoint
    url,bodies,_=endpoint
    with pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        _probe_single_zai_endpoint('local',('fixture',url,['local'],'fixture'),1)
    assert not bodies
    assert bounded[0]['outcome']=='denied_by_peer'


def test_urlopen_model_probe_refuses_before_unqualified_transport(bounded,monkeypatch):
    from hermes_cli.models import probe_opencode_free_execution
    native=Mock(return_value=io.BytesIO(b'{"choices":[{}]}'))
    monkeypatch.setattr('urllib.request.urlopen',native)
    with pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        probe_opencode_free_execution('local')
    native.assert_not_called()


def test_requests_x_search_refuses_before_unqualified_transport(bounded,monkeypatch):
    from tools import x_search_tool as module
    monkeypatch.setattr(module,'_resolve_xai_bearer',lambda:('local','http://local.invalid','fixture'))
    monkeypatch.setattr(module,'_get_x_search_model',lambda:'local')
    monkeypatch.setattr(module,'_get_x_search_reasoning_effort',lambda:'')
    native=Mock(return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{}))
    monkeypatch.setattr(module.requests,'post',native)
    with pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        module.x_search_tool('query')
    native.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_provider_proxy_is_not_a_physical_admission_escape(endpoint,bounded):
    from aiohttp.test_utils import TestClient,TestServer
    from hermes_cli.proxy.server import create_app
    url,bodies,_=endpoint
    adapter=SimpleNamespace(allowed_paths={'/chat/completions'},display_name='fixture',
        get_credential=lambda:SimpleNamespace(base_url=url,token_type='Bearer',bearer='fixture'))
    async with TestClient(TestServer(create_app(adapter))) as client:
        response=await client.post('/v1/chat/completions',json={'model':'local','max_tokens':7})
        assert response.status==403
        assert (await response.json())['error']['code']=='UNSUPPORTED_TRANSPORT'
    assert not bodies
