"""Real SQLite/credential lifecycle; no production stores or approval keys."""
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from ares_runtime.continuity.credentials import ControllerCredentialError, controller_credential
from hermes_cli.sqlite_safe_read import connect_tracked, connection_file_identity
from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError


pytestmark = pytest.mark.linux_only


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    yield value
    value.close()


def test_bootstrap_is_durable_and_metadata_never_contains_private_key(db):
    public = db.initialize_context_controller()
    assert db.initialize_context_controller() == public
    key_path = Path(db.db_path).parent / "context-controller-keys" / public["key_ref"]
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.parent.stat().st_mode & 0o777 == 0o700
    private = key_path.read_bytes()
    assert len(private) == 32
    assert bytes(public["public_key"]) != private
    assert private.hex() not in db.get_meta("context-controller:v1")
    assert list(private) not in list(json.loads(db.get_meta("context-controller:v1")).values())
    db.close()
    reopened = SessionDB(db_path=db.db_path)
    try:
        assert reopened.initialize_context_controller() == public
    finally:
        reopened.close()


@pytest.mark.parametrize("phase", [1, 2])
def test_lost_sqlite_bootstrap_ack_reads_exact_committed_identity(db, monkeypatch, phase):
    original = db._execute_write
    calls = 0

    def lose_ack(fn, *args, **kwargs):
        nonlocal calls
        result = original(fn, *args, **kwargs)
        calls += 1
        if calls == phase:
            raise OSError("ack lost")
        return result

    monkeypatch.setattr(db, "_execute_write", lose_ack)
    public = db.initialize_context_controller()
    assert db.read_context_controller_identity() == public
    assert len(list((Path(db.db_path).parent / "context-controller-keys").iterdir())) == 1


