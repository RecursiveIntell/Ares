"""C07 reader/backout, backup and crash-durability contract for native media custody.

Neutral generated images, scratch homes only, never ~/.ares or ~/.hermes.

Declared durability claim (operator decision, 2026-10-12): PROCESS-CRASH ONLY.
These tests SIGKILL a real process at a transaction boundary. They are not
power-loss proof and must not be renamed as such.

The old-reader witness runs the *released* be0088f reader (exported with
``git archive``) against a copy of a database that contains a V2 media receipt.
It is skipped, with the reason printed, when that commit is not present in the
local clone; a skip there is "not exercised", never a pass.
"""
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError

REPO = Path(__file__).resolve().parents[2]
RELEASED_READER = 'be0088f84dc1c8c6a66cad230815b31ebf85220c'


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(out, format='PNG')
    return out.getvalue()


def origin():
    return {'schema': 'SessionDBDesktopInputOriginV1', 'connection_id': None,
            'runtime_session_id': 'rt', 'stored_session_id': 'owned', 'profile_name': 'default',
            'owner_epoch': 'oe', 'transport_epoch': 'te', 'stop_revision': 0}


def item(color='red', name='neutral.png'):
    return {'data': png(color), 'mime_type': 'image/png', 'display_name': name}


def accept_media(db, event, color='red'):
    return db.accept_context_media_input(
        'owned', source='desktop', event_id=event, text='Inspect this image',
        media=[item(color, f'{event}.png')], desktop_origin=origin())


def seeded_home(tmp_path, name='home'):
    home = tmp_path / name
    home.mkdir()
    (home / 'config.yaml').write_text('model: {}\n')
    db = SessionDB(home / 'state.db')
    db.create_session('owned', source='desktop')
    db.accept_context_input('owned', source='desktop', event_id='text-1', content='plain text input')
    accept_media(db, 'media-1')
    db.close()
    return home


def blob_files(home):
    root = Path(home) / 'attachments'
    return sorted(p.relative_to(home).as_posix() for p in root.rglob('*') if p.is_file()) if root.exists() else []


def run_backup(home, zpath, monkeypatch):
    from hermes_cli import backup
    monkeypatch.setenv('HERMES_HOME', str(home))
    backup.run_backup(types.SimpleNamespace(output=str(zpath)))


def run_restore(home, zpath, monkeypatch):
    from hermes_cli import backup, gateway
    monkeypatch.setenv('HERMES_HOME', str(home))
    # run_import best-effort installs/starts a gateway service. Never let a test reach the service manager.
    monkeypatch.setattr(gateway, 'ensure_gateway_service', lambda *a, **k: False)
    monkeypatch.setattr(gateway, '_is_service_running', lambda *a, **k: True)
    backup.run_import(types.SimpleNamespace(zipfile=str(zpath), force=True))


# ---------------------------------------------------------------- old reader

@pytest.fixture(scope='module')
def released_tree(tmp_path_factory):
    check = subprocess.run(['git', 'cat-file', '-e', f'{RELEASED_READER}^{{commit}}'], cwd=REPO,
                           capture_output=True)
    if check.returncode != 0:
        pytest.skip(f'released reader commit {RELEASED_READER[:12]} not in this clone: old-reader '
                    'compatibility NOT exercised')
    dest = tmp_path_factory.mktemp('released-reader')
    archive = subprocess.run(['git', 'archive', RELEASED_READER], cwd=REPO, capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter='data')
    assert not (dest / 'hermes_state_input_media.py').exists(), 'released tree unexpectedly has the media module'
    return dest


