"""Real scratch-DB contracts. Judge responses below are injected fixtures."""
import json
import pytest
from hermes_cli import goals

@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    (tmp_path / 'hermes').mkdir()
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session('recovery', source='cli')
    mgr = goals.GoalManager('recovery', default_max_turns=8)
    mgr.set('finish authorized work')
    yield mgr
    goals._DB_CACHE.clear()

@pytest.mark.parametrize('verdict', ['blocked', 'GOAL_BLOCKED'])
def test_parser_preserves_typed_block(verdict):
    parsed = goals._parse_judge_response(json.dumps({'verdict': verdict, 'reason': 'owner needed'}))
    assert parsed[:3] == ('blocked', 'owner needed', False)

@pytest.mark.parametrize('payload', [{'verdict':'approve'}, {}, {'verdict':'wait'}])
def test_unknown_judge_contract_is_failure(payload):
    assert goals._parse_judge_response(json.dumps(payload))[2] is True

def test_typed_block_precedes_gate_and_counts_only_discovery(manager, monkeypatch):
    manager.add_gate("python -c 'raise SystemExit(7)'")
    monkeypatch.setattr(goals, 'run_gate', lambda *a, **k: pytest.fail('blocked gate ran'))
    decision = manager.evaluate_after_turn('needs owner', turn_outcome=goals.GOAL_BLOCKED,
        turn_metadata={'reason':'approval required', 'blocker_kind':'owner_approval'})
    assert decision['verdict'] == 'blocked'
    assert manager.state.checkpoint['blocker_kind'] == 'owner_approval'
    assert manager.state.turns_used == 1
    assert goals.GoalManager('recovery').resume() is not None
    again = goals.GoalManager('recovery')
    assert again.state.checkpoint['blocker_kind'] == 'owner_approval'
    assert again.next_continuation_prompt() is None
    assert again.claim_continuation('worker') is False
    assert again.start_continuation() is False
    assert again.evaluate_after_turn('notification')['should_continue'] is False
    assert again.state.turns_used == 1

def test_completion_candidate_is_a_durable_barrier(manager, monkeypatch):
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('done','fixture candidate',False,None,False))
    manager.evaluate_after_turn('candidate')
    current = goals.GoalManager('recovery')
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('authority barrier judged'))
    assert current.evaluate_after_turn('dependency finished')['should_continue'] is False
    assert current.state.turns_used == 1
    assert current.state.checkpoint['blocker_kind'] == 'unverified_completion'
    assert current.resume().turns_used == 1
    assert current.start_continuation() is False
    assert current.confirm_completion('explicit receipt')

def test_unknown_fixture_never_mints_goal_continuation(manager, monkeypatch):
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('continue','invalid fixture',True,None,False))
    result = manager.evaluate_after_turn('some work')
    assert result['should_continue'] is False
    assert manager.state.continuation_pending is False
    assert manager.state.status == 'paused'

def test_independent_approved_steps_survive_partial_block(manager, monkeypatch):
    result = manager.evaluate_after_turn('partial', turn_outcome=goals.GOAL_BLOCKED,
        turn_metadata={'reason':'one step needs input', 'blocker_kind':'missing_input',
          'blockers':[{'id':'a','kind':'missing_input'}, {'id':'b','kind':'owner_approval'}],
          'authorized_actionable_steps':['validate existing artifact']})
    assert result['should_continue'] is True
    assert manager.state.status == 'active'
    assert len(manager.state.checkpoint['blockers']) == 2
    assert manager.claim_continuation('worker')
    assert manager.start_continuation()
    assert 'validate existing artifact' in manager.next_continuation_prompt()
    manager.release_continuation(queued=False)

@pytest.mark.parametrize('outcome', [goals.GOAL_BLOCKED, goals.WAITING_FOR_AUTHORITY, 'UNKNOWN_OUTCOME'])
def test_legacy_explicit_or_unknown_truth_parks_on_load(manager, outcome):
    db = goals._get_session_db()
    row = json.loads(manager.state.to_json())
    row.update(status='active', outcome=outcome, continuation_pending=False)
    db.set_meta('goal:recovery', json.dumps(row))
    parked = goals.GoalManager('recovery')
    assert parked.state.status == 'paused'
    assert parked.next_continuation_prompt() is None
    assert parked.state.turns_used == 0

