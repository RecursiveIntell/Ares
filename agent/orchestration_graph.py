"""Agent Graph host admission, deliberately NOT a daemon security boundary.

The captured daemon has no consumed authenticated session-policy binding. Until
that boundary is qualified, launch/resume is a typed refusal, not a client-only
'permit'. Read/cleanup and inert spec/storage operations retain their own MCP
permissions. Raw RPC/shell access is outside this host adapter's containment.
"""
from agent.orchestration_policy import OrchestrationError
from agent.orchestration_runtime import admit_worker
from agent.subagent_lifecycle import get_active_subagent_parent

GRAPH_OPERATIONS = {
    'graph_approval_get': 'read', 'graph_approval_list': 'read',
    'graph_approval_request': 'approval_request', 'graph_create': 'spec',
    'graph_execute': 'launch', 'graph_inspect': 'read', 'graph_list': 'read',
    'graph_policy_check': 'preflight', 'graph_render': 'read',
    'graph_retention_review': 'read', 'graph_retention_set': 'retention',
    'graph_run_artifact': 'read', 'graph_run_cancel': 'cancel',
    'graph_run_checkpoint': 'read', 'graph_run_events': 'read',
    'graph_run_get': 'read', 'graph_run_receipt': 'read',
    'graph_run_resume': 'launch', 'graph_run_start': 'launch',
    'graph_run_state': 'read', 'graph_run_triage': 'read', 'graph_run_wait': 'read',
    'graph_source_witness_capture': 'witness', 'graph_source_witness_get': 'read',
    'graph_status': 'read', 'graph_template_candidates': 'read',
    'graph_template_instantiate': 'spec', 'graph_template_list': 'read',
    'graph_template_outcomes': 'read',
}


def guard_graph_mcp(server_name, tool_name, arguments):
    """Before MCP trust/connection/queue. No payload session ID is trusted."""
    if server_name != 'agent_graph':
        return
    category = GRAPH_OPERATIONS.get(tool_name)
    if category is None:
        raise OrchestrationError('graph_operation_unqualified')
    if category == 'launch':
        admit_worker(get_active_subagent_parent(), effect='graph')
        raise OrchestrationError('graph_session_binding_unqualified')
