"""Orchestration policy inheritance has one owner for every child-publication path.

A child session is published three ways: a fork (``create_session`` with a
parent), a compression child and a context-rebase child. Each must copy the
parent's effective policy inside the publishing transaction.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from hermes_state import SessionDB



@pytest.fixture
def db(tmp_path):
    owner = SessionDB(db_path=tmp_path / "state.db")
    yield owner
    owner.close()


def _session(db, session_id, *, parent=None):
    db.create_session(session_id, source="cli", profile_name="p1", model="test", parent_session_id=parent)
    db.append_message(session_id, "user", "original task")


def _seed(db, session_id):
    policy = OrchestrationPolicy(
        allow_graph=False, secondary_route=SecondaryRoute("openai-codex", "exact-worker", "low"),
    ).to_dict()
    db.set_orchestration_policy(session_id, policy, expected_revision=0, origin="operator-command")
    return db.get_orchestration_policy(session_id)


def _fork(db, parent, child):
    db.create_session(child, source="cli", parent_session_id=parent)


def _compression_child(db, parent, child):
    db.publish_compression_child(
        parent_session_id=parent, child_session_id=child, source="cli",
        messages=[{"role": "user", "content": "compacted handoff"}],
        require_compression_lease=False,
    )


def _context_rebase_child(db, parent, child):
    assert db.try_acquire_session_turn_lease(parent, "holder", ttl_seconds=300)
    snapshot = db.read_context_rebase_snapshot(parent)
    db.publish_context_rebase_child(
        transition_id="tx-" + child, parent_session_id=parent, child_session_id=child,
        continuation_digest="sha256:" + "a" * 64, expected_snapshot_digest=snapshot.digest,
        control_revision=snapshot.control_revision,
        input_watermark=db.get_active_message_watermark(parent), turn_lease_holder="holder",
        source="cli",
        messages=[
            {"role": "assistant", "content": "continuation brief", "display_kind": "hidden"},
            {"role": "user", "content": "continue the exact task"},
        ],
        model="test", model_config={"temperature": 0}, system_prompt="trusted base prompt",
        profile_name="p1", custody_transfers=(),
    )


PATHS = {"fork": _fork, "compression_child": _compression_child, "context_rebase_child": _context_rebase_child}


def _row(db, session_id):
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(
            "SELECT revision, policy_json, origin, source_message_id"
            " FROM session_orchestration_policy WHERE session_id=?", (session_id,),
        ).fetchone()
    finally:
        conn.close()


def _forget_policy(db, session_id):
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute("DELETE FROM session_orchestration_policy WHERE session_id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("path", sorted(PATHS))
def test_child_copies_the_parent_policy_bytes_and_records_its_origin(db, path):
    _session(db, "parent")
    parent = _seed(db, "parent")
    PATHS[path](db, "parent", "child")
    revision, policy_json, origin, source_message_id = _row(db, "child")
    assert policy_json == json.dumps(parent["policy"], sort_keys=True, separators=(",", ":"))
    assert (revision, origin) == (1, f"inherit:parent:{parent['revision']}")
    assert source_message_id == parent["source_message_id"]
    assert db.get_orchestration_policy("child")["policy"] == parent["policy"]


@pytest.mark.parametrize("path", sorted(PATHS))
def test_parent_without_a_policy_gives_the_child_the_declared_default(db, path):
    _session(db, "parent")
    assert db.get_orchestration_policy("parent") is None
    PATHS[path](db, "parent", "child")
    revision, policy_json, origin, source_message_id = _row(db, "child")
    assert (revision, origin, source_message_id) == (1, "inherit:parent:0", None)
    assert json.loads(policy_json) == OrchestrationPolicy.from_dict(json.loads(policy_json)).to_dict()


def test_every_path_gives_a_parent_without_a_policy_the_same_default(db):
    stored = []
    for index, path in enumerate(sorted(PATHS)):
        _session(db, f"parent-{index}")
        PATHS[path](db, f"parent-{index}", f"child-{index}")
        stored.append(_row(db, f"child-{index}")[1])
    assert len(set(stored)) == 1


def test_only_a_fork_refuses_a_parent_whose_own_lineage_is_unqualified(db):
    """``create_session`` reads the parent with the lineage requirement; the
    compression and rebase publishers deliberately read it without."""
    for index, path in enumerate(sorted(PATHS)):
        root, middle, child = f"root-{index}", f"middle-{index}", f"child-{index}"
        _session(db, root)
        _seed(db, root)
        _session(db, middle, parent=root)
        _forget_policy(db, middle)
        if path == "fork":
            with pytest.raises(OrchestrationError, match="orchestration_lineage_unqualified"):
                PATHS[path](db, middle, child)
            assert db.get_session(child) is None
        else:
            PATHS[path](db, middle, child)
            assert _row(db, child)[2] == f"inherit:{middle}:0"
