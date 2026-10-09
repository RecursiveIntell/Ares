"""Failed once custody at real model selection and prompt admission owners."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.tui_gateway.test_model_intent_admission_order import (  # noqa: F401
    agent, gateway, intent_turn, live_turn, routes, select, turn_env, run_turn,
)
from tui_gateway import server


@pytest.fixture
def failed_once(intent_turn):
    session, events = intent_turn
    assert not server.handle_request(select('model-b', 'endpoint-b', once=True)).get('error')
    snapshot, lease = server._consume_one_turn_model_runtime(session, session['agent'])
    lease['restore_failed'] = True
    lease['active'] = False
    return session, events, lease, snapshot


def submit(text='new input', **params):
    return server._methods['prompt.submit']('new-rid', {'session_id': 'selected', 'text': text, **params})


def refused(response, *, accepted=False):
    assert response['error']['data']['error_surface'] == {
        'layer': 'runtime', 'code': 'one_turn_model_restore_failed', 'retryable': False}
    assert response['error']['data']['execution_started'] is False
    assert response['error']['data']['durable_input_accepted'] is accepted


@pytest.mark.parametrize('truncate', [False, True])
def test_fresh_failure_refuses_before_input_history_and_attachments(failed_once, monkeypatch, truncate):
    session, _, lease, snapshot = failed_once
    session['attached_images'] = ['inert.png']
    history = copy.deepcopy(session['history'])
    version = session.get('history_version')
    accept = Mock(side_effect=AssertionError('input acceptance forbidden'))
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    response = submit(**({'truncate_before_user_ordinal': 0, 'confirm_truncate': True} if truncate else {}))
    refused(response)
    accept.assert_not_called()
    assert session['history'] == history and session.get('history_version') == version
    assert session['attached_images'] == ['inert.png'] and not session['running']
    assert session['_one_turn_model_runtime'] is lease and lease['restore_snapshot'] is snapshot


def test_dispatch_backstop_retains_its_own_failed_input(failed_once, monkeypatch):
    session, events, lease, snapshot = failed_once
    history = copy.deepcopy(session['history'])
    session['running'] = True
    provider = Mock(side_effect=AssertionError('provider forbidden'))
    marker = Mock(side_effect=AssertionError('crash marker forbidden'))
    monkeypatch.setattr(session['agent'], 'run_conversation', provider)
    monkeypatch.setattr(server, 'record_turn_start', marker)
    assert server._run_prompt_submit('queued', 'selected', session, 'owned queued input',
        queued_prompt_generation=int(session.get('_queued_prompt_generation', 0))) is False
    provider.assert_not_called();marker.assert_not_called()
    assert session['history'] == history and not session['running']
    assert session['inflight_turn']['user'] == 'owned queued input'
    assert session['inflight_turn']['status'] == 'error'
    terminal = [p for e, _, p in events if e == 'message.complete' and p.get('status') == 'error']
    assert terminal[-1]['error_surface']['code'] == 'one_turn_model_restore_failed'
    assert lease['restore_snapshot'] is snapshot and session['_one_turn_model_runtime'] is lease


def test_failure_racing_durable_acceptance_keeps_receipt(failed_once, monkeypatch):
    session, _, lease, _ = failed_once
    lease['restore_failed'] = False
    receipt = SimpleNamespace(event_id='recorded-input')
    accepted = []
    def accept(*args, **kwargs):
        accepted.append(receipt)
        lease['restore_failed'] = True
        return receipt
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    response = submit()
    refused(response, accepted=True)
    assert accepted == [receipt]
    assert response['error']['data']['input_event_id'] == receipt.event_id
    assert session['_one_turn_model_runtime'] is lease and not session['running']


def test_successful_idle_C_retires_failed_lease(failed_once, monkeypatch):
    session, _, _, _ = failed_once
    assert not server.handle_request(select('model-c', 'endpoint-c')).get('error')
    assert not session.get('_one_turn_model_runtime')
    assert run_turn(session, monkeypatch)[0][:2] == ('model-c', 'endpoint-c')


def test_new_once_C_preserves_original_A(failed_once, monkeypatch):
    session, _, old, snapshot = failed_once
    assert not server.handle_request(select('model-c', 'endpoint-c', once=True)).get('error')
    assert session['_one_turn_model_runtime'] is not old
    assert session['one_turn_model_restore'] is snapshot
    assert session['_one_turn_model_runtime']['restore_snapshot'] is snapshot
    assert run_turn(session, monkeypatch)[0][:2] == ('model-c', 'endpoint-c')
    assert session['agent'].model == 'model-a'


def test_failed_publication_keeps_failed_custody(failed_once, monkeypatch):
    session, _, lease, snapshot = failed_once
    def fail(*args):
        refused(submit())
        raise RuntimeError('inert publication failure')
    monkeypatch.setattr(server, '_restart_slash_worker', fail)
    response = server.handle_request(select('model-c', 'endpoint-c'))
    assert response.get('error')
    assert session['_one_turn_model_runtime'] is lease and lease['restore_snapshot'] is snapshot
    assert lease['restore_failed'] and not lease.get('superseding_intent')


def test_old_publication_failure_cannot_resurrect_over_newer_choice(failed_once, monkeypatch):
    session, _, lease, _ = failed_once
    def publish(*args):
        if session['agent'].model == 'model-c':
            assert not server.handle_request(select('newer-model', 'endpoint-b')).get('error')
            raise RuntimeError('older publication failure')
    monkeypatch.setattr(server, '_restart_slash_worker', publish)
    assert server.handle_request(select('model-c', 'endpoint-c')).get('error')
    assert session['agent'].model == 'newer-model' and not session.get('_one_turn_model_runtime')
    assert lease['restore_failed']


@pytest.mark.parametrize('kind', ['invalid', 'unconfirmed'])
def test_unsuccessful_intent_keeps_failed_lease(failed_once, monkeypatch, kind):
    session, _, lease, _ = failed_once
    if kind == 'invalid':
        response = server.handle_request(select('model-c', 'nonexistent-provider'))
        assert response.get('error')
    else:
        monkeypatch.setattr('hermes_cli.model_selection_guards.combined_selection_warning',
            lambda *a, **k: SimpleNamespace(message='inert consent required'))
        response = server.handle_request(select('model-c', 'endpoint-c', confirmed=False))
        assert response['result']['confirm_required']
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']


def test_internal_config_adoption_cannot_supersede_failed_lease(failed_once):
    session, _, lease, _ = failed_once
    with pytest.raises(ValueError, match='not restored'):
        server._apply_model_switch('selected', session, 'model-c --provider endpoint-c',
            confirm_expensive_model=True, pin_session_override=False, persist_override=False)
    assert session['_one_turn_model_runtime'] is lease and session['agent'].model == 'model-b'


def test_eligible_deferred_C_settles_before_input_admission(failed_once, monkeypatch):
    session, _, _, _ = failed_once
    session['pending_model_switch'] = {'raw': 'model-c --provider endpoint-c --session',
        'confirm_expensive_model': True}
    seen = []
    def accept(*args, **kwargs):
        seen.append((session['agent'].model, session.get('_one_turn_model_runtime')))
        raise RuntimeError('stop after admission observation')
    monkeypatch.setattr(server, '_accept_tui_context_input', accept)
    assert submit().get('error')
    assert seen == [('model-c', None)] and not session.get('pending_model_switch')


def test_Stop_does_not_clear_failed_lease(failed_once):
    session, _, lease, _ = failed_once
    response = server._methods['session.interrupt']('stop', {'session_id': 'selected'})
    assert not response.get('error')
    assert session['_one_turn_model_runtime'] is lease and lease['restore_failed']
    refused(submit())


def test_unexpected_same_session_agent_replacement_cannot_escape_failure(failed_once):
    session, _, lease, _ = failed_once
    session['agent'] = SimpleNamespace(model='replacement', provider='inert')
    refused(submit())
    assert session['_one_turn_model_runtime'] is lease


def test_stale_generation_does_not_poison_replacement(failed_once):
    session, _, lease, _ = failed_once
    replacement = {'agent': SimpleNamespace(), 'history_lock': session['history_lock']}
    server._sessions['selected'] = replacement
    with session['history_lock']:
        assert server._one_turn_model_restore_error('selected', session) is None
        assert server._one_turn_model_restore_error('selected', replacement) is None
    assert session['_one_turn_model_runtime'] is lease
    assert '_one_turn_model_runtime' not in replacement
