"""SDK completion receipt and terminal head commit as one SessionDB write."""
import hashlib
import json

import pytest

from hermes_state_execution import ExecutionRecordStoreError
from tests.hermes_state.test_execution_record_store import base, db  # noqa: F401


def sent(db):
    db.create_execution_record(base())
    db.transition_execution_record('turn:api:1:0', 'materialized',
                                   materialization_ref='ares-materialization:' + 'a' * 64,
                                   logical_request_digest='sha256:' + 'b' * 64)
    db.transition_execution_record('turn:api:1:0', 'send_started',
                                   wire_request_digest='sha256:' + 'c' * 64, wire_bytes=17,
                                   governor_qualified=True, governor_mode='enforce',
                                   dispatch_intent_refs=['ares-dispatch-intent:one'])


def complete(db, **overrides):
    values = dict(dispatch_intent_ref='ares-dispatch-intent:one',
                  observed_prefix_digest='sha256:' + 'd' * 64,
                  observed_prefix_bytes=19, http_status=200)
    return db.settle_sdk_stream_execution('turn:api:1:0', **(values | overrides))


def test_receipt_is_bound_durable_and_idempotent(db):
    sent(db)
    result = complete(db).to_dict()
    assert result['state'] == 'settled' and result['terminal_outcome'] == 'ok'
    assert result['response_digest'] is None
    ref, = result['receipt_refs']
    encoded = db.get_meta(ref)
    assert ref == 'execution-sdk-completion:' + hashlib.sha256(encoded.encode()).hexdigest()
    receipt = json.loads(encoded)
    assert receipt['execution_id'] == result['execution_id']
    assert receipt['dispatch_intent_ref'] in result['dispatch_intent_refs']
    assert receipt['response_complete'] is False
    assert complete(db).to_dict() == result


def test_failed_head_update_rolls_back_the_receipt(db, monkeypatch):
    sent(db)
    def fail(*a, **kw):
        raise ExecutionRecordStoreError('LOCAL_FAULT')
    monkeypatch.setattr(db, 'transition_execution_record_on_conn', fail)
    with pytest.raises(ExecutionRecordStoreError, match='LOCAL_FAULT'):
        complete(db)
    assert db.read_execution_record('turn:api:1:0').state == 'send_started'
    assert db.list_meta_prefix('execution-sdk-completion:') == []


@pytest.mark.parametrize('bad', [
    {'observed_prefix_digest': 'bad'}, {'observed_prefix_bytes': 0},
    {'observed_prefix_bytes': True}, {'http_status': True},
    {'dispatch_intent_ref': 'ares-dispatch-intent:other'},
])
def test_invalid_or_unbound_completion_never_advances_the_head(db, bad):
    sent(db)
    with pytest.raises(ExecutionRecordStoreError):
        complete(db, **bad)
    assert db.read_execution_record('turn:api:1:0').state == 'send_started'
    assert db.list_meta_prefix('execution-sdk-completion:') == []
