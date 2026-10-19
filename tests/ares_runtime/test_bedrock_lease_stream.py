"""Genuine botocore EventStream frames, CRCs and parser on localhost."""
import gc
import json
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest

boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
from botocore import UNSIGNED  # noqa: E402
from botocore.config import Config  # noqa: E402

from ares_runtime.physical_dispatch import bind_bedrock_client
from hermes_cli import middleware
from tests.ares_runtime.test_transport_deadline import govern


def frame(event,payload):
    headers=b''
    for key,value in [(':message-type','event'),(':event-type',event),(':content-type','application/json')]:
        key,value=key.encode(),value.encode()
        headers+=bytes([len(key)])+key+b'\x07'+struct.pack('!H',len(value))+value
    data=json.dumps(payload,separators=(',',':')).encode()
    prelude=struct.pack('!II',16+len(headers)+len(data),len(headers))
    message=prelude+struct.pack('!I',zlib.crc32(prelude))+headers+data
    return message+struct.pack('!I',zlib.crc32(message))


@pytest.fixture
def aws_stream():
    bodies=[];mode=['complete']
    data=frame('messageStart',{'role':'assistant'})+frame('messageStop',{'stopReason':'end_turn'})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers['content-length'])))
            self.send_response(200);self.send_header('Content-Type','application/vnd.amazon.eventstream')
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            try:
                if mode[0]=='drip':
                    for byte in data:
                        self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.015)
                else:
                    self.wfile.write(data);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):
                pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'http://127.0.0.1:{server.server_port}',bodies,mode,data
    server.shutdown();server.server_close();thread.join()


def sdk(url):
    return bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
        aws_access_key_id='local',aws_secret_access_key='local',
        config=Config(signature_version=UNSIGNED,retries={'max_attempts':2},proxies={})))


def test_real_bedrock_stream_exact_identity_and_terminal(aws_stream,monkeypatch):
    import hashlib
    url,bodies,_,data=aws_stream
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [])
    client=sdk(url)
    try:
        expected=list(client.converse_stream(modelId='local',messages=[],inferenceConfig={'maxTokens':7})['stream'])
        outcomes=govern(monkeypatch)
        actual=list(client.converse_stream(modelId='local',messages=[],inferenceConfig={'maxTokens':7})['stream'])
    finally:
        client.close()
    assert actual==expected==[{'messageStart':{'role':'assistant'}},{'messageStop':{'stopReason':'end_turn'}}]
    assert bodies[0]==bodies[1]
    assert len(outcomes)==1 and outcomes[0]['outcome']=='ok'
    assert outcomes[0]['response']['response_bytes']==len(data)
    assert outcomes[0]['response']['response_digest']=='sha256:'+hashlib.sha256(data).hexdigest()


def test_real_bedrock_stream_slow_drip_deadline_no_retry(aws_stream,monkeypatch):
    url,bodies,mode,_=aws_stream;mode[0]='drip';outcomes=govern(monkeypatch,send_timeout_ms=180)
    client=sdk(url)
    try:
        started=time.monotonic()
        result=client.converse_stream(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            list(result['stream'])
        result['stream'].close()
    finally:
        client.close()
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    assert time.monotonic()-started<1
    assert len(bodies)==len(outcomes)==1 and outcomes[0]['outcome']!='ok'


def test_real_bedrock_unconsumed_stream_gc_settles(aws_stream,monkeypatch):
    url,bodies,_,_=aws_stream;outcomes=govern(monkeypatch)
    client=sdk(url)
    try:
        result=client.converse_stream(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
        del result
        gc.collect()
        assert len(outcomes)==1 and outcomes[0]['outcome']=='abandoned'
    finally:
        client.close()
    assert len(bodies)==1
