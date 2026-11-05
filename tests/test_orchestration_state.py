"""Real SQLite tests for the session-owned orchestration policy record."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy
from hermes_state import SessionDB


def policy(mode="prefer_workers", *, delegation=True, graph=True, route=None):
    return OrchestrationPolicy(
        mode=mode,
        allow_delegation=delegation,
        allow_graph=graph,
        secondary_route=route,
    ).to_dict()


@pytest.fixture

def state_db(tmp_path):
    database_path = tmp_path / "state.db"
    database = SessionDB(db_path=database_path)
    database.create_session(
        "session-a",
        "cli",
        model="main-model-exact",
        model_config={"temperature": 0.25, "provider": "main-provider"},
    )
    database.create_session("session-b", "cli", model="other-main-model")
    try:
        yield database
    finally:
        database.close()


def _policy_row(database_path, session_id):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT revision, policy_json, origin, source_message_id "
            "FROM session_orchestration_policy WHERE session_id=?",
            (session_id,),
        ).fetchone()


def _main_route_row(database_path, session_id):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT model, model_config FROM sessions WHERE id=?", (session_id,)
        ).fetchone()


def test_missing_policy_is_absent_but_missing_session_is_a_typed_error(state_db):
    assert state_db.get_orchestration_policy("session-a") is None

    with pytest.raises(OrchestrationError) as caught:
        state_db.get_orchestration_policy("missing-session")

    assert caught.value.code == "session_not_found"


def test_policy_round_trip_survives_reopen_and_isolated_by_session(tmp_path):
    database_path = tmp_path / "state.db"
    first = SessionDB(db_path=database_path)
    first.create_session("session-a", "cli", model="main-a")
    first.create_session("session-b", "cli", model="main-b")
    first.set_orchestration_policy(
        "session-a",
        policy("direct", delegation=False, graph=True),
        expected_revision=0,
        origin="operator-command",
        source_message_id="message-17",
    )
    first.set_orchestration_policy(
        "session-b",
        policy("prefer_workers", delegation=True, graph=False),
        expected_revision=0,
        origin="operator-command",
    )
    first.close()

    reopened = SessionDB(db_path=database_path)
    try:
        record_a = reopened.get_orchestration_policy("session-a")
        record_b = reopened.get_orchestration_policy("session-b")

        assert record_a == {
            "session_id": "session-a",
            "revision": 1,
            "policy": policy("direct", delegation=False, graph=True),
            "origin": "operator-command",
            "source_message_id": "message-17",
        }
        assert record_b["policy"] == policy(
            "prefer_workers", delegation=True, graph=False
        )
        assert record_b["revision"] == 1
        assert record_b["source_message_id"] is None

        updated_a = reopened.set_orchestration_policy(
            "session-a",
            policy("prefer_workers", delegation=False, graph=False),
            expected_revision=1,
            origin="operator-command",
        )
        assert updated_a["revision"] == 2
        assert reopened.get_orchestration_policy("session-b") == record_b
    finally:
        reopened.close()


def test_policy_write_does_not_change_main_model_or_model_config(state_db):
    before_a = _main_route_row(state_db.db_path, "session-a")
    before_b = _main_route_row(state_db.db_path, "session-b")

    state_db.set_orchestration_policy(
        "session-a",
        {
            "schema": "orchestration.policy/v1",
            "mode": "prefer_workers",
            "allow_delegation": True,
            "allow_graph": True,
            "secondary_route": {
                "provider": "worker-provider",
                "model": "worker-model",
                "reasoning_effort": "xhigh",
                "service_tier": "priority",
            },
        },
        expected_revision=0,
        origin="test-control",
    )
    state_db.set_orchestration_policy(
        "session-b",
        policy("direct", delegation=False, graph=False),
        expected_revision=0,
        origin="test-control",
    )

    assert _main_route_row(state_db.db_path, "session-a") == before_a
    assert _main_route_row(state_db.db_path, "session-b") == before_b
    assert before_a[0] == "main-model-exact"
    assert json.loads(before_a[1]) == {
        "temperature": 0.25,
        "provider": "main-provider",
    }


def test_stale_revision_is_rejected_without_mutating_the_durable_record(state_db):
    initial = state_db.set_orchestration_policy(
        "session-a",
        policy("prefer_workers"),
        expected_revision=0,
        origin="first",
    )
    expected_row = _policy_row(state_db.db_path, "session-a")

    with pytest.raises(OrchestrationError) as caught:
        state_db.set_orchestration_policy(
            "session-a",
            policy("direct", delegation=False),
            expected_revision=0,
            origin="stale-writer",
        )

    assert caught.value.code == "revision_conflict"
    assert state_db.get_orchestration_policy("session-a") == initial
    assert _policy_row(state_db.db_path, "session-a") == expected_row


@pytest.mark.parametrize("expected_revision", [True, -1, 1.0, "0", None])
def test_revision_argument_requires_nonnegative_exact_integer(state_db, expected_revision):
    with pytest.raises(OrchestrationError) as caught:
        state_db.set_orchestration_policy(
            "session-a",
            policy(),
            expected_revision=expected_revision,
            origin="test",
        )

    assert caught.value.code == "revision_invalid"
    assert state_db.get_orchestration_policy("session-a") is None


def test_two_database_connections_racing_same_revision_commit_exactly_one_update(
    tmp_path,
):
    database_path = tmp_path / "state.db"
    owner = SessionDB(db_path=database_path)
    owner.create_session("session-race", "cli")
    owner.set_orchestration_policy(
        "session-race", policy("prefer_workers"), expected_revision=0, origin="seed"
    )
    peers = [SessionDB(db_path=database_path), SessionDB(db_path=database_path)]
    start = threading.Barrier(2)

    def update(peer_index):
        start.wait(timeout=5)
        try:
            result = peers[peer_index].set_orchestration_policy(
                "session-race",
                policy("direct" if peer_index == 0 else "prefer_workers", graph=False),
                expected_revision=1,
                origin=f"writer-{peer_index}",
            )
        except OrchestrationError as exc:
            return exc.code
        return result["revision"]

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, (0, 1)))

        assert sorted(outcomes, key=str) in ([2, "revision_conflict"], ["revision_conflict", 2])
        committed = owner.get_orchestration_policy("session-race")
        assert committed["revision"] == 2
        assert committed["origin"] in {"writer-0", "writer-1"}
        assert committed["policy"]["allow_graph"] is False
    finally:
        for peer in peers:
            peer.close()
        owner.close()


@pytest.mark.parametrize(
    "corrupt_value",
    [
        '{"schema":"orchestration.policy/v1","schema":"orchestration.policy/v1",'
        '"mode":"prefer_workers","allow_delegation":true,"allow_graph":true,'
        '"secondary_route":null}',
        "{not-json",
        "[]",
        '{"schema":"orchestration.policy/v9","mode":"prefer_workers",'
        '"allow_delegation":true,"allow_graph":true,"secondary_route":null}',
    ],
)
def test_corrupt_persisted_policy_fails_closed_and_cannot_be_overwritten(
    state_db, corrupt_value
):
    state_db.set_orchestration_policy(
        "session-a", policy(), expected_revision=0, origin="seed"
    )
    with sqlite3.connect(state_db.db_path) as connection:
        connection.execute(
            "UPDATE session_orchestration_policy SET policy_json=? WHERE session_id=?",
            (corrupt_value, "session-a"),
        )

    with pytest.raises(OrchestrationError) as caught:
        state_db.get_orchestration_policy("session-a")
    assert caught.value.code == "policy_corrupt"

    with pytest.raises(OrchestrationError) as caught:
        state_db.set_orchestration_policy(
            "session-a",
            policy("direct", delegation=False),
            expected_revision=1,
            origin="overwrite-attempt",
        )
    assert caught.value.code == "policy_corrupt"
    assert _policy_row(state_db.db_path, "session-a")[1] == corrupt_value


def test_foreign_and_prefix_only_session_ids_are_not_resolved_or_created(
    state_db, tmp_path
):
    foreign = SessionDB(db_path=tmp_path / "foreign.db")
    foreign.create_session("foreign-session-id", "cli")
    try:
        for unowned_id in ("foreign-session-id", "foreign-session"):
            with pytest.raises(OrchestrationError) as caught:
                state_db.set_orchestration_policy(
                    unowned_id,
                    policy(),
                    expected_revision=0,
                    origin="foreign-write",
                )
            assert caught.value.code == "session_not_found"
            with pytest.raises(OrchestrationError) as caught:
                state_db.get_orchestration_policy(unowned_id)
            assert caught.value.code == "session_not_found"

        with sqlite3.connect(state_db.db_path) as connection:
            rows = connection.execute(
                "SELECT session_id FROM session_orchestration_policy"
            ).fetchall()
        assert rows == []
    finally:
        foreign.close()


def test_unknown_policy_fields_are_rejected_before_any_sqlite_write(state_db):
    invalid_policy = policy()
    invalid_policy["api_key"] = "not-a-policy-field"

    with pytest.raises(OrchestrationError) as caught:
        state_db.set_orchestration_policy(
            "session-a",
            invalid_policy,
            expected_revision=0,
            origin="invalid-input",
        )

    assert caught.value.code == "policy_invalid"
    assert state_db.get_orchestration_policy("session-a") is None
    assert _policy_row(state_db.db_path, "session-a") is None