OLD_PROBE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hermes_state
assert hermes_state.__file__.startswith(sys.argv[1]), hermes_state.__file__
db = hermes_state.SessionDB(Path(sys.argv[2]))
def attempt(fn):
    try:
        return {'ok': True, 'value': fn()}
    except BaseException as exc:
        return {'ok': False, 'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
out = {
  'pending': attempt(lambda: [r.event_id for r in db.read_pending_context_inputs('owned')]),
  'work': attempt(lambda: len(db.read_context_input_work('owned')['receipts'])),
  'media': attempt(lambda: db.read_context_input('owned', source='desktop', event_id='media-1').schema),
  'text': attempt(lambda: db.read_context_input('owned', source='desktop', event_id='text-1').schema),
  'messages': attempt(lambda: len(db.get_messages('owned'))),
}
db.close()
print(json.dumps(out))
'''


def table_digest(path):
    """Content of every receipt/transcript-bearing table, to prove the old reader wrote nothing."""
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' "
            "and name not like '%_fts%' order by name")]
        return {t: sorted(map(repr, conn.execute(f'select * from "{t}"').fetchall())) for t in tables}
    finally:
        conn.close()


def test_released_reader_refuses_media_receipt_and_never_projects_it(released_tree, tmp_path):
    home = seeded_home(tmp_path)
    copy = tmp_path / 'old-reader-copy.db'
    conn = sqlite3.connect(home / 'state.db')
    backup = sqlite3.connect(copy)
    conn.backup(backup)
    backup.close()
    conn.close()
    before = table_digest(copy)

    proc = subprocess.run([sys.executable, '-P', '-c', OLD_PROBE, str(released_tree), str(copy)],
                          capture_output=True, text=True, timeout=120, cwd=released_tree,
                          env={**os.environ, 'HERMES_HOME': str(tmp_path / 'old-home'), 'PYTHONHASHSEED': '0'})
    assert proc.returncode == 0, proc.stderr[-800:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # Fail closed: the released reader raises a typed refusal for everything that touches the V2 receipt.
    for label in ('pending', 'work', 'media'):
        assert out[label]['ok'] is False, (label, out[label])
        assert out[label]['code'] == 'CONTEXT_INPUT_RECORD_INVALID', (label, out[label])
    # Plain V1 text remains readable on its own, and nothing was projected into the transcript.
    assert out['text'] == {'ok': True, 'value': 'SessionDBContextInputV1'}
    assert out['messages'] == {'ok': True, 'value': 0}
    # The old reader's open performs exactly one bookkeeping write (a search-index version marker).
    # It must not write anything else: no receipt, inbox, work, transcript or authority row changes.
    after = table_digest(copy)
    changed = {t for t in set(before) | set(after) if before.get(t) != after.get(t)}
    assert changed == {'state_meta'}, changed
    added = set(after['state_meta']) - set(before['state_meta'])
    assert not (set(before['state_meta']) - set(after['state_meta'])), 'old reader removed a state_meta row'
    assert len(added) == 1 and 'fts_storage_version' in next(iter(added)), added


def test_released_reader_refusal_parks_the_whole_root_which_is_the_accepted_rollback_cost(released_tree, tmp_path):
    """A text input queued BEHIND a media receipt is unreadable to the old reader too.

    Operator accepted this (2026-10-12): rollback parks the whole root until the media receipt is
    resolved. This test pins that the cost is a typed refusal, not data loss or a lossy projection.
    """
    home = seeded_home(tmp_path)
    db = SessionDB(home / 'state.db')
    db.accept_context_input('owned', source='desktop', event_id='text-behind', content='queued behind media')
    db.close()
    copy = tmp_path / 'copy.db'
    src = sqlite3.connect(home / 'state.db')
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    proc = subprocess.run([sys.executable, '-P', '-c', OLD_PROBE, str(released_tree), str(copy)],
                          capture_output=True, text=True, timeout=120, cwd=released_tree,
                          env={**os.environ, 'HERMES_HOME': str(tmp_path / 'old-home'), 'PYTHONHASHSEED': '0'})
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out['pending']['ok'] is False and out['pending']['code'] == 'CONTEXT_INPUT_RECORD_INVALID'
    # The new reader still sees every receipt, so nothing was lost by the rollback window.
    again = SessionDB(copy)
    try:
        assert [r.event_id for r in again.read_pending_context_inputs('owned')] == [
            'text-1', 'media-1', 'text-behind']
    finally:
        again.close()


# ------------------------------------------------------------ crash ordering

CRASH = r'''
import io, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PIL import Image
from hermes_state import SessionDB
def png():
    b = io.BytesIO(); Image.new('RGB', (8, 8), 'green').save(b, format='PNG'); return b.getvalue()
db = SessionDB(Path(sys.argv[2]) / 'state.db')
real = db._accept_context_input_owned
def die(*a, **k):
    factory = k['content_factory']
    def after_publish(root, profile):
        content = factory(root, profile)       # the media bytes are now published
        os.kill(os.getpid(), signal.SIGKILL)   # crash before the receipt commit
        return content
    k['content_factory'] = after_publish
    return real(*a, **k)
db._accept_context_input_owned = die
db.accept_context_media_input('owned', source='desktop', event_id='crash-1', text='crash me',
    media=[{'data': png(), 'mime_type': 'image/png', 'display_name': 'crash.png'}],
    desktop_origin={'schema': 'SessionDBDesktopInputOriginV1', 'connection_id': None, 'runtime_session_id': 'rt',
        'stored_session_id': 'owned', 'profile_name': 'default', 'owner_epoch': 'oe', 'transport_epoch': 'te',
        'stop_revision': 0})
print('SURVIVED')
'''


def test_sigkill_after_blob_publish_before_receipt_commit_leaves_no_receipt_and_retry_is_idempotent(tmp_path):
    home = seeded_home(tmp_path)
    blobs_before = blob_files(home)
    proc = subprocess.run([sys.executable, '-P', '-c', CRASH, str(REPO), str(home)],
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, 'HERMES_HOME': str(home / '.h')})
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert 'SURVIVED' not in proc.stdout

    db = SessionDB(home / 'state.db')
    try:
        # Blob-before-receipt: bytes may exist without a receipt, never the reverse.
        assert len(blob_files(home)) == len(blobs_before) + 1
        assert [r.event_id for r in db.read_pending_context_inputs('owned')] == ['text-1', 'media-1']
        # The same event id re-delivered after the crash is accepted exactly once, with exact bytes.
        first = db.accept_context_media_input('owned', source='desktop', event_id='crash-1', text='crash me',
                                              media=[item('green', 'crash.png')], desktop_origin=origin())
        second = db.accept_context_media_input('owned', source='desktop', event_id='crash-1', text='crash me',
                                               media=[item('green', 'crash.png')], desktop_origin=origin())
        assert first == second
        assert db.read_context_input_media('owned', first) == (png('green'),)
        assert [r.event_id for r in db.read_pending_context_inputs('owned')] == ['text-1', 'media-1', 'crash-1']
        # Deduplicated content-addressed publish: the retry did not add a second blob.
        assert len(blob_files(home)) == len(blobs_before) + 1
    finally:
        db.close()


# ------------------------------------------------------------ backup / restore

def test_backup_restore_roundtrip_preserves_receipts_and_exact_bytes(tmp_path, monkeypatch):
    home = seeded_home(tmp_path)
    zpath = tmp_path / 'backup.zip'
    run_backup(home, zpath, monkeypatch)
    with zipfile.ZipFile(zpath) as zf:
        assert zf.testzip() is None
        assert any(n.startswith('attachments/') for n in zf.namelist()), 'blobs missing from archive'
    restored = tmp_path / 'restored'
    restored.mkdir()
    run_restore(restored, zpath, monkeypatch)
    db = SessionDB(restored / 'state.db')
    try:
        assert [r.event_id for r in db.read_pending_context_inputs('owned')] == ['text-1', 'media-1']
        receipt = db.read_context_input('owned', source='desktop', event_id='media-1')
        assert db.read_context_input_media('owned', receipt) == (png('red'),)
    finally:
        db.close()
    assert blob_files(restored) == blob_files(home)


@pytest.mark.parametrize('remove_whole_tree, code', [
    (False, 'CONTEXT_INPUT_MEDIA_UNAVAILABLE'),          # byte files gone, directories remain
    (True, 'CONTEXT_INPUT_MEDIA_STORAGE_UNAVAILABLE'),   # whole attachments tree absent (e.g. restored without it)
])
def test_missing_blobs_refuse_media_with_typed_error_and_keep_text_and_receipt(tmp_path, remove_whole_tree, code):
    home = seeded_home(tmp_path)
    if remove_whole_tree:
        shutil.rmtree(home / 'attachments')
    else:
        for p in (home / 'attachments').rglob('*'):
            if p.is_file():
                p.unlink()
    db = SessionDB(home / 'state.db')
    try:
        receipt = db.read_context_input('owned', source='desktop', event_id='media-1')
        with pytest.raises(ContextContinuationError) as err:
            db.read_context_input_media('owned', receipt)
        assert err.value.code == code
        # No lossy fallback: the receipt survives, text is intact, nothing was invented.
        assert db.read_context_input('owned', source='desktop', event_id='text-1').content == 'plain text input'
        assert [r.event_id for r in db.read_pending_context_inputs('owned')] == ['text-1', 'media-1']
    finally:
        db.close()


def test_backup_never_archives_a_media_receipt_without_its_blob(tmp_path, monkeypatch):
    """Backup consistency (plan C07): a receipt that reaches the archive must have its bytes there too.

    The product scans the file tree FIRST and snapshots state.db LATER. A media input accepted in
    between lands in the snapshot but was absent from the scanned file list. The backup must sweep the
    byte namespace after the snapshot so the archive is self-consistent.
    """
    from hermes_cli import backup
    home = seeded_home(tmp_path)
    real_copy = backup._safe_copy_db
    fired = []

    def accept_between_scan_and_snapshot(src, dst, *args, **kwargs):
        if not fired and Path(src).name == 'state.db':
            fired.append(True)
            db = SessionDB(home / 'state.db')
            try:
                accept_media(db, 'raced-1', 'blue')
            finally:
                db.close()
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup, '_safe_copy_db', accept_between_scan_and_snapshot)
    zpath = tmp_path / 'raced.zip'
    run_backup(home, zpath, monkeypatch)
    assert fired, 'race injector never fired: test is not exercising the scan/snapshot window'

    with zipfile.ZipFile(zpath) as zf:
        assert zf.testzip() is None
        blobs_in_archive = [n for n in zf.namelist() if n.startswith('attachments/')]
    assert len(blobs_in_archive) == 2, blobs_in_archive   # media-1 and the raced-1 blob

    restored = tmp_path / 'restored'
    restored.mkdir()
    run_restore(restored, zpath, monkeypatch)
    db = SessionDB(restored / 'state.db')
    try:
        pending = [r.event_id for r in db.read_pending_context_inputs('owned')]
        assert pending == ['text-1', 'media-1', 'raced-1']
        for event, color in (('media-1', 'red'), ('raced-1', 'blue')):
            receipt = db.read_context_input('owned', source='desktop', event_id=event)
            assert db.read_context_input_media('owned', receipt) == (png(color),)
    finally:
        db.close()
