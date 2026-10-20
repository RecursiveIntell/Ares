"""Real loopback HTTP + installed SDK; no external provider calls."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_inference_client, inference_call_scope
from hermes_cli import middleware


@pytest.fixture
def endpoint():
    bodies = []
    statuses = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['content-length']))
            bodies.append(body)
            status = statuses.pop(0) if statuses else 200
            data = json.dumps({'id': 'local', 'object': 'chat.completion', 'model': 'local',
                               'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'text', 'text': 'ok'}],
                               'output': {'message': {'role': 'assistant', 'content': [{'text': 'ok'}]}},
                               'stopReason': 'end_turn', 'usage': {'inputTokens': 1, 'outputTokens': 1, 'totalTokens': 2,
                                                                'input_tokens': 1, 'output_tokens': 1},
                               'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}],
                               'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', bodies, statuses
    server.shutdown()
    server.server_close()
    thread.join()


def client(url, retries=0):
    return bind_inference_client(openai.OpenAI(api_key='local-disposable-test-key', base_url=url,
                                             max_retries=retries, http_client=httpx.Client(trust_env=False)))


def invoke(c):
    with inference_call_scope(None, {'model': 'local', 'max_tokens': 7}, purpose='conversation'):
        return c.chat.completions.create(model='local', messages=[{'role': 'user', 'content': 'hello'}], max_tokens=7)


def test_exact_body_identity_and_fresh_retry_intents(endpoint, monkeypatch):
    from blake3 import blake3
    url, bodies, statuses = endpoint
    observations, outcomes = [], []
    def govern(*, payload, dispatch_phase, **kw):
        assert dispatch_phase == 'physical_send'
        observations.append(payload)
        class Ticket:
            def settle(self, **result):
                outcomes.append(result)
        return Ticket()
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [])
    with client(url) as c:
        invoke(c)
    original = bodies[0]
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    statuses.extend([500, 200])
    with client(url, retries=1) as c:
        invoke(c)
    assert bodies == [original] * 3
    assert len(observations) == len(outcomes) == 2
    assert len({o['dispatch_intent_ref'] for o in observations}) == 2
    for observation in observations:
        assert observation['provider_request_digest'] == blake3(original).hexdigest()
        assert observation['provider_request_bytes'] == len(original)
        assert observation['qualification_error'] == 'SESSION_IDENTITY_UNAVAILABLE'
    assert all(o['response']['schema'] == 'ares.transport-response-body/v1' for o in outcomes)


@pytest.mark.parametrize('failure', ['deny', 'settle'])
def test_typed_error_survives_sdk_without_retry(endpoint, monkeypatch, failure):
    url, bodies, _ = endpoint
    error = (middleware.EgressDispatchDenied('LOCAL_TEST_DENIED') if failure == 'deny'
             else middleware.EgressOutcomeUnrecorded())
    def govern(**kw):
        if failure == 'deny':
            raise error
        class Ticket:
            def settle(self, **kw):
                raise error
        return Ticket()
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    with client(url, retries=2) as c, pytest.raises(type(error)) as caught:
        invoke(c)
    assert caught.value is error
    assert len(bodies) == (0 if failure == 'deny' else 1)


def test_no_session_enforce_has_no_send(endpoint, monkeypatch):
    url, bodies, _ = endpoint
    def enforce(*, payload, **kw):
        if payload['qualification_error']:
            raise middleware.EgressDispatchDenied(payload['qualification_error'])
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [enforce])
    with client(url) as c, pytest.raises(middleware.EgressDispatchDenied, match='SESSION_IDENTITY_UNAVAILABLE'):
        invoke(c)
    assert bodies == []
