"""SessionDB-owned WorkItem/Watch store tests against the real database."""

from __future__ import annotations

import json
import threading

import hermes_state_attention

import pytest

from ares_runtime.attention import (
    AttentionContractError,
    EventFactV1,
    WatchSpecV1,
    WatchState,
)
from ares_runtime.attention.records import (
    WatchRecordV1,
    WorkItemRecordV1,
    WorkItemState,
)
from hermes_state import SessionDB
from hermes_state_attention import (
    MAX_WATCHES_PER_WORK_ITEM,
    AttentionStoreError,
    attention_watch_key,
    attention_work_item_key,
)

T0 = 1_000
T1 = 2_000
EXPIRES = 10_000


def item(work_item_id="wi-1", now_ms=T0) -> WorkItemRecordV1:
    return WorkItemRecordV1.create(
        work_item_id=work_item_id,
        goal="Compare the revised agreement",
        criteria=("payment terms unchanged",),
        now_ms=now_ms,
    )


def watch(watch_id="w-1", work_item_id="wi-1", expires=EXPIRES) -> WatchRecordV1:
    return WatchRecordV1.create(
        WatchSpecV1(
            watch_id=watch_id,
            expectation_id=work_item_id,
            provider_id="synthetic",
            account_id="acct",
            object_type="message",
            event_type="arrived",
            required_facts=(EventFactV1("sender", "alex@example.test"),),
            not_before_ms=T0,
            expires_at_ms=expires,
            state=WatchState.ACTIVE,
        ),
        now_ms=T0,
    )


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield value
    finally:
        value.close()


def code_of(call) -> str:
    with pytest.raises(AttentionStoreError) as caught:
        call()
    return caught.value.code


# --- create / read / reopen -----------------------------------------------


