"""Turn-owned private custody handles over real SessionDB primitives."""
from dataclasses import asdict
import hashlib
import importlib
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint
from scripts.run_checkpoint_claim import ClaimOutcomeUnknown, ClaimRefusal


def sha(value):
    return hashlib.sha256(value).hexdigest()


def owner_class():
    assert importlib.util.find_spec("agent.run_checkpoint_custody"), "turn custody owner missing"
    return importlib.import_module("agent.run_checkpoint_custody").TurnRunCustody


def assert_unknown(operation):
    try:
        operation()
    except ClaimOutcomeUnknown:
        return
    except BaseException as exc:
        pytest.fail(f"Unclassified native outcome: {type(exc).__name__}")
    pytest.fail("Expected an unknown native outcome")


@pytest.fixture
def fixture(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("current", source="desktop")
    assert db.try_acquire_session_turn_lease("current", "holder", ttl_seconds=120)
    goal = '{"status":"cleared","outcome":"CANCELLED"}'
    db.set_meta("goal:old", goal)
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    inventory = json.dumps({str(source): sha(source.read_bytes())}).encode()
    files = {"members": {}}
    for name, data in {"plan": b"plan", "contract": b"contract", "source": inventory}.items():
        path = tmp_path / name
        path.write_bytes(data)
        files[name] = str(path)
    members = (("historical-goal-key", "goal:old"), ("scope", "no effects"))
    for name, text in members:
        path = tmp_path / (name + ".txt")
        path.write_text(text)
        files["members"][name] = str(path)
    cp = RunCheckpoint(sha(b"plan"), sha(b"contract"), sha(inventory), "observe", members,
                       ("effect:unknown",), ("finding:open",), ("no activation",))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"checkpoint": asdict(cp), "files": files,
                                  "session_id": "current", "lease_holder": "holder"}))
    args = dict(run_id="turn-fixture", expected_generation=0, session_id="current",
                request_path=str(request), expected_request_digest=sha(request.read_bytes()),
                origin_session_id="old", historical_goal_digest=sha(goal.encode()), ttl_seconds=120)
    yield db, args, goal
    db.close()


def started(fixture):
    db, args, _ = fixture
    owner = owner_class()(db)
    owner.begin_turn("holder")
    return owner, args


def test_claim_refresh_finish_releases_private_handle_and_preserves_history(fixture):
    db, _, goal = fixture
    owner, args = started(fixture)
    response = owner.claim("holder", **args)
    assert response["generation"] == 1
    native = db.read_run_custody(args["run_id"])
    assert native.owner_token not in json.dumps(response)
    assert native.owner_token not in repr(owner)
    refreshed = owner.refresh("holder", run_id=args["run_id"], expected_generation=1, ttl_seconds=120)
    assert refreshed["generation"] == 2
    assert refreshed["resume_authorized"] is False
    assert owner.finish_turn("holder") == []
    released = db.read_run_custody(args["run_id"])
    assert released.disposition == "released"
    assert released.generation == 3
    assert released.checkpoint == native.checkpoint
    assert db.get_meta("goal:old") == goal
    assert owner.finish_turn("holder") == []
    assert db.read_run_custody(args["run_id"]) == released
    with pytest.raises(ClaimRefusal, match="TURN_NOT_ACTIVE"):
        owner.claim("holder", **args)


def test_explicit_release_and_new_turn_reclaim_without_process_restart(fixture):
    db, _, _ = fixture
    owner, args = started(fixture)
    owner.claim("holder", **args)
    first = db.read_run_custody(args["run_id"])
    assert owner.release("holder", run_id=args["run_id"], expected_generation=1)["generation"] == 2
    assert owner.finish_turn("holder") == []
    db.release_session_turn_lease("current", "holder")
    assert db.try_acquire_session_turn_lease("current", "next-holder", ttl_seconds=120)
    from pathlib import Path
    request = Path(args["request_path"])
    value = json.loads(request.read_text())
    value["lease_holder"] = "next-holder"
    request.write_text(json.dumps(value))
    owner.begin_turn("next-holder")
    response = owner.claim("next-holder", **{**args, "expected_generation": 2,
                           "expected_request_digest": sha(request.read_bytes())})
    assert response["generation"] == 3
    assert db.read_run_custody(args["run_id"]).owner_token != first.owner_token
    with pytest.raises(ClaimRefusal, match="TURN_NOT_ACTIVE"):
        owner.refresh("holder", run_id=args["run_id"], expected_generation=3, ttl_seconds=120)
    assert owner.finish_turn("next-holder") == []


def test_finish_can_release_an_expired_handle(fixture, monkeypatch):
    import hermes_state_runs
    db, _, _ = fixture
    owner, args = started(fixture)
    owner.claim("holder", **args)
    native = db.read_run_custody(args["run_id"])
    monkeypatch.setattr(hermes_state_runs.time, "monotonic_ns", lambda: native.expires_monotonic_ns + 1)
    assert owner.finish_turn("holder") == []
    assert db.read_run_custody(args["run_id"]).disposition == "released"


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_lost_claim_ack_remains_unknown_and_is_not_retried_on_finish(fixture, monkeypatch, error_type):
    db, _, _ = fixture
    owner, args = started(fixture)
    original = db.claim_run_custody_checked
    calls = []
    def lost(*a, **kw):
        calls.append(1)
        original(*a, **kw)
        raise error_type("lost native acknowledgement")
    monkeypatch.setattr(db, "claim_run_custody_checked", lost)
    assert_unknown(lambda: owner.claim("holder", **args))
    errors = owner.finish_turn("holder")
    assert errors[0]["status"] == "unknown"
    assert errors[0]["run_id"] == args["run_id"]
    assert len(calls) == 1
    assert db.read_run_custody(args["run_id"]).disposition == "active"
    owner.begin_turn("next-holder")
    with pytest.raises(ClaimOutcomeUnknown):
        owner.claim("next-holder", **args)
    assert len(calls) == 1


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_lost_release_ack_is_retained_without_an_automatic_second_release(fixture, monkeypatch, error_type):
    db, _, _ = fixture
    owner, args = started(fixture)
    owner.claim("holder", **args)
    original = db.release_run_custody
    calls = []
    def lost(*a, **kw):
        calls.append(1)
        original(*a, **kw)
        raise error_type("lost release acknowledgement")
    monkeypatch.setattr(db, "release_run_custody", lost)
    assert_unknown(lambda: owner.release("holder", run_id=args["run_id"], expected_generation=1))
    assert owner.finish_turn("holder")[0]["status"] == "unknown"
    assert len(calls) == 1
    assert db.read_run_custody(args["run_id"]).disposition == "released"


def test_finish_serializes_with_inflight_claim(fixture, monkeypatch):
    db, _, _ = fixture
    owner, args = started(fixture)
    entered, release, finishing = threading.Event(), threading.Event(), threading.Event()
    original = db.claim_run_custody_checked
    def blocked(*a, **kw):
        entered.set()
        assert release.wait(10)
        return original(*a, **kw)
    monkeypatch.setattr(db, "claim_run_custody_checked", blocked)
    def finish():
        finishing.set()
        return owner.finish_turn("holder")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(owner.claim, "holder", **args)
        assert entered.wait(10)
        finish_future = pool.submit(finish)
        assert finishing.wait(10)
        release.set()
        assert claim_future.result(timeout=10)["generation"] == 1
        assert finish_future.result(timeout=10) == []
    assert db.read_run_custody(args["run_id"]).disposition == "released"
