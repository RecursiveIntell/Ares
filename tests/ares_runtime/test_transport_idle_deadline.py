"""Original total deadline closes the socket even while consumer is idle."""
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import httpx
import pytest

from ares_runtime.physical_dispatch import bind_http_client
from tests.ares_runtime.test_transport_deadline import govern


@pytest.fixture
def waiting_peer():
    disconnected=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(b'data: first\n\n');self.wfile.flush()
            self.connection.settimeout(2)
            try:
                if self.connection.recv(1)==b'':
                    disconnected.set()
            except OSError:
                disconnected.set()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/chat/completions',disconnected
    server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('asynchronous',[False,True])
def test_deadline_stops_socket_without_consumer_read(waiting_peer,monkeypatch,asynchronous):
    url,disconnected=waiting_peer;outcomes=govern(monkeypatch,send_timeout_ms=160)
    async def run():
        async with bind_http_client(httpx.AsyncClient(trust_env=False)) as client:
            response=await client.send(client.build_request('POST',url,json={'max_tokens':7}),stream=True)
            try:
                deadline=time.monotonic()+.7
                while not disconnected.is_set() and time.monotonic()<deadline:
                    await asyncio.sleep(.01)
                assert disconnected.is_set()
            finally:
                await response.aclose()
    if asynchronous:
        asyncio.run(run())
    else:
        with bind_http_client(httpx.Client(trust_env=False)) as client:
            response=client.send(client.build_request('POST',url,json={'max_tokens':7}),stream=True)
            try:
                assert disconnected.wait(.7)
            finally:
                response.close()
    assert len(outcomes)==1 and outcomes[0]['outcome']=='abandoned'
