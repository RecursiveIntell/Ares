"""Manifest writes must not follow attacker-controlled leaf links."""
import json
import os
from pathlib import Path

import pytest

from tools import delegation_live_log as live


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / 'live'
    path.mkdir()
    monkeypatch.setattr(live, 'live_transcript_root', lambda: path)
    return path


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'regular'])
def test_manifest_creation_preserves_existing_target(root, tmp_path, kind):
    run = root / 'deleg_12345678'
    run.mkdir()
    victim = tmp_path / 'victim'
    victim.write_text('synthetic canary')
    manifest = run / 'manifest.json'
    if kind == 'symlink':
        manifest.symlink_to(victim)
    elif kind == 'hardlink':
        os.link(victim, manifest)
    else:
        manifest.write_text('synthetic canary')
    deleg_id, writers, paths = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    assert deleg_id == 'deleg_12345678' and writers[0] is not None and len(paths) == 1
    assert victim.read_text() == 'synthetic canary'
    if kind == 'regular':
        assert manifest.read_text() == 'synthetic canary'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_manifest_update_refuses_replacement_link(root, tmp_path, kind):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    target = root / deleg_id / 'manifest.json'
    target.unlink()
    victim = tmp_path / 'victim.json'
    contents = json.dumps({'tasks': [{'index': 0, 'status': 'victim'}], 'canary': 'unchanged'})
    victim.write_text(contents)
    if kind == 'symlink':
        target.symlink_to(victim)
    else:
        os.link(victim, target)
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    assert victim.read_text() == contents


def test_ordinary_manifest_status_update_still_works(root):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed', 'exit_reason': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    manifest = json.loads((root / deleg_id / 'manifest.json').read_text())
    assert manifest['tasks'][0]['status'] == 'completed'
    assert manifest['tasks'][0]['exit_reason'] == 'completed'
    assert 'completed' in manifest


def test_regular_replacement_is_not_accepted_as_owned_manifest(root):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    target = root / deleg_id / 'manifest.json'
    replacement = target.with_name('replacement.json')
    contents = json.dumps({'tasks': [{'index': 0, 'status': 'foreign'}]})
    replacement.write_text(contents)
    os.replace(replacement, target)
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    assert target.read_text() == contents


def test_collision_does_not_grant_update_authority(root):
    run = root / 'deleg_12345678'
    run.mkdir()
    target = run / 'manifest.json'
    contents = json.dumps({'tasks': [{'index': 0, 'status': 'foreign'}]})
    target.write_text(contents)
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=getattr(writers[0], '_manifest_identity', None))
    assert target.read_text() == contents


def test_manifest_replacement_between_read_and_write_is_refused(root, monkeypatch):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    target = root / deleg_id / 'manifest.json'
    original_contents = target.read_text()
    moved = target.with_name('original.json')
    replacement_contents = json.dumps({'tasks': [{'index': 0, 'status': 'foreign'}]})
    original_load = json.load
    def replace_after_read(fh):
        doc = original_load(fh)
        target.rename(moved)
        target.write_text(replacement_contents)
        return doc
    monkeypatch.setattr(live.json, 'load', replace_after_read)
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    assert target.read_text() == replacement_contents
    assert moved.read_text() == original_contents


def test_update_fails_closed_if_no_follow_support_is_missing(root, monkeypatch):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    target = root / deleg_id / 'manifest.json'
    original = target.read_text()
    monkeypatch.delattr(os, 'O_NOFOLLOW', raising=False)
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    assert target.read_text() == original


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO unavailable')
def test_manifest_update_refuses_fifo_without_blocking(root):
    deleg_id, writers, _ = live.create_live_transcripts(
        [{'goal': 'inspect synthetic target'}], delegation_id='deleg_12345678'
    )
    target = root / deleg_id / 'manifest.json'
    target.unlink()
    os.mkfifo(target)
    live.update_manifest_statuses(deleg_id, [{'task_index': 0, 'status': 'completed'}],
                                  expected_identity=writers[0]._manifest_identity)
    assert target.exists() and not target.is_file()
