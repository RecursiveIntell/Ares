"""Wire failures retain safe attribution without raw exception text."""
import pytest
from tui_gateway import server
from tui_gateway.host_supervisor import HostSendNotSent, HostStopNotApplied, HostSendUncertain, HostProjectionFault


@pytest.mark.parametrize("exception,disposition", [(RuntimeError, "indeterminate"), (HostSendNotSent, "not_sent")])
def test_interrupt_failure_has_safe_correlation(monkeypatch, exception, disposition):
    session = {"_compute_host_active_request_id": "A"}
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _: True)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda: None)
    def fail(*a, **kw):
        raise exception("secret-prompt-credential")
    monkeypatch.setattr(server, "_interrupt_session_turn", fail)
    response = server.dispatch({"jsonrpc": "2.0", "id": "stop", "method": "session.interrupt", "params": {"session_id": "s"}})
    assert "secret-prompt-credential" not in str(response)
    assert response["error"]["data"]["effect_disposition"] == disposition
    assert response["error"]["data"]["request_id"] == "interrupt-stop"
    assert response["error"]["data"]["target_request_id"] == "A"


@pytest.mark.parametrize('exception,code', [
    (HostStopNotApplied, 'stop_refused_before_effect'),
    (HostSendNotSent, 'stop_not_sent'),
    (HostSendUncertain, 'stop_ack_unconfirmed'),
    (HostProjectionFault, 'stop_projection_failed'),
    (RuntimeError, 'stop_application_indeterminate'),
])
def test_first_refusal_has_distinct_safe_code(monkeypatch, exception, code):
    session = {'_compute_host_active_request_id': 'A'}
    monkeypatch.setattr(server, '_sess_nowait', lambda *a: (session, None))
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _: True)
    monkeypatch.setattr(server, '_tts_stream_stop', lambda: None)
    def fail(*a, **kw):
        raise exception('private-payload')
    monkeypatch.setattr(server, '_interrupt_session_turn', fail)
    response = server.dispatch({'jsonrpc': '2.0', 'id': 'stop', 'method': 'session.interrupt',
                                'params': {'session_id': 's'}})
    assert response['error']['data']['failure_code'] == code
    assert 'private-payload' not in str(response)


def test_unknown_return_reports_original_operation_without_raw_error(monkeypatch):
    session = {'_compute_host_active_request_id': 'A'}
    monkeypatch.setattr(server, '_sess_nowait', lambda *a: (session, None))
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _: True)
    monkeypatch.setattr(server, '_tts_stream_stop', lambda: None)
    def fail(*a, **kw):
        session['_stop_uncertain'] = {'operation_id': 'opaque-original', 'request_id': 'interrupt-stop',
                                      'target_request_id': 'A', 'host_boot_id': 'boot'}
        raise HostSendUncertain('private-detail')
    monkeypatch.setattr(server, '_interrupt_session_turn', fail)
    result = server.dispatch({'jsonrpc': '2.0', 'id': 'stop', 'method': 'session.interrupt',
                              'params': {'session_id': 's'}})
    assert result['error']['data']['operation_id'] == 'opaque-original'
    assert result['error']['data']['request_id'] == 'interrupt-stop'
    assert 'private-detail' not in str(result)
