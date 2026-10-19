"""Stream close/exhaustion are real lifecycle boundaries, never send retries."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from hermes_cli.middleware import GovernedStreamProxy


def ticket():
    return SimpleNamespace(settle=Mock())


def test_closed_stream_cannot_resume_underlying_iteration():
    t=ticket();source=iter(["before-close","forbidden-after-close"])
    stream=GovernedStreamProxy(source,[t])
    assert next(stream)=="before-close"
    stream.close()
    with pytest.raises(StopIteration):next(stream)
    t.settle.assert_called_once()
    assert t.settle.call_args.kwargs["outcome"]=="abandoned"


def test_close_is_idempotent_for_actual_provider_resource():
    t=ticket()
    class Source:
        close=Mock()
    source=Source();stream=GovernedStreamProxy(source,[t])
    stream.close();stream.close()
    source.close.assert_called_once()
    t.settle.assert_called_once()


def test_context_manager_exit_uses_original_manager_when_enter_returns_stream():
    t=ticket();closed=[]
    class Manager:
        def __enter__(self):return iter(["one"])
        def __exit__(self,*args):closed.append(args);return False
    with GovernedStreamProxy(Manager(),[t]) as stream:
        assert list(stream)==["one"]
    assert len(closed)==1
    assert t.settle.call_count==1
    assert t.settle.call_args.kwargs["outcome"]=="ok"