def test_create_read_and_reopen_returns_identical_authored_records(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    created = first.create_work_item(item())
    created_watch = first.create_watch(watch(), now_ms=T0)
    first.close()

    second = SessionDB(db_path=path)
    try:
        assert second.read_work_item("wi-1") == created
        assert second.read_watch("w-1") == created_watch
        assert second.read_work_item("wi-1").criteria == ("payment terms unchanged",)
    finally:
        second.close()


def test_absence_is_distinct_from_corruption(db):
    assert code_of(lambda: db.read_work_item("nope")) == "WORK_ITEM_NOT_FOUND"
    assert code_of(lambda: db.read_watch("nope")) == "WATCH_NOT_FOUND"
    db.set_meta(attention_work_item_key("bad"), "{not json")
    assert code_of(lambda: db.read_work_item("bad")) == "RECORD_INVALID"
    db.set_meta(attention_work_item_key("odd"), '{"schema":"x"}')
    assert code_of(lambda: db.read_work_item("odd")) == "RECORD_INVALID"


def test_duplicate_create_is_refused_and_never_overwrites(db):
    original = db.create_work_item(item())
    other = WorkItemRecordV1.create(
        work_item_id="wi-1", goal="different goal", criteria=("x",), now_ms=T1
    )
    assert code_of(lambda: db.create_work_item(other)) == "ALREADY_EXISTS"
    assert db.read_work_item("wi-1") == original
    db.create_watch(watch(), now_ms=T0)
    assert code_of(lambda: db.create_watch(watch(), now_ms=T0)) == "ALREADY_EXISTS"


def test_create_refuses_non_initial_or_foreign_records(db):
    moved = item().transition(WorkItemState.PAUSED, now_ms=T1)
    assert code_of(lambda: db.create_work_item(moved)) == "WORK_ITEM_MUST_START_ACTIVE"
    assert (
        code_of(lambda: db.create_work_item({"work_item_id": "wi-1"}))
        == "INVALID_RECORD"
    )


def test_watch_requires_an_existing_live_work_item_and_unexpired_window(db):
    assert code_of(lambda: db.create_watch(watch(), now_ms=T0)) == "WORK_ITEM_NOT_FOUND"
    db.create_work_item(item())
    assert code_of(lambda: db.create_watch(watch(), now_ms=EXPIRES)) == "WATCH_EXPIRED"
    db.transition_work_item(
        "wi-1", WorkItemState.PAUSED, expected_revision=1, now_ms=T1
    )
    assert (
        code_of(lambda: db.create_watch(watch(), now_ms=T1)) == "WORK_ITEM_NOT_ACTIVE"
    )


@pytest.mark.parametrize("bad", ["", "a:b", "a%b", "a_b\n", "x" * 257])
def test_identifiers_cannot_alias_keys_or_wildcards(db, bad):
    with pytest.raises(AttentionStoreError) as caught:
        attention_work_item_key(bad)
    assert caught.value.code == "INVALID_IDENTIFIER"
    with pytest.raises(AttentionStoreError):
        attention_watch_key("wi-1", bad)
    with pytest.raises(AttentionStoreError):
        attention_watch_key(bad, "w-1")


def test_work_item_and_watch_namespaces_do_not_collide(db):
    db.create_work_item(item("same"))
    db.create_watch(watch("same", "same"), now_ms=T0)
    assert db.read_work_item("same").work_item_id == "same"
    assert db.read_watch("same").watch_id == "same"
    assert attention_work_item_key("same") != attention_watch_key("same", "same")


def test_listing_is_namespaced_deterministic_and_surfaces_corruption(db):
    for name in ("wi-b", "wi-a", "wi-c"):
        db.create_work_item(item(name))
    db.set_meta("unrelated:wi-z", "x")
    db.set_meta("goal:wi-y", "x")
    assert [r.work_item_id for r in db.list_work_items()] == ["wi-a", "wi-b", "wi-c"]
    db.set_meta(attention_work_item_key("wi-bad"), "garbage")
    assert code_of(db.list_work_items) == "RECORD_INVALID"


# --- CAS / lifecycle -------------------------------------------------------


def test_transition_requires_the_expected_revision(db):
    db.create_work_item(item())
    assert (
        code_of(
            lambda: db.transition_work_item(
                "wi-1", WorkItemState.PAUSED, expected_revision=7, now_ms=T1
            )
        )
        == "STALE_REVISION"
    )
    assert db.read_work_item("wi-1").revision == 1
    paused = db.transition_work_item(
        "wi-1", WorkItemState.PAUSED, expected_revision=1, now_ms=T1
    )
    assert (paused.state, paused.revision) == (WorkItemState.PAUSED, 2)


def test_illegal_transition_leaves_the_stored_record_unchanged(db):
    db.create_work_item(item())
    db.transition_work_item(
        "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
    )
    before = db.get_meta(attention_work_item_key("wi-1"))
    assert (
        code_of(
            lambda: db.transition_work_item(
                "wi-1", WorkItemState.ACTIVE, expected_revision=2, now_ms=T1 + 1
            )
        )
        == "ILLEGAL_TRANSITION"
    )
    assert db.get_meta(attention_work_item_key("wi-1")) == before


def test_cancelling_a_work_item_cancels_its_watches_atomically(db):
    db.create_work_item(item())
    db.create_work_item(item("wi-2"))
    db.create_watch(watch("w-1"), now_ms=T0)
    db.create_watch(watch("w-2"), now_ms=T0)
    db.create_watch(watch("w-other", "wi-2"), now_ms=T0)
    db.transition_work_item(
        "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
    )
    assert db.read_watch("w-1").state is WatchState.CANCELLED
    assert db.read_watch("w-2").state is WatchState.CANCELLED
    assert db.read_watch("w-other").state is WatchState.ACTIVE  # other item untouched


def test_cancel_cascade_tolerates_an_already_cancelled_watch(db):
    db.create_work_item(item())
    db.create_watch(watch("w-done"), now_ms=T0)
    db.create_watch(watch("w-live"), now_ms=T0)
    db.transition_watch("w-done", WatchState.CANCELLED, expected_revision=1, now_ms=T1)
    db.transition_work_item(
        "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1 + 1
    )
    done = db.read_watch("w-done")
    assert (done.state, done.revision, done.updated_at_ms) == (
        WatchState.CANCELLED,
        2,
        T1,
    )  # untouched: not re-cancelled, not re-versioned
    assert db.read_watch("w-live").state is WatchState.CANCELLED


def test_cascade_failure_publishes_nothing(db, monkeypatch):
    db.create_work_item(item())
    db.create_watch(watch("w-1"), now_ms=T0)
    db.create_watch(watch("w-2"), now_ms=T0)
    before = {
        key: db.get_meta(key)
        for key in (
            attention_work_item_key("wi-1"),
            attention_watch_key("wi-1", "w-1"),
            attention_watch_key("wi-1", "w-2"),
        )
    }
    real = SessionDB._attention_cas_on_conn
    calls = {"n": 0}

    def flaky(self, conn, key, previous_text, new_text):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected failure after two writes")
        return real(self, conn, key, previous_text, new_text)

    monkeypatch.setattr(SessionDB, "_attention_cas_on_conn", flaky)
    with pytest.raises(RuntimeError):
        db.transition_work_item(
            "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
        )
    assert calls["n"] == 3
    assert {key: db.get_meta(key) for key in before} == before


def test_pause_does_not_cascade_and_stale_watch_write_is_refused(db):
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    evaluated = db.read_watch("w-1")  # a worker evaluates this revision...
    db.transition_watch("w-1", WatchState.PAUSED, expected_revision=1, now_ms=T1)
    # ...and its later write against the revision it read is fenced off.
    assert (
        code_of(
            lambda: db.transition_watch(
                "w-1",
                WatchState.CANCELLED,
                expected_revision=evaluated.revision,
                now_ms=T1 + 1,
            )
        )
        == "STALE_REVISION"
    )
    assert db.read_watch("w-1").state is WatchState.PAUSED


def test_watch_resume_after_expiry_is_refused_and_window_is_unchanged(db):
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    db.transition_watch("w-1", WatchState.PAUSED, expected_revision=1, now_ms=T1)
    assert (
        code_of(
            lambda: db.transition_watch(
                "w-1", WatchState.ACTIVE, expected_revision=2, now_ms=EXPIRES
            )
        )
        == "WATCH_EXPIRED"
    )
    assert db.read_watch("w-1").expires_at_ms == EXPIRES


def test_watch_cannot_resume_under_a_paused_or_cancelled_work_item(db):
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    db.transition_watch("w-1", WatchState.PAUSED, expected_revision=1, now_ms=T1)
    db.transition_work_item(
        "wi-1", WorkItemState.PAUSED, expected_revision=1, now_ms=T1
    )
    assert (
        code_of(
            lambda: db.transition_watch(
                "w-1", WatchState.ACTIVE, expected_revision=2, now_ms=T1 + 1
            )
        )
        == "WORK_ITEM_NOT_ACTIVE"
    )


def test_active_watch_specs_exclude_paused_cancelled_expired_and_inactive_parents(db):
    for name in ("wi-live", "wi-paused"):
        db.create_work_item(item(name))
    db.create_watch(watch("w-live", "wi-live"), now_ms=T0)
    db.create_watch(watch("w-short", "wi-live", expires=T1), now_ms=T0)
    db.create_watch(watch("w-paused-w", "wi-live"), now_ms=T0)
    db.create_watch(watch("w-parent", "wi-paused"), now_ms=T0)
    db.transition_watch(
        "w-paused-w", WatchState.PAUSED, expected_revision=1, now_ms=T1 - 1
    )
    db.transition_work_item(
        "wi-paused", WorkItemState.PAUSED, expected_revision=1, now_ms=T1 - 1
    )
    specs = db.list_active_watch_specs(now_ms=T1)
    assert [s.watch_id for s in specs] == ["w-live"]
    assert all(type(s) is WatchSpecV1 for s in specs)


# --- concurrency -----------------------------------------------------------


def _race(first, second, action):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(database, label):
        barrier.wait(timeout=5)
        try:
            action(database, label)
            outcomes.append("won")
        except AttentionStoreError as error:
            outcomes.append(error.code)

    threads = [
        threading.Thread(target=attempt, args=(first, "a")),
        threading.Thread(target=attempt, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    return outcomes


def test_two_connections_race_create_exactly_one_wins(tmp_path):
    path = tmp_path / "state.db"
    first, second = SessionDB(db_path=path), SessionDB(db_path=path)
    try:
        outcomes = _race(
            first, second, lambda database, _: database.create_work_item(item())
        )
        assert sorted(outcomes) == ["ALREADY_EXISTS", "won"]
    finally:
        first.close()
        second.close()


def test_two_connections_race_same_revision_transition_exactly_one_wins(tmp_path):
    path = tmp_path / "state.db"
    first, second = SessionDB(db_path=path), SessionDB(db_path=path)
    try:
        first.create_work_item(item())

        def act(database, label):
            target = WorkItemState.PAUSED if label == "a" else WorkItemState.CANCELLED
            database.transition_work_item(
                "wi-1", target, expected_revision=1, now_ms=T1
            )

        outcomes = _race(first, second, act)
        assert sorted(outcomes) == ["STALE_REVISION", "won"]
        assert first.read_work_item("wi-1").revision == 2
    finally:
        first.close()
        second.close()


# --- bounds and ownership boundaries ---------------------------------------


def test_per_work_item_watch_cap_fails_closed_and_isolated_to_that_item(db):
    db.create_work_item(item())
    db.create_work_item(item("wi-2"))
    for index in range(MAX_WATCHES_PER_WORK_ITEM):
        db.create_watch(watch(f"w-{index:03d}"), now_ms=T0)
    assert (
        code_of(lambda: db.create_watch(watch("w-over"), now_ms=T0)) == "LIMIT_EXCEEDED"
    )
    assert len(db.list_watches("wi-1")) == MAX_WATCHES_PER_WORK_ITEM  # nothing dropped
    db.create_watch(watch("w-other", "wi-2"), now_ms=T0)  # other items unaffected


def test_global_work_item_cap_fails_closed_without_dropping(db, monkeypatch):
    monkeypatch.setattr(hermes_state_attention, "MAX_WORK_ITEMS", 3)
    for name in ("wi-a", "wi-b", "wi-c"):
        db.create_work_item(item(name))
    assert code_of(lambda: db.create_work_item(item("wi-d"))) == "LIMIT_EXCEEDED"
    assert [r.work_item_id for r in db.list_work_items()] == ["wi-a", "wi-b", "wi-c"]


def test_global_watch_cap_fails_closed_without_dropping(db, monkeypatch):
    monkeypatch.setattr(hermes_state_attention, "MAX_WATCHES", 3)
    for name in ("wi-a", "wi-b"):
        db.create_work_item(item(name))
    db.create_watch(watch("w-1", "wi-a"), now_ms=T0)
    db.create_watch(watch("w-2", "wi-a"), now_ms=T0)
    db.create_watch(watch("w-3", "wi-b"), now_ms=T0)
    assert (
        code_of(lambda: db.create_watch(watch("w-4", "wi-b"), now_ms=T0))
        == "LIMIT_EXCEEDED"
    )
    assert [w.watch_id for w in db.list_watches()] == ["w-1", "w-2", "w-3"]


def test_two_connections_cannot_both_take_the_last_work_item_slot(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hermes_state_attention, "MAX_WORK_ITEMS", 1)
    path = tmp_path / "state.db"
    first, second = SessionDB(db_path=path), SessionDB(db_path=path)
    try:
        outcomes = _race(
            first,
            second,
            lambda database, label: database.create_work_item(item(f"wi-{label}")),
        )
        assert sorted(outcomes) == ["LIMIT_EXCEEDED", "won"]
        assert len(first.list_work_items()) == 1
    finally:
        first.close()
        second.close()


def test_two_connections_cannot_both_take_the_last_watch_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state_attention, "MAX_WATCHES", 1)
    path = tmp_path / "state.db"
    first, second = SessionDB(db_path=path), SessionDB(db_path=path)
    try:
        first.create_work_item(item())
        outcomes = _race(
            first,
            second,
            lambda database, label: database.create_watch(
                watch(f"w-{label}"), now_ms=T0
            ),
        )
        assert sorted(outcomes) == ["LIMIT_EXCEEDED", "won"]
        assert len(first.list_watches()) == 1
    finally:
        first.close()
        second.close()


def test_watch_id_is_unique_across_work_items(db):
    db.create_work_item(item("wi-1"))
    db.create_work_item(item("wi-2"))
    db.create_watch(watch("w-1", "wi-1"), now_ms=T0)
    assert (
        code_of(lambda: db.create_watch(watch("w-1", "wi-2"), now_ms=T0))
        == "ALREADY_EXISTS"
    )
    assert db.read_watch("w-1").expectation_id == "wi-1"
    assert db.list_watches("wi-2") == []


# --- availability: a corrupt row blocks only what must touch that row ------


def test_unrelated_corrupt_watch_does_not_block_create_or_cancel(db):
    db.create_work_item(item("wi-bad"))
    db.create_work_item(item("wi-good"))
    db.create_watch(watch("w-bad", "wi-bad"), now_ms=T0)
    db.create_watch(watch("w-good", "wi-good"), now_ms=T0)
    db.set_meta(attention_watch_key("wi-bad", "w-bad"), "garbage")

    db.create_watch(watch("w-new", "wi-good"), now_ms=T0)  # creation still works
    cancelled = db.transition_work_item(
        "wi-good", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
    )
    assert cancelled.state is WorkItemState.CANCELLED
    assert db.read_watch("w-good").state is WatchState.CANCELLED
    assert db.read_watch("w-new").state is WatchState.CANCELLED
    assert db.get_meta(attention_watch_key("wi-bad", "w-bad")) == "garbage"


def test_corrupt_child_still_blocks_its_own_parents_cancellation_atomically(db):
    db.create_work_item(item())
    db.create_watch(watch("w-ok"), now_ms=T0)
    db.create_watch(watch("w-bad"), now_ms=T0)
    db.set_meta(attention_watch_key("wi-1", "w-bad"), "garbage")
    before = db.get_meta(attention_work_item_key("wi-1"))
    assert (
        code_of(
            lambda: db.transition_work_item(
                "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
            )
        )
        == "RECORD_INVALID"
    )
    # Nothing was published: not the parent, not the healthy sibling.
    assert db.get_meta(attention_work_item_key("wi-1")) == before
    assert db.read_watch("w-ok").state is WatchState.ACTIVE


def test_watch_key_under_the_wrong_parent_is_refused(db):
    db.create_work_item(item("wi-1"))
    db.create_work_item(item("wi-2"))
    db.create_watch(watch("w-1", "wi-1"), now_ms=T0)
    stored = db.get_meta(attention_watch_key("wi-1", "w-1"))
    db.set_meta(attention_watch_key("wi-2", "w-1"), stored)  # parent mismatch
    assert code_of(lambda: db.list_watches("wi-2")) == "RECORD_INVALID"


# --- ownership boundaries (real SessionDB behavior, not key-prefix checks) --


def test_session_pruning_deletes_sessions_but_never_attention_records(db):
    work = db.create_work_item(item())
    watch_record = db.create_watch(watch(), now_ms=T0)
    db.create_session("s-old", source="cli")
    db.end_session("s-old", "done")
    assert db.get_session("s-old") is not None
    removed = db.prune_sessions(older_than_days=0, source="cli")
    assert removed >= 1  # the prune had a real candidate
    assert db.get_session("s-old") is None
    assert db.read_work_item("wi-1") == work
    assert db.read_watch("w-1") == watch_record


def test_foreign_namespaces_neither_leak_in_nor_get_modified(db):
    foreign = {
        "goal:s-1": "not json",
        "execution:turn:1": "{}",
        "loop:s-1": "x",
        "attention:work_item": "no colon",
        "attention:work_items:x": "close prefix",
        "attention:watching:x": "close prefix",
        "attention:work_item;x": "just past the work_item range",
        "attention:watch;x": "just past the watch range",
    }
    for key, value in foreign.items():
        db.set_meta(key, value)
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    db.transition_work_item(
        "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
    )
    assert [r.work_item_id for r in db.list_work_items()] == ["wi-1"]
    assert [w.watch_id for w in db.list_watches()] == ["w-1"]
    assert db.list_active_watch_specs(now_ms=T1) == []
    for key, value in foreign.items():
        assert db.get_meta(key) == value


def test_identifiers_with_separators_cannot_reach_the_store(db):
    with pytest.raises(AttentionContractError) as caught:
        item("goal:session")
    assert caught.value.code == "INVALID_IDENTIFIER"
    assert attention_watch_key("a", "b") == "attention:watch:a:b"


# --- backup / restore through the repo's own helpers -----------------------


def test_backup_snapshot_carries_attention_records_exactly(tmp_path):
    from hermes_cli.backup import copy_db_and_verify

    live_path = tmp_path / "live" / "state.db"
    live_path.parent.mkdir()
    live = SessionDB(db_path=live_path)
    try:
        work = live.create_work_item(item())
        stored_watch = live.create_watch(watch(), now_ms=T0)
        snapshot = tmp_path / "snapshot.db"
        assert copy_db_and_verify(live_path, snapshot)
    finally:
        live.close()

    restored = SessionDB(db_path=snapshot)
    try:
        assert restored.read_work_item("wi-1") == work
        assert restored.read_watch("w-1") == stored_watch
        # Strict decode still passes on the copied bytes (canonical form kept).
        assert [w.watch_id for w in restored.list_active_watch_specs(now_ms=T1)] == [
            "w-1"
        ]
    finally:
        restored.close()


def test_restore_over_a_live_database_replaces_attention_state(tmp_path):
    from hermes_cli.backup import _safe_restore_db, copy_db_and_verify

    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    snapshot = tmp_path / "snapshot.db"
    assert copy_db_and_verify(path, snapshot)

    # State drifts after the snapshot: the item is cancelled (cascading) and a
    # second item appears.  Restoring must roll all of it back, atomically.
    db.transition_work_item(
        "wi-1", WorkItemState.CANCELLED, expected_revision=1, now_ms=T1
    )
    db.create_work_item(item("wi-2"))
    db.close()

    assert _safe_restore_db(snapshot, path)
    reopened = SessionDB(db_path=path)
    try:
        assert reopened.read_work_item("wi-1").state is WorkItemState.ACTIVE
        assert reopened.read_watch("w-1").state is WatchState.ACTIVE
        assert code_of(lambda: reopened.read_work_item("wi-2")) == "WORK_ITEM_NOT_FOUND"
    finally:
        reopened.close()


# --- strict decode: shapes that parse but must not be trusted --------------


def _stored(db, work_item_id="wi-1"):
    return db.get_meta(attention_work_item_key(work_item_id))


def test_valid_but_noncanonical_json_is_refused(db):
    db.create_work_item(item())
    pretty = json.dumps(json.loads(_stored(db)), indent=2)
    db.set_meta(attention_work_item_key("wi-1"), pretty)
    assert code_of(lambda: db.read_work_item("wi-1")) == "RECORD_INVALID"
    assert code_of(db.list_work_items) == "RECORD_INVALID"


def test_duplicate_json_keys_and_nan_constants_are_refused(db):
    db.create_work_item(item())
    text = _stored(db)
    dup = text[:-1] + ',"revision":9}'
    db.set_meta(attention_work_item_key("wi-1"), dup)
    assert code_of(lambda: db.read_work_item("wi-1")) == "RECORD_INVALID"
    db.set_meta(
        attention_work_item_key("wi-1"), text.replace('"revision":1', '"revision":NaN')
    )
    assert code_of(lambda: db.read_work_item("wi-1")) == "RECORD_INVALID"


def test_record_stored_under_another_identifier_is_refused(db):
    db.create_work_item(item("wi-1"))
    db.set_meta(attention_work_item_key("wi-2"), _stored(db, "wi-1"))
    assert code_of(lambda: db.read_work_item("wi-2")) == "RECORD_INVALID"
    db.create_watch(watch(), now_ms=T0)
    db.set_meta(
        attention_watch_key("wi-1", "w-2"),
        db.get_meta(attention_watch_key("wi-1", "w-1")),
    )
    assert code_of(lambda: db.read_watch("w-2")) == "RECORD_INVALID"


def test_corrupt_record_blocks_its_own_mutation_and_leaves_bytes_unchanged(db):
    db.create_work_item(item())
    db.set_meta(attention_work_item_key("wi-1"), "{broken")
    assert (
        code_of(
            lambda: db.transition_work_item(
                "wi-1", WorkItemState.PAUSED, expected_revision=1, now_ms=T1
            )
        )
        == "RECORD_INVALID"
    )
    assert _stored(db) == "{broken"


def test_corrupt_watch_blocks_active_listing_instead_of_being_skipped(db):
    db.create_work_item(item())
    db.create_watch(watch(), now_ms=T0)
    db.set_meta(attention_watch_key("wi-1", "w-1"), "garbage")
    assert code_of(lambda: db.list_active_watch_specs(now_ms=T1)) == "RECORD_INVALID"


def test_list_watches_filters_by_work_item_and_validates_the_filter(db):
    db.create_work_item(item("wi-1"))
    db.create_work_item(item("wi-2"))
    db.create_watch(watch("w-a", "wi-1"), now_ms=T0)
    db.create_watch(watch("w-b", "wi-2"), now_ms=T0)
    assert [w.watch_id for w in db.list_watches("wi-1")] == ["w-a"]
    assert [w.watch_id for w in db.list_watches()] == ["w-a", "w-b"]
    assert code_of(lambda: db.list_watches("bad id")) == "INVALID_IDENTIFIER"


def test_invalid_timestamps_and_revisions_are_typed_refusals(db):
    db.create_work_item(item())
    assert code_of(lambda: db.list_active_watch_specs(now_ms=-1)) == "INVALID_TIMESTAMP"
    for bad in (0, -1, True, "1", None):
        assert (
            code_of(
                lambda bad=bad: db.transition_work_item(
                    "wi-1", WorkItemState.PAUSED, expected_revision=bad, now_ms=T1
                )
            )
            == "INVALID_REVISION"
        )