def test_stale_manager_evaluation_consumes_zero(manager, monkeypatch):
    stale = goals.GoalManager('recovery')
    manager.pause()
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('stale turn judged'))
    assert stale.evaluate_after_turn('old notification')['should_continue'] is False
    assert goals.GoalManager('recovery').state.turns_used == 0

def test_consumed_crash_is_not_a_fresh_replay_or_budget_charge(manager, monkeypatch):
    manager.evaluate_after_turn('', turn_outcome=goals.EXECUTION_FAILED)
    assert manager.start_continuation()
    crashed = goals.GoalManager('recovery')
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('unknown admission judged'))
    assert crashed.claim_continuation('restart') is False
    assert crashed.checkpoint_recovery('restart')['should_continue'] is False
    assert crashed.evaluate_after_turn('stale notification')['should_continue'] is False
    assert crashed.state.turns_used == 1

def test_block_restart_resume_reblock_cycle_keeps_budget_and_blockers(manager):
    manager.evaluate_after_turn('blocked', turn_outcome=goals.GOAL_BLOCKED,
        turn_metadata={'reason':'needs data','blocker_kind':'missing_input',
          'blockers':[{'id':'a','kind':'missing_input'}, {'id':'b','kind':'owner_approval'}]})
    restarted=goals.GoalManager('recovery'); restarted.resume()
    assert len(restarted.state.checkpoint['blockers']) == 2
    assert restarted.state.turns_used == 1
    assert restarted.evaluate_after_turn('dependency output')['should_continue'] is False
    assert goals.GoalManager('recovery').state.turns_used == 1


def test_bound_duplicate_settlement_cannot_judge_new_checkpoint(manager, monkeypatch):
    manager.evaluate_after_turn('',turn_outcome=goals.EXECUTION_FAILED)
    binding={'goal_id':manager.state.goal_id, 'checkpoint_revision':manager.state.checkpoint_revision,
             'continuation_token':manager.state.continuation_token}
    assert manager.start_continuation()
    monkeypatch.setattr(goals,'judge_goal', lambda *a,**k: ('continue','fixture unfinished',False,None,False))
    goals.GoalManager('recovery').evaluate_after_turn('result',turn_metadata={'continuation':binding})
    before=goals.load_goal('recovery').turns_used
    monkeypatch.setattr(goals,'judge_goal', lambda *a,**k: pytest.fail('duplicate judged'))
    assert goals.GoalManager('recovery').evaluate_after_turn('result',turn_metadata={'continuation':binding})['should_continue'] is False
    assert goals.load_goal('recovery').turns_used == before


def test_independent_notification_is_delivered_without_goal_authority(manager, monkeypatch):
    manager.add_gate("python -c 'raise SystemExit(7)'")
    monkeypatch.setattr(goals, 'run_gate', lambda *a, **k: pytest.fail('notification ran gate'))
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('notification judged'))
    result=manager.evaluate_after_turn('independent owned result',
        turn_metadata={'origin':'notification'})
    assert result['should_continue'] is False
    assert manager.state.turns_used == 0
    assert manager.state.status == 'active'


def test_two_consumers_cannot_consume_newer_checkpoint(manager):
    manager.evaluate_after_turn('', turn_outcome=goals.EXECUTION_FAILED)
    binding={'expected_goal_id':manager.state.goal_id,
        'expected_checkpoint_revision':manager.state.checkpoint_revision,
        'expected_continuation_token':manager.state.continuation_token}
    second=goals.GoalManager('recovery')
    assert manager.claim_continuation('first')
    assert not second.claim_continuation('second')
    assert manager.start_continuation(**binding)
    manager.release_continuation(queued=False)
    assert not second.start_continuation(**binding)
    assert not goals.GoalManager('recovery').state.continuation_pending
