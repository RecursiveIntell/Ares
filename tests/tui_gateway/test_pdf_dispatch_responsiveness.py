"""PDF conversion must leave controls responsive without late publication.

Real gateway dispatch/RPC/staging paths with neutral generated media and an
explicit held converter boundary. No provider, real approval, clipboard or live
state. Existing real Poppler tests cover the actual converter success path.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.transport import current_transport
from tests.tui_gateway.test_attachment_staging_integrity import image_bytes, pdf_bytes
from tests.tui_gateway.test_attach_does_not_wait_for_agent import building_session


class CaptureTransport:
    def __init__(self):
        self.frames = []
        self.done = threading.Event()
        self.closed = False
    def write(self, frame):
        self.frames.append(frame)
        self.done.set()
        return True


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    monkeypatch.setattr(server, '_tts_stream_stop', lambda *a, **k: None)
    home = tmp_path/'home'
    home.mkdir()
    session = building_session(home, 's')
    session['agent_ready'].set()
    session['_queued_prompt_generation'] = 0
    transport = CaptureTransport()
    session['transport'] = transport
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(server, '_pool', pool)
        yield home, session, transport


def request(rid='pdf'):
    return {'id': rid, 'method': 'pdf.attach', 'params': {'session_id': 's', 'filename': 'neutral.pdf',
        'content_base64': base64.b64encode(pdf_bytes()).decode(), 'first_page': 1, 'last_page': 1}}


def renderer(monkeypatch, entered=None, release=None):
    # Only the pdftoppm invocation is doubled. Real staging and response owners
    # run unchanged. Holding output proves control progress before completion.
    import subprocess
    original = subprocess.run
    calls = []
    def render(argv, *a, **kwargs):
        if not isinstance(argv, list) or not argv or Path(argv[0]).name != 'pdftoppm':
            return original(argv, *a, **kwargs)
        calls.append(list(argv))
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(8), 'test must release converter before teardown'
        Path(str(argv[-1])+'-1.png').write_bytes(image_bytes('red'))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', render)
    return calls


def test_slow_pdf_allows_stop_and_denial_on_same_reader(scope, monkeypatch):
    home, session, transport = scope
    entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
    renderer(monkeypatch, entered, release)
    responses, errors = [], []
    def reader():
        try:
            responses.append(server.dispatch(request(), transport))
            responses.append(server.dispatch({'id': 'stop', 'method': 'session.interrupt',
                'params': {'session_id': 's'}}, transport))
            responses.append(server.dispatch({'id': 'deny', 'method': 'approval.respond',
                'params': {'session_id': 's', 'choice': 'deny', 'request_id': 'no-real-approval'}}, transport))
            controlled.set()
        except Exception as exc:
            errors.append(repr(exc))
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(3)
        assert controlled.wait(3), 'PDF blocked the same connection reader before controls'
        assert not errors
        assert responses[0] is None
        assert responses[1]['result']['status'] == 'interrupted'
        # The approval owner returns a resolved-count, not a boolean.
        assert type(responses[2]['result']['resolved']) is int
        assert responses[2]['result']['resolved'] == 0
        assert session['_queued_prompt_generation'] > 0
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert transport.done.wait(3)
    assert transport.frames[0].get('error'), transport.frames
    assert session['attached_images'] == []
    assert not [p for p in (home/'images').rglob('*') if p.is_file()]


@pytest.mark.parametrize('replacement', ['record', 'transport', 'stored_key', 'profile_home', 'Stop'])
def test_pdf_worker_queued_before_owner_change_never_starts_conversion(scope, monkeypatch, replacement):
    home, session, transport = scope
    pending = []
    class HeldPool:
        def submit(self, fn):
            pending.append(fn)
            return SimpleNamespace(add_done_callback=lambda cb: None)
    monkeypatch.setattr(server, '_pool', HeldPool())
    calls = renderer(monkeypatch)
    response = server.dispatch(request(), transport)
    assert response is None, response
    assert len(pending) == 1
    if replacement == 'record':
        server._sessions['s'] = dict(session)
    elif replacement == 'transport':
        session['transport'] = CaptureTransport()
    elif replacement == 'stored_key':
        session['session_key'] = 'replacement'
    elif replacement == 'profile_home':
        session['profile_home'] = str(home/'replacement')
    else:
        server._methods['session.interrupt']('stop', {'session_id': 's'})
    pending.pop()()
    assert calls == []
    assert transport.frames[0].get('error'), transport.frames
    assert session['attached_images'] == []


@pytest.mark.parametrize('replacement', ['record', 'transport', 'stored_key', 'profile_home', 'Stop'])
def test_running_pdf_does_not_publish_into_changed_owner(scope, monkeypatch, replacement):
    home, session, transport = scope
    entered, release = threading.Event(), threading.Event()
    renderer(monkeypatch, entered, release)
    result = []
    # Invoke dispatch on a reader thread even for the initial RED implementation.
    reader = threading.Thread(target=lambda: result.append(server.dispatch(request(), transport)))
    reader.start()
    try:
        assert entered.wait(3)
        if replacement == 'record':
            server._sessions['s'] = dict(session)
        elif replacement == 'transport':
            session['transport'] = CaptureTransport()
        elif replacement == 'stored_key':
            session['session_key'] = 'replacement'
        elif replacement == 'profile_home':
            session['profile_home'] = str(home/'replacement')
        else:
            server._methods['session.interrupt']('stop', {'session_id': 's'})
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive()
    assert transport.done.wait(3)
    assert transport.frames[0].get('error'), transport.frames
    assert session['attached_images'] == []
    assert not [p for p in home.rglob('*.png')]


def test_pool_refusal_does_not_run_pdf_or_leave_work_reserved(scope, monkeypatch):
    _, session, transport = scope
    class RefusingPool:
        def submit(self, fn):
            raise RuntimeError('controlled unavailable worker')
    monkeypatch.setattr(server, '_pool', RefusingPool())
    calls = renderer(monkeypatch)
    response = server.dispatch(request(), transport)
    assert response.get('error'), response
    assert calls == []
    assert not session.get('_pdf_attachment_pending')


def test_second_pdf_is_refused_while_original_worker_is_owned(scope, monkeypatch):
    _, session, transport = scope
    entered, release = threading.Event(), threading.Event()
    calls = renderer(monkeypatch, entered, release)
    result = []
    reader = threading.Thread(target=lambda: result.append(server.dispatch(request(), transport)))
    reader.start()
    try:
        assert entered.wait(3)
        second = server.dispatch(request('second'), transport)
        assert second.get('error'), second
        assert len(calls) == 1
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive()
    assert transport.done.wait(3)
    assert transport.frames[0]['result']['attached'] is True
    assert not session.get('_pdf_attachment_pending')


def test_pdf_worker_expired_in_queue_never_calls_converter(scope, monkeypatch):
    _, session, transport = scope
    pending = []
    now = [100.0]
    class HeldPool:
        def submit(self, fn):
            pending.append(fn)
            return SimpleNamespace(add_done_callback=lambda cb: None)
    monkeypatch.setattr(server, '_pool', HeldPool())
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    calls = renderer(monkeypatch)
    assert server.dispatch(request(), transport) is None
    assert session.get('_pdf_attachment_pending') is not None
    now[0] = 126.0
    pending.pop()()
    assert calls == []
    assert transport.frames[0]['error']['code'] == 4009
    assert not session.get('_pdf_attachment_pending')


def test_foreign_transport_cannot_reserve_or_convert_pdf(scope, monkeypatch):
    _, session, _ = scope
    calls = renderer(monkeypatch)
    response = server.dispatch(request(), CaptureTransport())
    assert response['error']['code'] == 4001
    assert calls == []
    assert not session.get('_pdf_attachment_pending')


def test_pdf_converter_exception_releases_only_its_worker_reservation(scope, monkeypatch):
    _, session, transport = scope
    import subprocess
    original = subprocess.run
    def failing_converter(argv, *args, **kwargs):
        if isinstance(argv, list) and argv and Path(argv[0]).name == 'pdftoppm':
            raise OSError('controlled converter failure')
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', failing_converter)
    assert server.dispatch(request(), transport) is None
    assert transport.done.wait(3)
    assert transport.frames[0]['error']
    assert not session.get('_pdf_attachment_pending')
    assert session['attached_images'] == []


def test_successive_pdf_workers_keep_transport_and_context_isolated(scope, monkeypatch):
    _, session, transport = scope
    calls = renderer(monkeypatch)
    assert server.dispatch(request('first'), transport) is None
    assert transport.done.wait(3)
    assert transport.frames[0]['result']['attached'] is True
    first_paths = list(session['attached_images'])
    transport.done.clear()
    assert server.dispatch(request('next'), transport) is None
    assert transport.done.wait(3)
    assert transport.frames[1]['result']['attached'] is True
    assert len(calls) == 2
    assert len(session['attached_images']) == 2
    assert session['attached_images'][0] == first_paths[0]
    assert session['attached_images'][0] != session['attached_images'][1]
    assert not session.get('_pdf_attachment_pending')
