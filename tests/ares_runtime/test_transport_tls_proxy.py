"""Actual TLS, authenticated CONNECT proxy and provider auth remain intact."""
import asyncio
import base64
import json
import select
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_inference_client,bind_bedrock_client
from hermes_cli import middleware
from tests.ares_runtime.test_transport_deadline import govern


@pytest.fixture
def tls_endpoint(tmp_path):
    cert,key=tmp_path/'cert.pem',tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
        '-keyout',str(key),'-out',str(cert)],capture_output=True,check=True,timeout=15)
    seen=[];modes=['normal']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['content-length']))
            seen.append((self.path,dict(self.headers),body))
            data=json.dumps({'id':'x','choices':[{'index':0,'message':{'role':'assistant','content':'ok'}}],
                'output':{'message':{'role':'assistant','content':[{'text':'ok'}]}},'stopReason':'end_turn','usage':{},'metrics':{}}).encode()
            try:
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers()
                if modes[0]=='drip':
                    for byte in data:
                        self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.025)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError,ssl.SSLError):
                pass
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'https://localhost:{server.server_port}/v1',cert,seen,modes
    server.shutdown();server.server_close();thread.join()


@pytest.fixture
def connect_proxy(tls_endpoint):
    target_port=int(tls_endpoint[0].split(':')[2].split('/')[0]);seen=[]
    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            line=self.rfile.readline().decode().strip();headers={}
            while True:
                item=self.rfile.readline()
                if item in (b'\r\n',b'\n',b''):
                    break
                k,v=item.decode().split(':',1);headers[k.lower()]=v.strip()
            seen.append((line,headers))
            assert line.startswith('CONNECT localhost:')
            with socket.create_connection(('127.0.0.1',target_port),timeout=2) as remote:
                self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n');self.wfile.flush()
                while True:
                    ready,_,_=select.select([self.connection,remote],[],[],2)
                    if not ready:
                        return
                    for source in ready:
                        data=source.recv(65536)
                        if not data:
                            return
                        (remote if source is self.connection else self.connection).sendall(data)
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        daemon_threads=True
    server=Server(('127.0.0.1',0),Proxy);thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'http://proxy-user:proxy-pass@127.0.0.1:{server.server_address[1]}',seen
    server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('asynchronous',[False,True])
@pytest.mark.parametrize('proxy',[False,True])
def test_sdk_tls_proxy_auth_and_exact_body(tls_endpoint,connect_proxy,monkeypatch,asynchronous,proxy):
    url,cert,seen,_=tls_endpoint;proxy_url,proxy_seen=connect_proxy
    context=ssl.create_default_context(cafile=str(cert));outcomes=govern(monkeypatch)
    async def run():
        async with bind_inference_client(openai.AsyncOpenAI(api_key='local-test-auth',base_url=url,max_retries=0,
                http_client=httpx.AsyncClient(verify=context,proxy=proxy_url if proxy else None,trust_env=False))) as sdk:
            return await sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    if asynchronous:
        result=asyncio.run(run())
    else:
        with bind_inference_client(openai.OpenAI(api_key='local-test-auth',base_url=url,max_retries=0,
                http_client=httpx.Client(verify=context,proxy=proxy_url if proxy else None,trust_env=False))) as sdk:
            result=sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    assert result.choices[0].message.content=='ok'
    assert len(seen)==len(outcomes)==1
    assert seen[0][1]['Authorization']=='Bearer local-test-auth'
    assert json.loads(seen[0][2])=={'model':'x','messages':[],'max_tokens':7}
    if proxy:
        assert len(proxy_seen)==1
        assert proxy_seen[0][1]['proxy-authorization']=='Basic '+base64.b64encode(b'proxy-user:proxy-pass').decode()
    else:
        assert not proxy_seen


@pytest.mark.parametrize('proxy',[False,True])
def test_bedrock_prepared_signature_tls_proxy_preserved(tls_endpoint,connect_proxy,monkeypatch,proxy):
    import boto3
    from botocore.config import Config
    url,cert,seen,_=tls_endpoint;proxy_url,proxy_seen=connect_proxy
    outcomes=govern(monkeypatch)
    client=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
        aws_access_key_id='fixture-access',aws_secret_access_key='fixture-secret',verify=str(cert),
        config=Config(retries={'max_attempts':0},proxies={'https':proxy_url} if proxy else {})))
    try:
        client.converse(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
    finally:
        client.close()
    assert len(seen)==len(outcomes)==1 and outcomes[0]['outcome']=='ok'
    assert seen[0][1]['Authorization'].startswith('AWS4-HMAC-SHA256 Credential=fixture-access/')
    assert 'SignedHeaders=' in seen[0][1]['Authorization']
    if proxy:
        assert len(proxy_seen)==1 and 'proxy-authorization' in proxy_seen[0][1]


@pytest.mark.parametrize('asynchronous',[False,True])
def test_tls_slow_drip_has_total_not_idle_deadline(tls_endpoint,monkeypatch,asynchronous):
    url,cert,seen,modes=tls_endpoint;modes[0]='drip';outcomes=govern(monkeypatch,send_timeout_ms=180)
    context=ssl.create_default_context(cafile=str(cert))
    async def run():
        async with bind_inference_client(openai.AsyncOpenAI(api_key='local',base_url=url,max_retries=2,
                http_client=httpx.AsyncClient(verify=context,trust_env=False))) as sdk:
            await sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    started=time.monotonic()
    with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
        if asynchronous:
            asyncio.run(run())
        else:
            with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
                    http_client=httpx.Client(verify=context,trust_env=False))) as sdk:
                sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    assert time.monotonic()-started<1
    assert len(seen)==len(outcomes)==1
