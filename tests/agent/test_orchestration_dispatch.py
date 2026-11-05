"""Runtime denial and route propagation through actual framework entry points."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.delegate_tool import delegate_task


@pytest.fixture
def parent(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    db = SessionDB(home / "state.db")
    db.create_session("parent-session", "cli", model="main-model")
    parent = SimpleNamespace(session_id="parent-session", _session_db=db,
        model="main-model", provider="main-provider", _delegate_depth=0,
        enabled_toolsets=["file"], reasoning_config={"enabled": True, "effort": "high"})
    try:
        yield parent
    finally:
        db.close()
        reset_hermes_home_override(token)


def store(parent, **kwargs):
    return parent._session_db.set_orchestration_policy(parent.session_id,
        OrchestrationPolicy(**kwargs).to_dict(), expected_revision=0, origin="operator-command")


@pytest.mark.parametrize('effect', ['delegation', 'graph'])
def test_legacy_child_without_policy_is_not_admitted(parent, effect):
    import sqlite3
    from agent.orchestration_runtime import admit_worker

    db = parent._session_db
    store(parent, allow_delegation=False, allow_graph=False)
    db.create_session('legacy-child', 'cli', parent_session_id=parent.session_id)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute('DELETE FROM session_orchestration_policy WHERE session_id=?', ('legacy-child',))
    legacy = SimpleNamespace(session_id='legacy-child', _session_db=db)
    with pytest.raises(OrchestrationError, match='orchestration_lineage_unqualified'):
        admit_worker(legacy, effect=effect)
    with pytest.raises(OrchestrationError, match='orchestration_lineage_unqualified'):
        db.claim_orchestration_effect('legacy-child', expected_revision=0, effect=effect,
                                      route={}, config_fingerprint='fixture')
    assert db.get_orchestration_policy('legacy-child') is None


def test_orphaned_unqualified_legacy_child_remains_denied(parent):
    import sqlite3
    from agent.orchestration_runtime import admit_worker

    db = parent._session_db
    store(parent, allow_delegation=False, allow_graph=False)
    db.create_session('legacy-branch', 'cli', parent_session_id=parent.session_id)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute('DELETE FROM session_orchestration_policy WHERE session_id=?', ('legacy-branch',))
    assert db.delete_session(parent.session_id)
    assert db.get_session('legacy-branch')['parent_session_id'] is None
    with pytest.raises(OrchestrationError, match='orchestration_lineage_unqualified'):
        admit_worker(SimpleNamespace(session_id='legacy-branch', _session_db=db))


def test_native_optout_denies_before_credential_resolution_or_construction(parent):
    store(parent, allow_delegation=False)
    with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=AssertionError("credentials reached")) as credentials, \
         patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=AssertionError("child reached")) as child:
        result = json.loads(delegate_task(parent_agent=parent,
            tasks=[{"goal": "Read a bounded file and report a concrete result"}]))
    assert result["code"] == "delegation_disabled"
    assert credentials.call_count == child.call_count == 0


def test_public_lifecycle_optout_denies_before_construction(parent):
    store(parent, allow_delegation=False)
    with patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=AssertionError("child reached")) as child:
        with pytest.raises(OrchestrationError) as caught:
            SubagentLifecycleService(lambda: parent).launch(SubagentLaunchRequest(goal="A useful bounded review"))
    assert caught.value.code == "delegation_disabled"
    assert child.call_count == 0


def test_model_dispatch_carries_session_route_without_changing_main(parent):
    route = SecondaryRoute("openai-codex", "exact-secondary-model", "low")
    store(parent, secondary_route=route)
    with patch("tools.delegate_tool.delegate_task", return_value="{}") as dispatch:
        AIAgent._dispatch_delegate_task(parent, {"tasks": [{"goal": "A useful bounded task"}]})
    cfg = dispatch.call_args.kwargs["credentials_cfg"]
    assert cfg["provider"] == "openai-codex"
    assert cfg["model"] == "exact-secondary-model"
    assert cfg["reasoning_effort"] == "low"
    assert parent.model == "main-model"
    assert parent.provider == "main-provider"
    assert parent._session_db.get_session(parent.session_id)["model"] == "main-model"


def test_control_action_stop_does_not_require_spawn_permission_or_config(parent):
    store(parent, allow_delegation=False)
    with patch("tools.delegate_tool.delegate_task", return_value='{"status":"interrupted"}') as dispatch:
        result = AIAgent._dispatch_delegate_task(parent, {"action": "stop", "subagent_id": "owned-child"})
    assert json.loads(result)["status"] == "interrupted"
    assert dispatch.call_args.kwargs["action"] == "stop"


def test_queued_child_rechecks_policy_before_credential_lease(parent):
    from agent.orchestration_runtime import admit_worker
    from tools.delegate_tool import _run_single_child
    store(parent)
    admission = admit_worker(parent)
    parent._session_db.set_orchestration_policy(parent.session_id,
        OrchestrationPolicy(allow_delegation=False).to_dict(), expected_revision=1,
        origin="operator-command")
    lease = Mock(side_effect=AssertionError("credential lease reached"))
    child = SimpleNamespace(_orchestration_admission=admission,
                            _credential_pool=SimpleNamespace(acquire_lease=lease))
    result = _run_single_child(0, "Queued task must not start after opt-out", child, parent)
    assert result["status"] == "blocked"
    assert result["api_calls"] == 0
    assert lease.call_count == 0


def test_session_route_conflict_is_refused_not_substituted(parent):
    route = SecondaryRoute("openai-codex", "exact-secondary-model", "low")
    store(parent, secondary_route=route)
    with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=AssertionError("credentials reached")) as credentials:
        result = json.loads(delegate_task(parent_agent=parent,
            credentials_cfg={"provider": "different-provider", "model": "different-model"},
            tasks=[{"goal": "Do not silently override the operator route"}]))
    assert result["code"] == "secondary_route_conflict"
    assert credentials.call_count == 0


def test_managed_denial_cannot_be_widened_by_a_session_record(parent, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("orchestration:\n  allow_delegation: false\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    store(parent, allow_delegation=True)
    with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=AssertionError("credentials reached")) as credentials:
        result = json.loads(delegate_task(parent_agent=parent,
            tasks=[{"goal": "Never widen an administrator restriction"}]))
    assert result["code"] == "managed_orchestration_denied"
    assert credentials.call_count == 0


def test_unavailable_policy_owner_never_reaches_spawn(parent):
    parent._session_db.close()
    with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=AssertionError("credentials reached")) as credentials:
        result = json.loads(delegate_task(parent_agent=parent,
            tasks=[{"goal": "Do not construct a child without policy state"}]))
    assert result["code"] == "policy_store_unavailable"
    assert credentials.call_count == 0