def test_completed_orphan_is_reconciled_using_the_existing_intent(db, monkeypatch):
    original = db._execute_write
    calls = 0

    def crash_before_commit(fn, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("process interruption")
        return original(fn, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", crash_before_commit)
    with pytest.raises(ContextContinuationError, match="BOOTSTRAP_PENDING"):
        db.initialize_context_controller()
    planned = json.loads(db.get_meta("context-controller:v1"))
    key_path = Path(db.db_path).parent / "context-controller-keys" / planned["key_ref"]
    original_key = key_path.read_bytes()
    monkeypatch.setattr(db, "_execute_write", original)
    ready = db.initialize_context_controller()
    assert ready["key_ref"] == planned["key_ref"]
    assert ready["store_nonce"] == planned["store_nonce"]
    assert key_path.read_bytes() == original_key


def test_database_backup_and_profile_copy_do_not_copy_signing_authority(db, tmp_path):
    public = db.initialize_context_controller()
    copied_dir = tmp_path / "copied"
    copied_dir.mkdir()
    dst = sqlite3.connect(copied_dir / "state.db")
    try:
        with db._lock:
            db._conn.backup(dst)
    finally:
        dst.close()
    shutil.copytree(tmp_path / "context-controller-keys", copied_dir / "context-controller-keys")
    copied = SessionDB(db_path=copied_dir / "state.db")
    try:
        assert json.loads(copied.get_meta("context-controller:v1"))["public_key"] == public["public_key"]
        with pytest.raises(ContextContinuationError, match="IDENTITY_MISMATCH"):
            copied.initialize_context_controller()
    finally:
        copied.close()


@pytest.mark.parametrize("change", ["mode", "replace", "symlink", "hardlink", "missing"])
def test_credential_replacement_or_insecure_file_never_repairs_itself(db, change):
    public = db.initialize_context_controller()
    path = Path(db.db_path).parent / "context-controller-keys" / public["key_ref"]
    if change == "mode":
        path.chmod(0o644)
    elif change == "hardlink":
        os.link(path, path.with_suffix(".copy"))
    else:
        moved = path.with_suffix(".saved")
        path.rename(moved)
        if change == "symlink":
            path.symlink_to(moved)
        elif change == "replace":
            shutil.copyfile(moved, path)
            path.chmod(0o600)
    with pytest.raises(ControllerCredentialError):
        db.initialize_context_controller()
    assert json.loads(db.get_meta("context-controller:v1")) == public


def test_live_sqlite_replacement_is_detected_without_reopening_the_database(tmp_path):
    path = tmp_path / "original.db"
    conn = connect_tracked(path)
    conn.execute("CREATE TABLE witness(n INTEGER)")
    identity = connection_file_identity(conn)
    path.rename(tmp_path / "retained.db")
    other = connect_tracked(path)
    try:
        assert connection_file_identity(other) != identity
        with pytest.raises(ValueError, match="DATABASE_REPLACED"):
            connection_file_identity(conn)
    finally:
        conn.close()
        other.close()


def test_bootstrap_refuses_readonly_store(db):
    db.initialize_context_controller()
    readonly = SessionDB(db_path=db.db_path, read_only=True)
    try:
        with pytest.raises(ContextContinuationError, match="READ_ONLY"):
            readonly.initialize_context_controller()
    finally:
        readonly.close()


def test_private_key_can_verify_a_signature_but_is_not_exported(db):
    public = db.initialize_context_controller()
    with controller_credential(db.db_path, public) as key:
        signature = key.sign(b"test-only challenge")
        key.public_key().verify(signature, b"test-only challenge")
    assert "private_key" not in public


def test_credentials_are_filtered_from_tools_and_backup_collection(db):
    from agent.file_safety import get_read_block_error, is_write_denied
    from hermes_cli.backup import _iter_external_files, _should_exclude

    public = db.initialize_context_controller()
    relative = Path("context-controller-keys") / public["key_ref"]
    path = Path(db.db_path).parent / relative
    assert get_read_block_error(str(path))
    assert is_write_denied(str(path))
    assert _should_exclude(relative)
    assert path not in _iter_external_files(Path(db.db_path).parent)
    assert _iter_external_files(path) == []


def test_private_orphan_is_fsynced_before_ready(db, monkeypatch):
    from ares_runtime.continuity.credentials import create_controller_credential

    public = db.initialize_context_controller()
    calls = []
    original = os.fsync

    def observe(fd):
        calls.append(os.fstat(fd).st_mode)
        return original(fd)

    monkeypatch.setattr(os, "fsync", observe)
    import stat
    assert create_controller_credential(db.db_path, public["key_ref"])["public_key"] == public["public_key"]
    assert any(stat.S_ISREG(mode) for mode in calls)
    assert any(stat.S_ISDIR(mode) for mode in calls)


def test_profile_clone_export_import_cannot_transfer_controller_key(db, tmp_path, monkeypatch):
    import tarfile
    from hermes_cli import profiles

    public = db.initialize_context_controller()
    source = Path(db.db_path).parent
    (source / "config.yaml").write_text("model: test\n")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda _: source)
    copied = tmp_path / "clone"
    # Keep the target outside the source to exercise the real recursive copy.
    source_files = tmp_path / "profile"
    source_files.mkdir()
    shutil.copytree(source / "context-controller-keys", source_files / "context-controller-keys")
    shutil.copytree(source_files, copied, ignore=profiles._clone_all_copytree_ignore(source_files))
    assert not (copied / "context-controller-keys").exists()
    archive = profiles.export_profile("source", str(tmp_path / "profile.tar.gz"))
    with tarfile.open(archive) as tf:
        assert all("context-controller-keys" not in Path(m.name).parts for m in tf.getmembers())
    hostile = tmp_path / "foreign.tar.gz"
    with tarfile.open(hostile, "w:gz") as tf:
        tf.add(source / "context-controller-keys", arcname="source/context-controller-keys")
    with pytest.raises(ValueError, match="nonportable context controller"):
        profiles._safe_extract_profile_archive(hostile, tmp_path / "imported")
    assert public == db.read_context_controller_identity()


def test_attachment_and_media_readers_refuse_controller_files(db):
    from agent.context_references import _ensure_reference_path_allowed
    from gateway.platforms.base import BasePlatformAdapter

    public = db.initialize_context_controller()
    path = Path(db.db_path).parent / "context-controller-keys" / public["key_ref"]
    with pytest.raises(ValueError, match="sensitive credential"):
        _ensure_reference_path_allowed(path)
    assert BasePlatformAdapter.validate_media_delivery_path(str(path)) is None
