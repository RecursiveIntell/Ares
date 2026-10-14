"""Staging occurrences must not overwrite one another before input custody.

Neutral fixtures, actual RPC/helper and filesystem paths; no real clipboard,
provider, application restart or live profile state. Staging is not input ACK.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as RealDatetime
import io
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from tui_gateway import server
from tests.tui_gateway.test_attach_does_not_wait_for_agent import building_session


def image_bytes(color, fmt='PNG'):
    output = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(output, format=fmt)
    return output.getvalue()


def data_url(data, mime):
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode()


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
    # Deliberately put every producer in the same timestamp/counter namespace.
    monkeypatch.setattr(server, 'datetime', SimpleNamespace(now=lambda: RealDatetime(2026, 1, 1, 1, 1, 1)))
    home = tmp_path/'profile'
    home.mkdir()
    sessions = {name: building_session(home, name) for name in ('a', 'b')}
    return home, sessions


def rpc(method, sid, **params):
    return server._methods[method]('fixture', {'session_id': sid, **params})


def upload(sid, payload, filename='neutral.png'):
    response = rpc('image.attach_bytes', sid,
        content_base64=base64.b64encode(payload).decode(), filename=filename)
    assert 'error' not in response, response
    return Path(response['result']['path'])


def test_two_sessions_same_timestamp_and_counter_keep_distinct_image_bytes(staging):
    home, sessions = staging
    first = upload('a', image_bytes('red'))
    original = first.read_bytes()
    second = upload('b', image_bytes('blue'))
    assert first != second
    assert first.read_bytes() == original == image_bytes('red')
    assert second.read_bytes() == image_bytes('blue')
    assert sessions['a']['attached_images'] == [str(first)]
    assert sessions['b']['attached_images'] == [str(second)]
    assert first.is_relative_to(home/'images') and second.is_relative_to(home/'images')
    assert not sessions['a']['agent_ready'].is_set()


def test_concurrent_image_helpers_do_not_share_staging_occurrences(staging):
    _, sessions = staging
    start = threading.Barrier(2)
    def stage(sid, color):
        start.wait(timeout=5)
        return server._queue_attached_image(sessions[sid], image_bytes(color), '.png', prefix='upload')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(stage, sid, color) for sid, color in (('a', 'red'), ('b', 'blue'))]
        paths = [job.result(timeout=10) for job in jobs]
    assert paths[0] != paths[1]
    assert paths[0].read_bytes() == image_bytes('red')
    assert paths[1].read_bytes() == image_bytes('blue')


def test_concurrent_same_session_images_are_both_queued_once(staging):
    _, sessions = staging
    start = threading.Barrier(2)
    def stage(color):
        start.wait(timeout=5)
        return server._queue_attached_image(sessions['a'], image_bytes(color), '.png', prefix='upload')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(stage, color) for color in ('red', 'blue')]
        paths = [job.result(timeout=10) for job in jobs]
    assert paths[0] != paths[1]
    assert set(sessions['a']['attached_images']) == {str(path) for path in paths}
    assert len(sessions['a']['attached_images']) == 2
    assert [path.read_bytes() for path in paths] == [image_bytes('red'), image_bytes('blue')]


def test_clipboard_sessions_do_not_overwrite_previous_staged_image(staging, monkeypatch):
    _, sessions = staging
    def save(path):
        path.write_bytes(image_bytes('red' if not sessions['a']['attached_images'] else 'blue'))
        return True
    monkeypatch.setattr('hermes_cli.clipboard.save_clipboard_image', save)
    first_response = rpc('clipboard.paste', 'a')
    assert first_response['result']['attached']
    first = Path(first_response['result']['path'])
    original = first.read_bytes()
    second_response = rpc('clipboard.paste', 'b')
    assert second_response['result']['attached']
    second = Path(second_response['result']['path'])
    assert first != second
    assert first.read_bytes() == original == image_bytes('red')
    assert second.read_bytes() == image_bytes('blue')


def test_clipboard_false_discards_only_this_failed_capture(staging, monkeypatch):
    home, sessions = staging
    kept = upload('a', image_bytes('red'))
    before = {p for p in (home/'images').rglob('*') if p.is_file()}
    def empty(path):
        path.write_bytes(b'partial neutral capture')
        return False
    monkeypatch.setattr('hermes_cli.clipboard.save_clipboard_image', empty)
    monkeypatch.setattr('hermes_cli.clipboard.has_clipboard_image', lambda: False)
    response = rpc('clipboard.paste', 'b')
    assert response['result']['attached'] is False
    assert sessions['b']['attached_images'] == []
    assert {p for p in (home/'images').rglob('*') if p.is_file()} == before
    assert kept.read_bytes() == image_bytes('red')


@pytest.mark.parametrize('fmt,filename', [('PNG', 'neutral.png'), ('JPEG', 'neutral.jpg'),
    ('GIF', 'neutral.gif'), ('WEBP', 'neutral.webp'), ('BMP', 'neutral.bmp')])
def test_image_staging_preserves_supported_original_bytes_and_suffix(staging, fmt, filename):
    _, sessions = staging
    payload = image_bytes('red', fmt)
    path = upload('a', payload, filename)
    assert path.read_bytes() == payload
    assert path.suffix == Path(filename).suffix
    assert sessions['a']['attached_images'] == [str(path)]


def test_file_upload_reservation_is_atomic_across_concurrent_sessions(staging, monkeypatch):
    home, sessions = staging
    (home/'attachments').mkdir()
    original_exists = Path.exists
    barrier = threading.Barrier(2)
    def both_saw_absent(path):
        if path.parent == home/'attachments' and path.name == 'shared.txt':
            observed = original_exists(path)
            barrier.wait(timeout=5)
            return observed
        return original_exists(path)
    monkeypatch.setattr(Path, 'exists', both_saw_absent)
    def stage(sid, payload):
        response = rpc('file.attach', sid, path='', name='shared.txt',
            data_url=data_url(payload, 'text/plain'))
        assert 'error' not in response, response
        return Path(response['result']['path'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(stage, sid, payload) for sid, payload in (('a', b'first'), ('b', b'second'))]
        paths = [job.result(timeout=10) for job in jobs]
    assert paths[0] != paths[1]
    assert paths[0].read_bytes() == b'first'
    assert paths[1].read_bytes() == b'second'
    assert all(path.is_relative_to(home/'attachments') for path in paths)


def test_local_workspace_file_is_not_copied_or_replaced(staging):
    home, _ = staging
    original = home/'notes.txt'
    original.write_bytes(b'original local artifact')
    response = rpc('file.attach', 'a', path=str(original), name='notes.txt',
        data_url=data_url(b'not the selected local source', 'text/plain'))
    assert response['result']['uploaded'] is False
    assert Path(response['result']['path']) == original
    assert original.read_bytes() == b'original local artifact'


def test_external_local_file_is_copied_without_mutating_the_source(staging):
    home, _ = staging
    original = home.parent/'outside.txt'
    original.write_bytes(b'neutral external source')
    response = rpc('file.attach', 'a', path=str(original), name='outside.txt')
    assert 'error' not in response, response
    assert response['result']['uploaded'] is True
    staged = Path(response['result']['path'])
    assert staged != original and staged.is_relative_to(home/'attachments')
    assert staged.read_bytes() == original.read_bytes() == b'neutral external source'


def test_failed_file_write_discards_only_new_partial_bytes(staging, monkeypatch):
    home, _ = staging
    first = rpc('file.attach', 'a', path='', name='kept.txt', data_url=data_url(b'kept', 'text/plain'))
    kept = Path(first['result']['path'])
    before = {p for p in (home/'attachments').rglob('*') if p.is_file()}
    real_open = Path.open
    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, payload):
            self.stream.write(payload[:2])
            raise OSError('controlled partial file write')
    def open_with_fault(path, mode='r', *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        if path.is_relative_to(home/'attachments') and path.name == 'failed.txt' and mode in ('wb', 'xb'):
            return PartialWriter(stream)
        return stream
    monkeypatch.setattr(Path, 'open', open_with_fault)
    response = rpc('file.attach', 'b', path='', name='failed.txt', data_url=data_url(b'partial', 'text/plain'))
    assert response.get('error'), response
    assert kept.read_bytes() == b'kept'
    assert {p for p in (home/'attachments').rglob('*') if p.is_file()} == before


def test_empty_file_remains_a_valid_artifact(staging):
    _, _ = staging
    response = rpc('file.attach', 'a', path='', name='empty.txt', data_url=data_url(b'', 'text/plain'))
    assert 'error' not in response, response
    assert Path(response['result']['path']).read_bytes() == b''


def test_image_staging_keeps_profile_roots_separate(staging):
    home, sessions = staging
    other = home.parent/'other-profile'
    other.mkdir()
    sessions['b']['profile_home'] = str(other)
    first = upload('a', image_bytes('red'))
    second = upload('b', image_bytes('blue'))
    assert first.is_relative_to(home/'images')
    assert second.is_relative_to(other/'images')
    assert first.read_bytes() == image_bytes('red')
    assert second.read_bytes() == image_bytes('blue')


def pdf_bytes():
    output = io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(output, format='PDF', save_all=True,
        append_images=[Image.new('RGB', (16, 16), 'blue')])
    return output.getvalue()


@pytest.mark.linux_only
def test_real_pdf_pages_preserve_order_and_do_not_overwrite_another_session(staging):
    _, sessions = staging
    first_response = rpc('pdf.attach', 'a', content_base64=base64.b64encode(pdf_bytes()).decode(),
        filename='neutral.pdf', first_page=1, last_page=2)
    assert 'error' not in first_response, first_response
    first_pages = first_response['result']['pages']
    assert [page['page'] for page in first_pages] == [1, 2]
    original = [(Path(page['path']), Path(page['path']).read_bytes()) for page in first_pages]
    second_response = rpc('pdf.attach', 'b', content_base64=base64.b64encode(pdf_bytes()).decode(),
        filename='neutral.pdf', first_page=1, last_page=2)
    assert 'error' not in second_response, second_response
    second_paths = [Path(page['path']) for page in second_response['result']['pages']]
    assert set(path for path, _ in original).isdisjoint(second_paths)
    assert all(path.read_bytes() == payload for path, payload in original)
    assert sessions['a']['attached_images'] == [str(path) for path, _ in original]
    assert sessions['b']['attached_images'] == [str(path) for path in second_paths]
    for index, path in enumerate(second_paths):
        with Image.open(path) as image:
            red, _, blue = image.convert('RGB').getpixel((image.width//2, image.height//2))
        assert (red > blue) if index == 0 else (blue > red)


@pytest.mark.linux_only
def test_failed_pdf_batch_does_not_publish_partial_pages_or_delete_prior_images(staging, monkeypatch):
    home, sessions = staging
    original = upload('a', image_bytes('red'))
    files_before = {p for p in (home/'images').rglob('*') if p.is_file()}
    queue = server._queue_attached_image
    attempts = []
    def fail_second(*args, **kwargs):
        attempts.append(kwargs['prefix'])
        if len(attempts) == 2:
            raise OSError('controlled second-page staging failure')
        return queue(*args, **kwargs)
    monkeypatch.setattr(server, '_queue_attached_image', fail_second)
    response = server.handle_request({'jsonrpc': '2.0', 'id': 'pdf-failure',
        'method': 'pdf.attach', 'params': {'session_id': 'a', 'filename': 'neutral.pdf',
            'content_base64': base64.b64encode(pdf_bytes()).decode(), 'first_page': 1, 'last_page': 2}})
    assert response.get('error'), response
    assert len(attempts) == 2
    assert sessions['a']['attached_images'] == [str(original)]
    assert original.read_bytes() == image_bytes('red')
    assert {p for p in (home/'images').rglob('*') if p.is_file()} == files_before


@pytest.mark.linux_only
def test_failed_pdf_keeps_a_same_session_peer_upload(staging, monkeypatch):
    home, sessions = staging
    original = upload('a', image_bytes('red'))
    queue = server._queue_attached_image
    calls = []
    peer = []
    def fail_after_peer(*args, **kwargs):
        calls.append(kwargs['prefix'])
        if len(calls) == 2:
            peer.append(queue(sessions['a'], image_bytes('blue'), '.png', prefix='upload'))
            raise OSError('controlled PDF failure after peer publication')
        return queue(*args, **kwargs)
    monkeypatch.setattr(server, '_queue_attached_image', fail_after_peer)
    response = rpc('pdf.attach', 'a', content_base64=base64.b64encode(pdf_bytes()).decode(),
        filename='neutral.pdf', first_page=1, last_page=2)
    assert response.get('error'), response
    assert sessions['a']['attached_images'] == [str(original), str(peer[0])]
    assert original.read_bytes() == image_bytes('red')
    assert peer[0].read_bytes() == image_bytes('blue')
    assert {p for p in (home/'images').rglob('*') if p.is_file()} == {original, peer[0]}


@pytest.mark.linux_only
def test_pdf_response_construction_failure_does_not_publish_pages(staging, monkeypatch):
    home, sessions = staging
    original = upload('a', image_bytes('red'))
    ok = server._ok
    def fail_pdf(rid, result):
        if 'pages_attached' in result:
            raise OSError('controlled PDF response construction failure')
        return ok(rid, result)
    monkeypatch.setattr(server, '_ok', fail_pdf)
    response = rpc('pdf.attach', 'a', content_base64=base64.b64encode(pdf_bytes()).decode(),
        filename='neutral.pdf', first_page=1, last_page=2)
    assert response.get('error'), response
    assert sessions['a']['attached_images'] == [str(original)]
    assert original.read_bytes() == image_bytes('red')
    assert {p for p in (home/'images').rglob('*') if p.is_file()} == {original}


@pytest.mark.linux_only
def test_new_staging_occurrence_directory_is_private(staging):
    _, _ = staging
    path = upload('a', image_bytes('red'))
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
