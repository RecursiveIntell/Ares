"""Exact physical copy correspondence, not todo eligibility or authority."""
import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        store.create_session("source", source="test")
        yield store


def seed(store):
    boundary = store.append_message("source", "user", "prefix")
    rows = [
        {"role": "user", "content": "identical", "timestamp": 123.0},
        {"role": "user", "content": "identical", "timestamp": 123.0},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "reused", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
        ], "reasoning": "sidecar", "display_metadata": {"test": True}},
        {"role": "tool", "content": "result", "tool_call_id": "reused", "tool_name": "todo"},
    ]
    store.append_messages_batch("source", rows)
    return boundary, [row["_row_id"] for row in rows]


def compact(store, kind, boundary, *, ceiling=None):
    summary = [{"role": "user", "content": "summary"}]
    if kind == "in_place_tail":
        store.archive_and_compact("source", summary, watermark=boundary)
        return "source", summary[0]["_row_id"]
    assert store.try_acquire_compression_lock("source", "copy-test", ttl_seconds=60)
    store.publish_compression_child(
        parent_session_id="source", child_session_id="child", source="test",
        messages=summary, watermark=boundary, watermark_ceiling=ceiling,
        compression_lock_holder="copy-test",
    )
    return "child", summary[0]["_row_id"]


def require_relation(store):
    with store._read_ctx() as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_copy_edges'"
        ).fetchone()
    assert present, "canonical copy committed without durable exact row correspondence"


def origin(store, session, row_id):
    return store.get_message_copy_origin(session, row_id)


@pytest.mark.parametrize("kind", ["in_place_tail", "compression_child_tail"])
def test_real_copy_retains_ordered_exact_correspondence_after_reopen(db, kind):
    boundary, source_ids = seed(db)
    destination, summary_id = compact(db, kind, boundary)
    require_relation(db)
    copied = [row for row in db.get_messages(destination) if row["id"] > summary_id]
    assert len(copied) == len(source_ids)
    assert origin(db, destination, summary_id) is None
    with db._read_ctx() as conn:
        for source_id, dest in zip(source_ids, copied):
            edge = origin(db, destination, dest["id"])
            assert edge == {
                "schema_version": 1, "copy_kind": kind,
                "source_session_id": "source", "source_message_id": source_id,
                "destination_session_id": destination, "destination_message_id": dest["id"],
            }
            before = dict(conn.execute("SELECT * FROM messages WHERE id=?", (source_id,)).fetchone())
            after = dict(conn.execute("SELECT * FROM messages WHERE id=?", (dest["id"],)).fetchone())
            excluded = {"id", "session_id", "active", "compacted"}
            assert {k: v for k, v in before.items() if k not in excluded} == {
                k: v for k, v in after.items() if k not in excluded
            }
            assert after["active"] == 1 and after["compacted"] == 0
    with SessionDB(db.db_path, read_only=True) as reopened:
        for source_id, dest in zip(source_ids, copied):
            assert origin(reopened, destination, dest["id"])["source_message_id"] == source_id


def test_rotation_ceiling_excludes_own_flush_from_mapping(db):
    boundary, source_ids = seed(db)
    ceiling = source_ids[-1]
    db.append_message("source", "user", "own late flush")
    destination, summary_id = compact(db, "compression_child_tail", boundary, ceiling=ceiling)
    require_relation(db)
    copied = [r for r in db.get_messages(destination) if r["id"] > summary_id]
    assert [origin(db, destination, row["id"])["source_message_id"] for row in copied] == source_ids
    assert not any(row["content"] == "own late flush" for row in copied)


def test_imported_ids_parentage_and_lookalike_text_never_create_edges(db):
    boundary, _ = seed(db)
    compact(db, "in_place_tail", boundary)
    require_relation(db)
    copied_id = db.get_messages("source")[-1]["id"]
    db.create_session("unrelated", source="test", parent_session_id="source")
    imported = [{"role": "user", "content": '{"copy_kind":"in_place_tail"}', "_row_id": copied_id}]
    db.append_messages_batch("unrelated", imported)
    assert origin(db, "unrelated", imported[0]["_row_id"]) is None
    assert origin(db, "unrelated", copied_id) is None
    assert origin(db, "source", boundary) is None


def test_repeated_compaction_preserves_one_hop_identity_not_content_inference(db):
    boundary, original_ids = seed(db)
    _, first_summary = compact(db, "in_place_tail", boundary)
    require_relation(db)
    middle_ids = [r["id"] for r in db.get_messages("source") if r["id"] > first_summary]
    _, second_summary = compact(db, "in_place_tail", first_summary)
    final_ids = [r["id"] for r in db.get_messages("source") if r["id"] > second_summary]
    assert [origin(db, "source", i)["source_message_id"] for i in final_ids] == middle_ids
    assert [origin(db, "source", i)["source_message_id"] for i in middle_ids] == original_ids


