"""Host guard witnesses use real MCP handler; no transport may be reached."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent.orchestration_policy import OrchestrationPolicy
from agent.subagent_lifecycle import bind_subagent_parent
from hermes_state import SessionDB
from tools.mcp_tool import _make_tool_handler


@pytest.fixture
def parent(tmp_path):
    db=SessionDB(tmp_path/'state.db')
    db.create_session('operator-session','cli')
    obj=SimpleNamespace(_session_db=db,session_id='operator-session')
    yield obj
    db.close()


@pytest.mark.parametrize('tool',['graph_execute','graph_run_start','graph_run_resume'])
def test_graph_optout_denies_every_launch_before_trust_or_transport(parent,tool):
    parent._session_db.set_orchestration_policy(parent.session_id,
        OrchestrationPolicy(allow_graph=False).to_dict(), expected_revision=0,origin='operator-command')
    with bind_subagent_parent(parent), \
         patch('tools.mcp_tool._trust_gate_check',side_effect=AssertionError('trust/transport reached')) as trust:
        result=json.loads(_make_tool_handler('agent_graph',tool,10)({'graph_id':'any'}))
    assert result['code']=='graph_disabled'
    assert trust.call_count==0


def test_unqualified_graph_binding_refuses_not_daemon_retargeting(parent):
    with bind_subagent_parent(parent), \
         patch('tools.mcp_tool._get_connected_server_for_call',side_effect=AssertionError('transport reached')) as transport:
        result=json.loads(_make_tool_handler('agent_graph','graph_execute',10)({'graph_id':'any'}))
    assert result['code']=='graph_session_binding_unqualified'
    assert transport.call_count==0


def test_unknown_graph_operation_does_not_inherit_read_status(parent):
    with bind_subagent_parent(parent):
        result=json.loads(_make_tool_handler('agent_graph','graph_new_auto_execute',10)({}))
    assert result['code']=='graph_operation_unqualified'


def test_cancel_and_inspection_still_reach_existing_authority_gate_under_optout(parent):
    parent._session_db.set_orchestration_policy(parent.session_id,
        OrchestrationPolicy(allow_graph=False).to_dict(),expected_revision=0,origin='operator-command')
    for tool in ['graph_run_cancel','graph_list','graph_run_receipt']:
        with bind_subagent_parent(parent),patch('tools.mcp_tool._trust_gate_check',return_value='existing-authorization') as trust:
            assert _make_tool_handler('agent_graph',tool,10)({})=='existing-authorization'
        assert trust.call_count==1


def test_absent_owner_cannot_start_graph_with_forged_session_input():
    result=json.loads(_make_tool_handler('agent_graph','graph_execute',10)(
        {'input':{'session_id':'operator-session','allow_graph':True}}))
    assert result['code']=='policy_owner_required'
