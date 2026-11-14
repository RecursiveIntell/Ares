"""Authenticated controls exercise real state without invoking any provider."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    owner=SessionDB(tmp_path/'state.db')
    owner.create_session('current','cli',model='main-unchanged')
    yield owner
    owner.close()


def test_tui_desktop_command_control_does_not_rebuild_agent_or_prompt(db,monkeypatch):
    from tui_gateway import server
    import tui_gateway.methods_tools
    session={'session_key':'current','profile_home':str(db.db_path.parent),
        'agent':None,'messages':[], 'sentinel_prefix':b'fixed-prefix'}
    monkeypatch.setitem(server._sessions,'surface-key',session)
    with patch.object(server,'_ensure_session_db_row'):
        result=server._methods['command.dispatch'](1,{'name':'orchestration','arg':'off','session_id':'surface-key'})
    assert 'error' not in result
    assert db.get_orchestration_policy('current')['policy']['allow_delegation'] is False
    assert session['agent'] is None
    assert session['sentinel_prefix']==b'fixed-prefix'
    assert db.get_session('current')['model']=='main-unchanged'


def test_tui_unknown_session_fails_not_global_control(db):
    from tui_gateway import server
    import tui_gateway.methods_tools
    result=server._methods['command.dispatch'](1,{'name':'orchestration','arg':'off','session_id':'foreign'})
    assert 'error' in result
    assert db.get_orchestration_policy('current') is None


def test_acp_control_uses_managed_state_not_text_session_id(db):
    from acp_adapter.server import HermesACPAgent
    from acp_adapter.session import SessionManager,SessionState
    owner=HermesACPAgent(session_manager=SessionManager(db=db))
    state=SessionState(session_id='current',agent=SimpleNamespace(model='main-unchanged'))
    result=owner._handle_slash_command('/orchestration no-graphs',state)
    assert json.loads(result)['policy']['allow_graph'] is False
    assert state.agent.model=='main-unchanged'


def test_cli_full_message_optout_runs_before_credentials(db):
    from cli import HermesCLI
    obj=HermesCLI.__new__(HermesCLI)
    obj._session_db=db
    obj.session_id='current'
    obj.model='main-unchanged'
    with patch('cli._cprint'), patch.object(obj, '_ensure_runtime_credentials', side_effect=AssertionError('credentials reached')):
        result=json.loads(obj.chat('do not delegate'))
    assert result['policy']['allow_delegation'] is False
    assert result['policy']['allow_graph'] is False


@pytest.mark.parametrize('replay_fields',[{'display_kind':'hidden'}, {'truncate_before_user_ordinal':0}, {'truncate_before_row_id':1}])
def test_tui_replay_or_hidden_event_cannot_change_policy(db, monkeypatch, replay_fields):
    from tui_gateway import server
    import tui_gateway.methods_prompt
    session={'session_key':'current','profile_home':str(db.db_path.parent)}
    monkeypatch.setattr(server,'_sess_nowait',lambda params,rid:(session,None))
    monkeypatch.setattr(server,'_voice_mode_enabled',lambda:False)
    monkeypatch.setattr(server,'_ensure_active_session_slot',lambda *args:'stop-before-model')
    result=server._methods['prompt.submit'](1,dict(session_id='current',text='no graphs',**replay_fields))
    assert 'error' in result
    assert db.get_orchestration_policy('current') is None


def test_tui_current_user_grammar_reaches_control_without_model(db, monkeypatch):
    from tui_gateway import server
    import tui_gateway.methods_prompt
    import tui_gateway.methods_tools
    session={'session_key':'current','profile_home':str(db.db_path.parent)}
    monkeypatch.setitem(server._sessions,'surface-key',session)
    monkeypatch.setattr(server,'_sess_nowait',lambda params,rid:(session,None))
    monkeypatch.setattr(server,'_voice_mode_enabled',lambda:False)
    with patch.object(server,'_ensure_session_db_row'):
        result=server._methods['prompt.submit'](1,{'session_id':'surface-key','text':'no graphs'})
    assert 'error' not in result
    assert db.get_orchestration_policy('current')['policy']['allow_graph'] is False


@pytest.mark.asyncio
async def test_gateway_control_has_exact_event_session_provenance(db):
    from gateway.slash_commands import GatewaySlashCommandsMixin
    entry=SimpleNamespace(session_id='current')
    runner=SimpleNamespace(async_session_store=SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry)),
        _session_db=SimpleNamespace(_db=db))
    event=SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value='telegram')),
        message_id='operator-event', get_command_args=lambda:'off')
    response=await GatewaySlashCommandsMixin._handle_orchestration_command(runner,event)
    result=json.loads(response)
    assert result['policy']['allow_delegation'] is False
    assert result['source_message_id']=='operator-event'


@pytest.mark.asyncio
async def test_api_control_requires_auth_and_only_accepts_control_field(db):
    from gateway.platforms.api_server import APIServerAdapter
    from aiohttp import web
    adapter=APIServerAdapter.__new__(APIServerAdapter)
    request=SimpleNamespace(match_info={'session_id':'current'},method='POST')
    adapter._check_auth=lambda request:web.json_response({'error':'unauthorized'},status=401)
    result=await adapter._handle_session_orchestration(request)
    assert result.status==401
    assert db.get_orchestration_policy('current') is None
    adapter._check_auth=lambda request:None
    adapter._get_existing_session_or_404=AsyncMock(return_value=(db.get_session('current'),None))
    adapter._ensure_session_db_async=AsyncMock(return_value=db)
    adapter._read_json_body=AsyncMock(return_value=({'control':'off','session_id':'foreign'},None))
    result=await adapter._handle_session_orchestration(request)
    assert result.status==400
    adapter._read_json_body=AsyncMock(return_value=({'control':'off'},None))
    result=await adapter._handle_session_orchestration(request)
    assert result.status==200
    assert db.get_orchestration_policy('current')['policy']['allow_graph'] is False
