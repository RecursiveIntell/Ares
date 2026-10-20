"""Final-body metadata must be available before authorization, without scope drift."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_http_client, bind_inference_client
from hermes_cli import middleware
from tests.ares_runtime.test_physical_http_transport import endpoint, client  # noqa: F401


@contextmanager
def local_server(*, redirect=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def answer(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            received.append(('GET', self.path, b''))
            self.answer({'object':'list','data':[{'id':'local','object':'model','created':0,'owned_by':'fixture'}]})
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            received.append(('POST', self.path, body))
            if redirect:
                self.send_response(307)
                self.send_header('Location', redirect)
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self.answer({'id':'local','object':'chat.completion','model':'local',
                             'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('registered', [False, True], ids=['off','enforcing-inference-governor'])
def test_bound_sdk_metadata_is_not_a_model_request(registered, monkeypatch):
    callbacks = []
    def deny_inference(**kwargs):
        callbacks.append(kwargs)
        raise middleware.EgressDispatchDenied('INFERENCE_DISABLED')
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [deny_inference] if registered else [])
    with local_server() as (url, received):
        with client(url) as sdk:
            result = sdk.models.list()
            assert result.data[0].id == 'local'
    assert len(received) == 1 and received[0][0] == 'GET'
    assert callbacks == []


def test_exact_output_and_effective_route_precede_governor(endpoint, monkeypatch):
    url, bodies, _ = endpoint
    observed = []
    def govern(*, payload, dispatch_phase, **kwargs):
        assert dispatch_phase == 'physical_send'
        assert payload['output_token_ceiling'] == 7
        assert payload['output_bound_error'] is None
        route = payload['resolved_route']
        assert route['schema'] == 'ares.resolved-dispatch-route/v1'
        assert route['provider_identity'] == 'openai_compatible:' + url + '/chat/completions'
        assert route['model_ref'] == 'local'
        assert route['api_mode'] == 'chat_completions'
        observed.append(payload)
        return SimpleNamespace(settle=lambda **kw: None)
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    with client(url) as sdk:
        sdk.chat.completions.create(model='local', messages=[], max_tokens=7)
    assert len(bodies) == len(observed) == 1


def test_cross_origin_redirect_cannot_keep_the_original_route_identity(monkeypatch):
    observations = []
    with local_server() as (target, target_received):
        with local_server(redirect=target + '/chat/completions') as (origin, origin_received):
            allowed = 'openai_compatible:' + origin + '/chat/completions'
            def enforce(*, payload, **kwargs):
                observations.append(payload)
                if payload['resolved_route']['provider_identity'] != allowed:
                    raise middleware.EgressDispatchDenied('ROUTE_NOT_ALLOWED')
                return SimpleNamespace(settle=lambda **kw: None)
            monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [enforce])
            sdk = bind_inference_client(openai.OpenAI(api_key='local', base_url=origin,
                max_retries=2, http_client=httpx.Client(trust_env=False, follow_redirects=True)))
            try:
                with pytest.raises(middleware.EgressDispatchDenied) as caught:
                    sdk.chat.completions.create(model='local', messages=[], max_tokens=7)
                assert caught.value.code == 'ROUTE_NOT_ALLOWED', {
                    'code': caught.value.code, 'cause': repr(caught.value.__cause__),
                    'observations': observations,
                }
            finally:
                sdk.close()
    assert len(origin_received) == 1
    assert target_received == []
    assert len(observations) == 2
    assert observations[0]['dispatch_intent_ref'] != observations[1]['dispatch_intent_ref']


@pytest.mark.parametrize('observing', [False, True], ids=['off','observe'])
def test_unbuffered_requests_are_not_blocked_before_observe_can_record_them(monkeypatch, observing):
    calls, observations = [], []
    def transport(request):
        calls.append(request.read())
        return httpx.Response(200, json={'ok':True})
    def observe(*, payload, **kw):
        observations.append(payload)
        assert payload['qualification_error'] == 'UNSUPPORTED_TRANSPORT'
        assert payload['provider_request_digest'] is None
        return None
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [observe] if observing else [])
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
        result = sdk.post('https://fixture.invalid/v1/chat/completions', content=iter([b'opaque']))
        assert result.status_code == 200
    assert calls == [b'opaque']
    assert len(observations) == (1 if observing else 0)
