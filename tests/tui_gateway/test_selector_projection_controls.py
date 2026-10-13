"""Sibling selector/recovery boundary controls, using synthetic owned records."""
import io
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.selector_projection import admit_host_selector_snapshot, stamp_selector_snapshot


def info(revision, model='B', effort='high', epoch='child'):
    return {'model': model, 'provider': 'p', 'reasoning_effort': effort,
            'service_tier': '', 'fast': False, 'pending_runtime_options': None,
            'selector_version': {'epoch': epoch, 'revision': revision}}


def test_host_mirror_rejects_old_revision_and_previous_boot():
    state = {}
    assert admit_host_selector_snapshot(state, info(2), 'boot', 'boot')
    assert not admit_host_selector_snapshot(state, info(1, 'A'), 'boot', 'boot')
    assert not admit_host_selector_snapshot(state, info(3, epoch='different'), 'boot', 'boot')
    assert admit_host_selector_snapshot(state, info(1, 'C', epoch='replacement'), 'new-boot', 'new-boot')
    assert not admit_host_selector_snapshot(state, info(100), 'boot', 'new-boot')
    assert state['_host_selector_projection']['values']['model'] == 'C'


def test_host_mirror_rejects_equal_version_conflict_and_unstamped_downgrade():
    state = {}
    assert admit_host_selector_snapshot(state, info(2), 'boot', 'boot')
    assert not admit_host_selector_snapshot(state, info(2, 'C'), 'boot', 'boot')
    assert not admit_host_selector_snapshot(state, {'model': 'A'}, 'boot', 'boot')


def test_owner_snapshot_versions_are_stable_on_noops_and_order_return_to_prior_model():
    state = {}
    a = stamp_selector_snapshot(state, info(1, 'A'))
    assert stamp_selector_snapshot(state, info(1, 'A')) == a
    b = stamp_selector_snapshot(state, info(1, 'B'))
    again = stamp_selector_snapshot(state, info(1, 'A'))
    assert a['epoch'] == b['epoch'] == again['epoch']
    assert a['revision'] < b['revision'] < again['revision']


@pytest.fixture
def owned(monkeypatch):
    record = {'session_key': 'stored', 'agent': None, '_compute_host_active': True,
              'history_lock': threading.Lock(), 'history': [], 'running': False,
              'create_reasoning_override': {'enabled': True, 'effort': 'low'},
              'create_service_tier_override': '', '_metadata_mirror': info(1, 'A', 'low')}
    monkeypatch.setattr(server, '_sessions', {'live': record})
    monkeypatch.setattr(server, '_load_cfg', lambda: {})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: True)
    supervisor = SimpleNamespace(boot_id='boot')
    monkeypatch.setattr(server, '_compute_host_supervisor', supervisor)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: supervisor)
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_session_info', lambda a, r: dict(r['_metadata_mirror']))
    return record


def dispatch(values):
    return server._methods['config.set']('request', {'session_id': 'live', 'key': 'runtime_options', 'value': values})


def ack(values, *, deferred=False, observed=None):
    observed = info(2) if observed is None else observed
    return {'type': 'control.ack', 'sid': 'live', 'route_name': 'config.set.runtime_options', '_host_boot_id': 'boot',
            'result': {'key': 'runtime_options', 'value': values, 'deferred': deferred}, 'session_info': observed}


def test_combined_fast_off_and_effort_pins_survive_actual_host_reconstruction(owned, monkeypatch):
    values = {'effort': 'high', 'fast': False}
    monkeypatch.setattr(server, '_send_compute_host_control', Mock(return_value=ack(values)))
    assert dispatch(values)['result']['deferred'] is False
    snapshot = server._compute_host_turn_frame('next', 'live', owned, '')
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_get_db', lambda: SimpleNamespace(get_session=lambda k: None))
    factory = Mock(side_effect=lambda sid, key, **kw: SimpleNamespace(model='B', provider='p', reasoning_config=kw['reasoning_config_override']))
    monkeypatch.setattr(server, '_make_agent', factory)
    monkeypatch.setattr(server, '_init_session', Mock(side_effect=RuntimeError('synthetic minimal-session boundary')))
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        rebuilt = host._ensure_server_session(server, snapshot)
        assert rebuilt['agent'].reasoning_config == {'enabled': True, 'effort': 'high'}
        assert factory.call_args.kwargs['service_tier_override'] == ''
    finally:
        host.close()


def test_deferred_options_preserve_committed_pins_and_reconstruction_intent(owned, monkeypatch):
    values = {'effort': 'high', 'fast': False}
    observed = info(2, 'A', 'low'); observed['pending_runtime_options'] = values
    monkeypatch.setattr(server, '_send_compute_host_control', Mock(return_value=ack(values, deferred=True, observed=observed)))
    assert dispatch(values)['result']['deferred'] is True
    snapshot = server._compute_host_turn_frame('next', 'live', owned, '')
    assert snapshot['reasoning_config_override']['effort'] == 'low'
    assert snapshot['pending_runtime_options'] == values


@pytest.mark.parametrize('fault', ['missing', 'contradictory', 'bad_boot', 'lost_ack'])
def test_unconfirmed_combined_option_ack_does_not_advance_pins(owned, monkeypatch, fault):
    values = {'effort': 'high'}
    frame = ack(values)
    if fault == 'missing': frame.pop('session_info')
    if fault == 'contradictory': frame['session_info']['reasoning_effort'] = 'low'
    if fault == 'bad_boot': frame['_host_boot_id'] = 'retired'
    send = Mock(side_effect=TimeoutError('synthetic lost ACK')) if fault == 'lost_ack' else Mock(return_value=frame)
    monkeypatch.setattr(server, '_send_compute_host_control', send)
    response = dispatch(values)
    assert response['error']['data']['delivery'] == 'uncertain'
    assert owned['create_reasoning_override']['effort'] == 'low'
    assert send.call_count == 1  # No write replay.
