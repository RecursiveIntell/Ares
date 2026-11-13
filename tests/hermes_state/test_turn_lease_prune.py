"""Expired cross-process turn leases must not accumulate forever.

`try_acquire_session_turn_lease` only cleaned the conversation it was acquiring,
so leases for tips that were compressed away, abandoned profiles, or crashed
hosts stayed in `session_turn_leases` indefinitely (observed: rows from two
months earlier). The prune is deliberately conservative — only rows well past
expiry — because a lease that merely lapsed during a long tool call may still be
refreshed by its holder.
"""

import sqlite3
import time

from hermes_state import SessionDB


def _age_lease(path, session_fragment: str, *, seconds_ago: float) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE session_turn_leases SET expires_at = ? "
            "WHERE conversation_id LIKE ?",
            (time.time() - seconds_ago, f"%{session_fragment}%"),
        )
        conn.commit()


def _lease_keys(path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT conversation_id FROM session_turn_leases")]


def test_long_expired_foreign_lease_is_pruned(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)

    try:
        assert db.try_acquire_session_turn_lease("old-conv", "dead-holder", ttl_seconds=300)
        _age_lease(path, "old-conv", seconds_ago=7200)

        assert db.try_acquire_session_turn_lease("new-conv", "live-holder", ttl_seconds=300)
        keys = _lease_keys(path)
    finally:
        db.close()

    assert not any("old-conv" in key for key in keys)
    assert any("new-conv" in key for key in keys)


def test_recently_lapsed_lease_survives_the_prune(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)

    try:
        assert db.try_acquire_session_turn_lease("lapsed-conv", "slow-holder", ttl_seconds=300)
        _age_lease(path, "lapsed-conv", seconds_ago=300)

        assert db.try_acquire_session_turn_lease("other-conv", "live-holder", ttl_seconds=300)
        keys = _lease_keys(path)
    finally:
        db.close()

    assert any("lapsed-conv" in key for key in keys)
