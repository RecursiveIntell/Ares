"""Real WebSocket loss/resume/replay; disposable DB, no agent or provider."""
import json
import threading
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.ws import WSTransport, handle_ws


@pytest.fixture
def peer(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('stored', source='desktop')
    monkeypatch.setattr(server, '_db', db)
    monkeypatch.setattr(server, '_profile_home', lambda _p: None)
    monkeypatch.setattr(server, '_pending', {})
    monkeypatch.setattr(server, '_pending_prompt_payloads', {})
    monkeypatch.setattr(server, '_clarify_request_owners', {})
    monkeypatch.setattr(server, '_clarify_response_receipts', {})
    monkeypatch.setattr(server, '_answers', {})
    monkeypatch.setattr(server, '_batch_clarify', {})
    monkeypatch.setattr(server, '_sessions', {'runtime': {
        'session_key': 'stored', 'session_id': 'runtime', 'source': 'desktop',
        'cwd': str(tmp_path), 'history': [], 'history_lock': threading.Lock(),
        'running': True, 'transport': server._stdio_transport, 'agent': None,
        'created_at': time.time(),
    }})
    monkeypatch.setattr(server, '_ensure_skin_watcher', lambda: None)
    monkeypatch.setattr(server, '_schedule_startup_orphan_sweep', lambda: None)
    # Leave actual detach and resume/rebind enabled; suppress only a delayed
    # production orphan timer (there is no agent in this disposable session).
    monkeypatch.setattr(server, '_schedule_ws_orphan_reap', lambda *_a: None)
    monkeypatch.setattr(server, 'resolve_skin', lambda: {})
    app = FastAPI()

    @app.websocket('/ws')
    async def socket(ws: WebSocket):
        await handle_ws(ws)

    yield app
    server._clear_pending()
    db.close()


def rpc(ws, rid, method, **params):
    ws.send_json({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    for _ in range(100):
        frame = json.loads(ws.receive_text())
        if frame.get('id') == rid:
            return frame
    raise AssertionError('missing RPC acknowledgement')


@pytest.mark.parametrize('batch', [False, True])
def test_lost_ack_reconnect_requires_resume_and_only_explicit_retry(peer, monkeypatch, batch):
    dropped = threading.Event()
    calls = []
    original_write = WSTransport.write_async
    original_handler = server._methods['clarify.respond']

    async def drop_ack(self, frame):
        if frame.get('id') == 'lost':
            dropped.set()
            return True  # Accepted answer, intentionally lost acknowledgement.
        return await original_write(self, frame)

    def record_reply(rid, params):
        calls.append(dict(params))
        return original_handler(rid, params)

    monkeypatch.setattr(WSTransport, 'write_async', drop_ack)
    monkeypatch.setitem(server._methods, 'clarify.respond', record_reply)
    outcomes = []
    payload = {'question': 'Target?'}
    if batch:
        payload = {'questions': [{'qid': 'q0', 'question': 'Target?'}, {'qid': 'q1', 'question': 'Name?'}]}
    worker = threading.Thread(target=lambda: outcomes.append(server._block(
        'clarify.request', 'runtime', payload, timeout=10,
        batch_qids=['q0', 'q1'] if batch else None,
    )), daemon=True)
    try:
        with TestClient(peer) as client:
            with client.websocket_connect('/ws') as first:
                resumed = rpc(first, 'resume-first', 'session.resume', session_id='stored',
                              defer_history=True, omit_messages=True, source='desktop')
                assert resumed['result']['session_id'] == 'runtime'
                worker.start()
                event = json.loads(first.receive_text())
                assert event['params']['type'] == 'clarify.request'
                request_id = event['params']['payload']['request_id']
                if batch:
                    assert rpc(first, 'lock0', 'clarify.respond', session_id='runtime',
                               request_id=request_id, question_id='q0', answer='staging')['result']['status'] == 'ok'
                answer = 'packet' if batch else 'staging'
                params = {'session_id': 'runtime', 'request_id': request_id, 'answer': answer}
                if batch:
                    params['question_id'] = 'q1'
                first.send_json({'jsonrpc': '2.0', 'id': 'lost', 'method': 'clarify.respond', 'params': params})
                assert dropped.wait(5)
                worker.join(5)
                assert not worker.is_alive()
                assert outcomes == [json.dumps({'answers': {'q0': 'staging', 'q1': 'packet'}}) if batch else 'staging']
                assert rpc(first, 'barrier', 'gateway.ping')['result']['ok']
                assert len(calls) == (2 if batch else 1)
            with client.websocket_connect('/ws') as reconnected:
                assert rpc(reconnected, 'unattached', 'clarify.respond', **params)['error']['code'] == 4030
                resumed = rpc(reconnected, 'resume-second', 'session.resume', session_id='stored',
                              defer_history=True, omit_messages=True, source='desktop')
                assert resumed['result']['session_id'] == 'runtime'
                assert 'pending_clarify' not in resumed['result']
                assert len(calls) == (3 if batch else 2)  # Resume caused no answer resend.
                assert rpc(reconnected, 'explicit-retry', 'clarify.respond', **params)['result']['status'] == 'ok'
                params['answer'] = 'changed'
                assert rpc(reconnected, 'changed', 'clarify.respond', **params)['result']['status'] == 'conflict'
                assert len(outcomes) == 1
    finally:
        server._clear_pending()
        if worker.ident is not None:
            worker.join(5)
