"""Reclaim only provably dead holders on unrelated lease acquisition.

Use real, bounded Python subprocesses instead of POSIX-only commands or
invented 'live' PIDs. The database and every child belong to the test.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from hermes_state import SessionDB


def _make_dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


@contextmanager
def _live_child_pid():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc.pid
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _holder(pid: int, turn: str) -> str:
    return f"pid={pid}:turn={turn}:platform=desktop"


def _lease_keys(path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute("SELECT conversation_id FROM session_turn_leases")]


def test_dead_holder_foreign_lease_is_swept_on_any_acquire(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        assert db.try_acquire_session_turn_lease(
            "victim-conv", _holder(_make_dead_pid(), "victim"), ttl_seconds=3600
        )
        assert db.try_acquire_session_turn_lease(
            "new-conv", _holder(os.getpid(), "live"), ttl_seconds=60
        )
        keys = _lease_keys(path)
    finally:
        db.close()
    assert not any("victim-conv" in key for key in keys)
    assert any("new-conv" in key for key in keys)


def test_same_process_holder_survives_the_sweep(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        assert db.try_acquire_session_turn_lease(
            "live-conv", _holder(os.getpid(), "mine"), ttl_seconds=3600
        )
        assert db.try_acquire_session_turn_lease(
            "probe-conv", _holder(os.getpid(), "probe"), ttl_seconds=60
        )
        keys = _lease_keys(path)
    finally:
        db.close()
    assert any("live-conv" in key for key in keys)


def test_different_live_process_holder_survives_the_sweep(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        with _live_child_pid() as pid:
            assert db.try_acquire_session_turn_lease(
                "live-child-conv", _holder(pid, "child"), ttl_seconds=3600
            )
            assert db.try_acquire_session_turn_lease(
                "probe-conv", _holder(os.getpid(), "probe"), ttl_seconds=60
            )
            keys = _lease_keys(path)
    finally:
        db.close()
    assert any("live-child-conv" in key for key in keys)


def test_unstructured_legacy_holder_survives_the_sweep(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        assert db.try_acquire_session_turn_lease(
            "legacy-conv", "legacy-owner-without-process-evidence", ttl_seconds=3600
        )
        assert db.try_acquire_session_turn_lease(
            "probe-conv", _holder(os.getpid(), "probe"), ttl_seconds=60
        )
        keys = _lease_keys(path)
    finally:
        db.close()
    assert any("legacy-conv" in key for key in keys)


def test_process_probe_uncertainty_preserves_the_holder(tmp_path, monkeypatch):
    import hermes_state

    def denied(_pid):
        raise PermissionError("test-owned process evidence unavailable")

    monkeypatch.setattr(hermes_state, "psutil", SimpleNamespace(pid_exists=denied))
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        assert db.try_acquire_session_turn_lease(
            "uncertain-conv", _holder(os.getpid() + 100000, "uncertain"), ttl_seconds=3600
        )
        assert db.try_acquire_session_turn_lease(
            "probe-conv", _holder(os.getpid(), "probe"), ttl_seconds=60
        )
        keys = _lease_keys(path)
    finally:
        db.close()
    assert any("uncertain-conv" in key for key in keys)
