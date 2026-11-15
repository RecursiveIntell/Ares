"""Exact mission artifacts in the real Kanban owner, not a second task store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from ares_runtime.collaboration import MissionContractV1, contract_ref
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.linux_only


@pytest.fixture
def owner(tmp_path, monkeypatch):
    assert sys.version_info >= (3, 14)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db(board="default")
    with kb.connect_closing(board="default") as conn:
        task = kb.create_task(conn, title="offline mission fixture", board="default")
        yield conn, task


def mission(task, objective="verify"):
    return MissionContractV1.create({
        "mission_id": "mission:offline", "kanban_root_task_ref": "task:" + task,
        "objective": objective,
        "source_freeze": [{"source_ref": "repo:fixture", "revision_or_digest": "frozen",
                           "state": "OBSERVED_SOURCE"}],
        "closure_profile": "engineering", "risk_class": "low", "effect_class": "none",
        "boundaries": {"allowed": ["offline fixture"], "forbidden": ["send", "activate"],
                       "source_owners": ["repo:fixture"]},
        "required_evidence": [{"gate_id": "behavior", "evidence_kinds": ["test_result"],
                               "minimum_count": 1, "independent_witness_required": False}],
        "topology_policy": {"default_executor_count": 1, "allowed_patterns": ["single"],
                            "max_concurrent_specialists": 1, "escalation_requires_evidence_gap": True},
        "stop_conditions": ["missing authority"],
    })


def attach(owner):
    conn, task = owner
    artifact = mission(task)
    attachment = kb.attach_task_mission_contract(conn, task, artifact, board="default")
    return conn, task, artifact, attachment


def attachment_path(conn, attachment):
    row = kb.get_attachment(conn, attachment)
    assert row is not None
    return Path(row.stored_path)


def read(conn, task, artifact, attachment, board="default"):
    return kb.read_task_mission_contract(conn, task, attachment,
                                        expected_contract_ref=contract_ref(artifact), board=board)


def test_owner_api_exists_without_optional_callback():
    assert Path(kb.__file__).resolve() == Path(__file__).resolve().parents[2] / "hermes_cli/kanban_db.py"
    assert callable(getattr(kb, "attach_task_mission_contract", None))
    assert callable(getattr(kb, "read_task_mission_contract", None))


def test_real_attachment_event_and_reopen_exact_readback(owner):
    conn, task, artifact, attachment = attach(owner)
    row = kb.get_attachment(conn, attachment)
    assert row is not None
    assert Path(row.stored_path).read_bytes() == artifact.canonical_bytes()
    assert row.task_id == task
    assert row.size == len(artifact.canonical_bytes())
    assert [e.kind for e in kb.list_events(conn, task)].count("attached") == 1
    with kb.connect_closing(board="default") as reopened:
        before = reopened.total_changes
        assert read(reopened, task, artifact, attachment).canonical_bytes() == artifact.canonical_bytes()
        assert reopened.total_changes == before
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("mission" in name or "campaign" in name for name in tables)


def test_mission_attachment_is_private_under_group_writable_umask(owner):
    previous = os.umask(0o002)
    try:
        conn, task, artifact, attachment = attach(owner)
    finally:
        os.umask(previous)
    path = attachment_path(conn, attachment)
    assert path.stat().st_mode & 0o777 == 0o600
    assert read(conn, task, artifact, attachment) == artifact


def test_readback_selects_exact_attachment_never_latest(owner):
    conn, task, original, first = attach(owner)
    successor = mission(task, "different objective")
    second = kb.attach_task_mission_contract(conn, task, successor, board="default")
    assert read(conn, task, original, first).to_dict() == original.to_dict()
    assert read(conn, task, successor, second).to_dict() == successor.to_dict()
    with pytest.raises(ValueError):
        read(conn, task, original, second)


@pytest.mark.parametrize("task_kind", ["missing", "foreign"])
def test_wrong_task_cannot_attach_or_read(owner, task_kind):
    conn, task, artifact, attachment = attach(owner)
    other = "t_ffffffff" if task_kind == "missing" else kb.create_task(conn, title="other", board="default")
    with pytest.raises(ValueError):
        kb.attach_task_mission_contract(conn, other, artifact, board="default")
    with pytest.raises(ValueError):
        read(conn, other, artifact, attachment)
    assert len(kb.list_attachments(conn, task)) == 1


def test_explicit_board_must_match_tracked_database(owner):
    conn, task, artifact, attachment = attach(owner)
    kb.init_db(board="other")
    with pytest.raises(ValueError):
        read(conn, task, artifact, attachment, board="other")
    with pytest.raises(ValueError):
        kb.attach_task_mission_contract(conn, task, artifact, board="other")


def test_untracked_database_cannot_supply_owner_identity(owner):
    conn, task, artifact, attachment = attach(owner)
    with sqlite3.connect(kb.kanban_db_path(board="default")) as untracked:
        untracked.row_factory = sqlite3.Row
        with pytest.raises(ValueError):
            read(untracked, task, artifact, attachment)


@pytest.mark.parametrize("corruption", ["missing", "changed", "duplicate", "noncanonical", "symlink", "hardlink", "fifo"])
def test_damaged_artifact_is_refused_without_repair(owner, tmp_path, corruption):
    conn, task, artifact, attachment = attach(owner)
    path = attachment_path(conn, attachment)
    raw = path.read_bytes()
    if corruption == "changed":
        path.write_bytes(raw.replace(b'"verify"', b'"VERIFY"'))
    elif corruption == "duplicate":
        path.write_bytes(b'{"objective":"verify",' + raw[1:])
    elif corruption == "noncanonical":
        path.write_bytes(json.dumps(artifact.to_dict(), indent=2).encode())
    elif corruption == "hardlink":
        os.link(path, tmp_path / "alias")
    else:
        path.unlink()
        if corruption == "symlink":
            target = tmp_path / "copied.json"
            target.write_bytes(raw)
            path.symlink_to(target)
        elif corruption == "fifo":
            os.mkfifo(path)
    if corruption in {"duplicate", "noncanonical"}:
        # Reach the exact ingress guard rather than failing only size metadata.
        conn.execute("UPDATE task_attachments SET size=? WHERE id=?", (path.stat().st_size, attachment))
        conn.commit()
    before = conn.total_changes
    with pytest.raises((ValueError, OSError)):
        read(conn, task, artifact, attachment)
    assert conn.total_changes == before


@pytest.mark.parametrize("field,value", [("size", 1), ("content_type", "text/plain"),
                                        ("stored_path", "/tmp/foreign.json")])
def test_metadata_substitution_is_refused(owner, field, value):
    conn, task, artifact, attachment = attach(owner)
    conn.execute(f"UPDATE task_attachments SET {field}=? WHERE id=?", (value, attachment))
    conn.commit()
    with pytest.raises((ValueError, OSError)):
        read(conn, task, artifact, attachment)


def test_attachment_reference_is_not_authority_and_missing_is_not_recreated(owner):
    conn, task, artifact, attachment = attach(owner)
    before = conn.total_changes
    with pytest.raises(ValueError):
        kb.read_task_mission_contract(conn, task, attachment,
                                     expected_contract_ref="contract:" + "0" * 64, board="default")
    with pytest.raises(ValueError):
        read(conn, task, artifact, attachment + 1000)
    assert conn.total_changes == before


def test_valid_foreign_body_cannot_be_rebound_to_a_different_task(owner):
    conn, task, artifact, attachment = attach(owner)
    other = kb.create_task(conn, title="foreign", board="default")
    foreign = mission(other)
    path = attachment_path(conn, attachment)
    path.write_bytes(foreign.canonical_bytes())
    conn.execute("UPDATE task_attachments SET size=? WHERE id=?", (path.stat().st_size, attachment))
    conn.commit()
    with pytest.raises(ValueError):
        read(conn, task, foreign, attachment)


@pytest.mark.parametrize("board", ["DEFAULT", " default", "default ", "", None])
def test_board_identity_is_never_silently_normalized(owner, board):
    conn, task, artifact, attachment = attach(owner)
    with pytest.raises(ValueError):
        read(conn, task, artifact, attachment, board=board)


def test_parent_symlink_is_not_an_admitted_attachment_root(owner):
    conn, task, artifact, attachment = attach(owner)
    path = attachment_path(conn, attachment)
    root = path.parent
    moved = root.with_name(root.name + "-moved")
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        read(conn, task, artifact, attachment)


@pytest.mark.parametrize("bad_task", [None, True, "", "../foreign"])
def test_malformed_task_is_a_typed_refusal_without_writes(owner, bad_task):
    from ares_runtime.collaboration import ContractError
    conn, task = owner
    before = conn.total_changes
    with pytest.raises(ContractError):
        kb.attach_task_mission_contract(conn, bad_task, mission(task), board="default")
    assert conn.total_changes == before


def test_invalid_typed_artifact_is_revalidated_before_write(owner):
    conn, task = owner
    valid = mission(task)
    forged = MissionContractV1({**valid.to_dict(), "objective": "tampered"}, "contract_digest")
    before = conn.total_changes
    with pytest.raises(ValueError):
        kb.attach_task_mission_contract(conn, task, forged, board="default")
    assert conn.total_changes == before
    assert kb.list_attachments(conn, task) == []