@pytest.mark.parametrize("kind", ["in_place_tail", "compression_child_tail"])
def test_mapping_failure_rolls_back_whole_owner_transition(db, kind):
    boundary, _ = seed(db)
    require_relation(db)
    before = db.get_messages("source", include_inactive=True)
    before_session = db.get_session("source")
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER deny_copy BEFORE INSERT ON message_copy_edges "
        "BEGIN SELECT RAISE(ABORT, 'injected mapping failure'); END"
    ))
    with pytest.raises(sqlite3.IntegrityError, match="injected mapping failure"):
        compact(db, kind, boundary)
    assert db.get_messages("source", include_inactive=True) == before
    assert db.get_session("source")["ended_at"] is None
    assert db.get_session("child") is None
    assert db.get_session("source") == before_session
    with db._read_ctx() as conn:
        assert conn.execute("SELECT count(*) FROM message_copy_edges").fetchone()[0] == 0


@pytest.mark.parametrize("change,reason", [
    ("schema_version=99", "schema"),
    ("copy_kind='import'", "kind"),
    ("source_session_id='unrelated'", "scope"),
    ("destination_session_id='unrelated'", "scope"),
    ("copy_kind='compression_child_tail'", "scope"),
])
def test_invalid_latest_edge_is_refused_not_rematched(db, change, reason):
    boundary, _ = seed(db)
    compact(db, "in_place_tail", boundary)
    require_relation(db)
    dest = db.get_messages("source")[-1]["id"]
    db._execute_write(lambda conn: conn.execute(
        f"UPDATE message_copy_edges SET {change} WHERE destination_message_id=?", (dest,)
    ))
    with pytest.raises(hermes_state.MessageCopyError) as exc:
        origin(db, "source", dest)
    assert exc.value.reason == reason


def test_copy_edges_do_not_prevent_existing_owner_message_deletion(db):
    boundary, _ = seed(db)
    compact(db, "in_place_tail", boundary)
    require_relation(db)
    db.replace_messages("source", [{"role": "user", "content": "replacement"}])
    with db._read_ctx() as conn:
        assert conn.execute("SELECT count(*) FROM message_copy_edges").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_read_schema_is_explicit_not_unmapped(db):
    require_relation(db)
    db._execute_write(lambda conn: conn.execute("DROP TABLE message_copy_edges"))
    with SessionDB(db.db_path, read_only=True) as legacy:
        with pytest.raises(hermes_state.MessageCopyError) as exc:
            origin(legacy, "source", 1)
        assert exc.value.reason == "unsupported_schema"


@pytest.mark.parametrize("kind", ["in_place_tail", "compression_child_tail"])
def test_insert_trigger_gaps_do_not_become_copy_destinations(db, kind):
    boundary, source_ids = seed(db)
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER insert_gap AFTER INSERT ON messages "
        "WHEN NEW.content IS NOT 'trigger gap' BEGIN "
        "INSERT INTO messages(session_id,role,content,timestamp,active,compacted) "
        "VALUES(NEW.session_id,'user','trigger gap',0,0,0); END"
    ))
    destination, summary_id = compact(db, kind, boundary)
    copied = [r for r in db.get_messages(destination) if r["id"] > summary_id]
    assert [origin(db, destination, r["id"])["source_message_id"] for r in copied] == source_ids
    gaps = [r for r in db.get_messages(destination, include_inactive=True) if r["content"] == "trigger gap"]
    assert gaps
    assert all(origin(db, destination, r["id"]) is None for r in gaps)


@pytest.mark.parametrize("column", ["source_message_id", "destination_message_id"])
def test_dangling_legacy_edge_is_explicit_corruption(db, column):
    boundary, _ = seed(db)
    compact(db, "in_place_tail", boundary)
    dest = db.get_messages("source")[-1]["id"]
    with db._read_ctx() as conn:
        row_id = conn.execute(
            f"SELECT {column} FROM message_copy_edges WHERE destination_message_id=?", (dest,)
        ).fetchone()[0]
    # Emulate a legacy/corrupt writer with FK enforcement disabled, scratch only.
    with sqlite3.connect(db.db_path) as corrupt:
        corrupt.execute("DELETE FROM messages WHERE id=?", (row_id,))
    with pytest.raises(hermes_state.MessageCopyError) as exc:
        origin(db, "source", dest)
    assert exc.value.reason == "scope"


def test_malformed_read_only_copy_schema_is_explicit(db):
    db._execute_write(lambda conn: conn.execute("DROP TABLE message_copy_edges"))
    db._execute_write(lambda conn: conn.execute(
        "CREATE TABLE message_copy_edges(destination_message_id INTEGER PRIMARY KEY)"
    ))
    with SessionDB(db.db_path, read_only=True) as malformed:
        with pytest.raises(hermes_state.MessageCopyError) as exc:
            origin(malformed, "source", 1)
        assert exc.value.reason == "unsupported_schema"


def test_schema_upgrade_does_not_infer_edges_for_legacy_copies(tmp_path):
    path = tmp_path / "legacy.db"
    with SessionDB(path) as old:
        old.create_session("source", source="test")
        boundary, _ = seed(old)
        compact(old, "in_place_tail", boundary)
        old._execute_write(lambda conn: conn.execute("DROP TABLE message_copy_edges"))
    with SessionDB(path) as upgraded:
        require_relation(upgraded)
        assert all(origin(upgraded, "source", r["id"]) is None for r in upgraded.get_messages("source"))
