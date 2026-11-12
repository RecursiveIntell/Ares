"""Real pinned SDK completion differs from consuming the entire HTTP body."""
import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_inference_client
from hermes_cli import middleware


@pytest.fixture(params=['length', 'chunked', 'eof', 'stalled_tail', 'invalid_json'])
def completed_endpoint(request):
    mode = request.param
    release = threading.Event()
    chunks = [
        {'choices': [{'index': 0, 'delta': {'content': 'ok'}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
    ]
    data = b''.join(('data: ' + json.dumps({'id': 'local', 'object': 'chat.completion.chunk',
                                          'model': 'local', **chunk}) + '\n\n').encode() for chunk in chunks)
    data += b'data: {broken}\n\n' if mode == 'invalid_json' else b'data: [DONE]\n\n'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            chunked = mode in {'chunked', 'stalled_tail'}
            if chunked:
                self.send_header('Transfer-Encoding', 'chunked')
            elif mode in {'length', 'invalid_json'}:
                self.send_header('Content-Length', str(len(data)))
            else:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write((f'{len(data):x}\r\n'.encode() + data + b'\r\n') if chunked else data)
                self.wfile.flush()
                if mode == 'stalled_tail':
                    release.wait(15)
                if chunked:
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', mode, data, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def outcomes(monkeypatch):
    settled = []
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [
        lambda **kw: SimpleNamespace(settle=lambda **result: settled.append(result))
    ])
    return settled


def assert_completion(outcomes, data):
    assert len(outcomes) == 1
    assert outcomes[0]['outcome'] == 'ok'
    response = outcomes[0]['response']
    assert response['response_complete'] is False
    assert response['completion_evidence'] == 'native_openai_sdk_stream_exhausted'
    assert response['response_digest'] is None
    count = response['response_bytes']
    assert 0 < count <= len(data)
    assert response['observed_prefix_digest'] == 'sha256:' + hashlib.sha256(data[:count]).hexdigest()


@pytest.mark.parametrize('consume', ['list', 'next', 'context'])
def test_native_sync_sdk_completion_without_body_eof(completed_endpoint, outcomes, consume):
    url, mode, data, release = completed_endpoint
    with bind_inference_client(openai.OpenAI(api_key='local', base_url=url, max_retries=0,
                                             http_client=httpx.Client(trust_env=False))) as sdk:
        stream = sdk.chat.completions.create(model='local', messages=[], max_tokens=7, stream=True)
        assert isinstance(stream, openai.Stream)
        if mode == 'invalid_json':
            with pytest.raises(ValueError):
                list(stream)
            assert outcomes[0]['outcome'] == 'abandoned'
            assert 'completion_evidence' not in outcomes[0]['response']
            return
        if consume == 'list':
            assert len(list(stream)) == 2
        elif consume == 'next':
            assert next(stream).choices
            assert next(stream).choices
            with pytest.raises(StopIteration):
                next(stream)
        else:
            with stream as entered:
                assert entered is stream
                assert len(list(entered)) == 2
        assert not release.is_set()  # The stalled HTTP tail was never drained.
        assert_completion(outcomes, data)


@pytest.mark.asyncio
@pytest.mark.parametrize('consume', ['for', 'next', 'context'])
async def test_native_async_sdk_completion_without_body_eof(completed_endpoint, outcomes, consume):
    url, mode, data, release = completed_endpoint
    async with bind_inference_client(openai.AsyncOpenAI(api_key='local', base_url=url, max_retries=0,
                                                        http_client=httpx.AsyncClient(trust_env=False))) as sdk:
        stream = await sdk.chat.completions.create(model='local', messages=[], max_tokens=7, stream=True)
        assert isinstance(stream, openai.AsyncStream)
        if mode == 'invalid_json':
            with pytest.raises(ValueError):
                [chunk async for chunk in stream]
            assert outcomes[0]['outcome'] == 'abandoned'
            assert 'completion_evidence' not in outcomes[0]['response']
            return
        if consume == 'for':
            assert len([chunk async for chunk in stream]) == 2
        elif consume == 'next':
            assert (await stream.__anext__()).choices
            assert (await stream.__anext__()).choices
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
        else:
            async with stream as entered:
                assert entered is stream
                assert len([chunk async for chunk in entered]) == 2
        await asyncio.sleep(0)
        assert not release.is_set()
        assert_completion(outcomes, data)
