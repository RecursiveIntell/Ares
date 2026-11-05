from unittest.mock import patch
from types import SimpleNamespace

import pytest
from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy
from agent.subagent_lifecycle import bind_subagent_parent
from hermes_state import SessionDB
from run_agent import AIAgent


def test_imported_constructor_alias_cannot_erase_bound_origin_before_init(tmp_path):
    db=SessionDB(tmp_path/'state.db')
    db.create_session('origin','cli')
    db.set_orchestration_policy('origin',OrchestrationPolicy(allow_delegation=False).to_dict(),
        expected_revision=0,origin='operator-command')
    parent=SimpleNamespace(_session_db=db,session_id='origin')
    alias=AIAgent
    try:
        with bind_subagent_parent(parent),patch('agent.agent_init.init_agent',side_effect=AssertionError('init reached')) as init:
            with pytest.raises(OrchestrationError,match='delegation_disabled'):
                alias(session_id='fresh-root-looking-id',model='not-permitted')
        assert init.call_count==0
    finally:
        db.close()


def test_admitted_constructor_retains_origin_not_new_identity(tmp_path):
    db=SessionDB(tmp_path/'state.db')
    db.create_session('origin','cli')
    parent=SimpleNamespace(_session_db=db,session_id='origin')
    try:
        with bind_subagent_parent(parent),patch('agent.agent_init.init_agent'):
            obj=AIAgent(session_id='new-child-id')
        assert obj._orchestration_admission.session_id=='origin'
        assert obj._orchestration_admission.profile_home==str(tmp_path.resolve())
    finally:
        db.close()
