"""Evidence-only handoffs: never copy logical-call/credential authority context."""
from contextvars import ContextVar
import json
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

from ares_runtime import physical_dispatch as physical

REAL_THREAD = threading.Thread


def bind(callback, purpose='title_generation'):
    assert hasattr(physical, 'bind_capture_observer'), 'explicit observer-only handoff API missing'
    return physical.bind_capture_observer(callback, purpose=purpose)


def evidence(capture):
    assert hasattr(capture, 'evidence_snapshot'), 'atomic capture evidence API missing'
    return capture.evidence_snapshot()


def send():
    with physical.bind_http_client(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200,stream=httpx.ByteStream(b'{"fixture":"ok"}'))
    ))) as client:
        client.post('https://fixture.invalid/v1/messages',json={
            'model':'fixture','messages':[],'max_tokens':8})


def run_thread(callback):
    errors = []
    def run():
        try:
            callback()
        except BaseException as error:
            errors.append(error)
    thread = REAL_THREAD(target=run)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    if errors:
        raise errors[0]


def test_real_title_thread_is_captured_without_transferring_parent_authority(monkeypatch):
    from agent import title_generator as title
    authority = ContextVar('fixture_unrelated_authority',default=None)
    seen, threads, errors = [], [], []
    def title_callback(*args,**kwargs):
        try:
            state = (physical._call.get(), physical._sdk_capture_tickets.get(), authority.get())
            seen.append(state)
            assert state == (None, None, None)
            send()
        except BaseException as error:
            errors.append(error)
    def start_thread(*args,**kwargs):
        thread = REAL_THREAD(*args,**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(title.threading,'Thread',start_thread)
    monkeypatch.setattr(title,'_auto_title_enabled',lambda:True)
    monkeypatch.setattr(title,'is_titleable_user_message',lambda message:True)
    monkeypatch.setattr(title,'apply_instant_title',lambda *args,**kwargs:None)
    monkeypatch.setattr(title,'auto_title_session',title_callback)
    call_token = physical._call.set({'parent-authority':'must-not-cross'})
    sdk_token = physical._sdk_capture_tickets.set(['parent-sdk-ticket'])
    authority_token = authority.set('parent-private-authority')
    try:
        with physical.capture_physical_dispatch() as capture:
            title.maybe_auto_title(object(),'fixture-session','Explain the fixture',[])
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
            if errors:
                raise errors[0]
            assert len(capture.snapshot()) == 1
            report = evidence(capture)
        assert seen == [(None,None,None)]
        assert report['registered_work_settled'] is True
        assert report['handoffs'][0]['purpose'] == 'title_generation'
        assert report['requests'][0]['capture_handoff_id'] == report['handoffs'][0]['id']
        assert report['requests'][0]['execution_id'] is None
        assert 'parent-private-authority' not in json.dumps(report)
    finally:
        authority.reset(authority_token)
        physical._sdk_capture_tickets.reset(sdk_token)
        physical._call.reset(call_token)


def test_no_capture_returns_original_callable():
    callback = lambda: 'fixture'
    assert bind(callback) is callback


@pytest.mark.parametrize('inherit_flag',[0,1])
def test_title_entry_uses_fresh_context_under_real_interpreter_flags(tmp_path,inherit_flag):
    repo = Path(__file__).resolve().parents[2]
    code = '''from pathlib import Path
from contextvars import ContextVar
import json,sys,threading,httpx
from agent import title_generator as title
from ares_runtime import physical_dispatch as p
assert Path(p.__file__).resolve().parent.parent == Path(sys.argv[1])
real_thread = threading.Thread
threads,seen,errors = [],[],[]
authority=ContextVar("fixture-extra-authority",default=None)
def title_fixture(*args,**kwargs):
    try:
        state=(p._call.get(),p._sdk_capture_tickets.get(),authority.get())
        seen.append(state)
        assert state == (None,None,None), "parent context crossed title-thread entry"
        with p.bind_http_client(httpx.Client(transport=httpx.MockTransport(
            lambda request:httpx.Response(200,stream=httpx.ByteStream(b"fixture"))))) as client:
            client.post("https://fixture.invalid/v1/messages",json={"model":"fixture","messages":[],"max_tokens":1})
    except BaseException as error:
        errors.append(error)
def thread_factory(*args,**kwargs):
    thread=real_thread(*args,**kwargs); threads.append(thread); return thread
title.threading.Thread=thread_factory
title.auto_title_session=title_fixture
title._auto_title_enabled=lambda:True
title.is_titleable_user_message=lambda message:True
title.apply_instant_title=lambda *args,**kwargs:None
p._call.set({"synthetic-parent":"must-not-cross"})
p._sdk_capture_tickets.set(["synthetic-parent-ticket"])
authority.set("synthetic-parent-authority")
with p.capture_physical_dispatch() as capture:
    title.maybe_auto_title(object(),"fixture-session","Explain fixture",[])
    for thread in threads:
        thread.join(5); assert not thread.is_alive()
    if errors: raise errors[0]
    report=capture.evidence_snapshot()
assert seen == [(None,None,None)]
assert len(report["requests"]) == 1 and report["registered_work_settled"] is True
print(json.dumps({"thread_inherit_context":sys.flags.thread_inherit_context,"context_isolated":True,"requests":len(report["requests"])}))
'''
    env={'HOME':str(tmp_path),'HERMES_HOME':str(tmp_path/'ares'),'PATH':'/usr/bin:/bin',
         'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(repo)}
    result=subprocess.run([sys.executable,'-P','-X',f'thread_inherit_context={inherit_flag}',
                           '-c',code,str(repo)],env=env,text=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,timeout=20)
    assert result.returncode == 0,result.stderr
    report=json.loads(result.stdout)
    assert report == {'thread_inherit_context':inherit_flag,'context_isolated':True,'requests':1}



def test_scheduled_late_work_is_registered_and_old_snapshot_cannot_be_promoted():
    with physical.capture_physical_dispatch() as capture:
        callback = bind(send)
        pending = evidence(capture)
        assert pending['requests'] == []
        assert pending['handoffs'][0]['state'] == 'scheduled'
        assert pending['registered_work_settled'] is False
    run_thread(callback)
    final = evidence(capture)
    assert final['registered_work_settled'] is True
    assert final['handoffs'][0]['state'] == 'returned'
    assert len(final['requests']) == 1
    assert pending['requests'] == [] and pending['registered_work_settled'] is False
    assert physical._capture.get() is None


def test_nested_capture_does_not_borrow_outer_handoff_identity():
    inner_reports = []
    def callback():
        with physical.capture_physical_dispatch() as inner:
            send()
            inner_reports.append(evidence(inner))
        send()
    with physical.capture_physical_dispatch() as outer:
        run_thread(bind(callback))
        report = evidence(outer)
    assert len(report['requests']) == 1
    assert report['requests'][0]['capture_handoff_id'] == report['handoffs'][0]['id']
    assert inner_reports[0]['requests'][0]['capture_handoff_id'] is None
    assert inner_reports[0]['handoffs'] == []


def test_callback_error_is_preserved_without_message_and_context_is_restored():
    def fail():
        raise RuntimeError('private-fixture-message')
    with physical.capture_physical_dispatch() as owner:
        callback = bind(fail)
    with physical.capture_physical_dispatch() as other:
        with pytest.raises(RuntimeError,match='private-fixture-message'):
            callback()
        assert physical._capture.get() is other
    report = evidence(owner)
    assert report['handoffs'][0]['state'] == 'raised'
    assert report['handoffs'][0]['error_type'] == 'RuntimeError'
    assert report['registered_work_settled'] is True
    assert 'private-fixture-message' not in json.dumps(report)


def test_duplicate_invocation_is_observed_but_never_qualified_as_one_handoff():
    with physical.capture_physical_dispatch() as capture:
        callback = bind(send)
        callback()
        callback()
        report = evidence(capture)
    assert len(report['requests']) == 2
    assert report['registered_work_settled'] is False
    assert report['handoff_conflict'] is True


def test_concurrent_duplicate_invocation_preserves_distinct_observations():
    entered, both = threading.Event(), threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    entries, errors, threads = [], [], []
    def job():
        with lock:
            entries.append(True)
            entered.set()
            if len(entries) == 2:
                both.set()
        assert release.wait(timeout=5)
        send()
    with physical.capture_physical_dispatch() as capture:
        callback = bind(job)
        def invoke():
            try:
                callback()
            except BaseException as error:
                errors.append(error)
        try:
            for _ in range(2):
                thread = REAL_THREAD(target=invoke)
                threads.append(thread)
                thread.start()
            assert entered.wait(timeout=5) and both.wait(timeout=5)
            assert evidence(capture)['registered_work_settled'] is False
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
        if errors:
            raise errors[0]
        report = evidence(capture)
    assert report['handoff_conflict'] is True
    assert report['registered_work_settled'] is False
    assert len({item['capture_handoff_id'] for item in report['requests']}) == 2
    assert len(report['handoffs']) == len(report['requests']) == 2


def test_concurrent_snapshot_is_detached_and_aware_of_running_work():
    entered = [threading.Event() for _ in range(4)]
    release = threading.Event()
    errors, threads = [], []
    def job(index):
        entered[index].set()
        assert release.wait(timeout=5)
        send()
    with physical.capture_physical_dispatch() as capture:
        for index in range(4):
            target = bind(lambda i=index:job(i))
            def invoke(callback=target):
                try:
                    callback()
                except BaseException as error:
                    errors.append(error)
            thread = REAL_THREAD(target=invoke)
            threads.append(thread)
            thread.start()
        try:
            assert all(event.wait(timeout=5) for event in entered)
            pending = evidence(capture)
            assert pending['registered_work_settled'] is False
            assert all(item['state']=='running' for item in pending['handoffs'])
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
        if errors:
            raise errors[0]
        final = evidence(capture)
    assert final['registered_work_settled'] is True
    assert len(final['requests']) == len(final['handoffs']) == 4
    assert all(item['state']=='running' for item in pending['handoffs'])


def test_returned_worker_with_deferred_response_is_still_unsettled():
    with physical.bind_http_client(httpx.Client(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=httpx.ByteStream(b'fixture')))
    )) as client:
        with physical.capture_physical_dispatch() as capture:
            callback = bind(lambda:client.send(client.build_request('POST',
                'https://fixture.invalid/v1/messages',json={'model':'fixture','messages':[],'max_tokens':1}),stream=True))
            response = callback()
            pending = evidence(capture)
        assert pending['handoffs'][0]['state']=='returned'
        assert pending['registered_work_settled'] is False
        response.read()
        response.close()
    assert evidence(capture)['registered_work_settled'] is True


def test_oneshot_usage_retains_scheduled_work_as_incomplete(monkeypatch,tmp_path,capsys):
    import hermes_cli.oneshot as oneshot
    def fake_agent(*args,**kwargs):
        bind(lambda:None)
        return 'fixture answer',{}
    monkeypatch.setattr(oneshot,'_run_agent',fake_agent)
    path = tmp_path/'usage.json'
    assert oneshot.run_oneshot('fixture',reasoning='high',usage_file=str(path)) == 0
    report = json.loads(path.read_text())['native_request_evidence']
    assert report['requests'] == []
    assert report['handoffs'][0]['state'] == 'scheduled'
    assert report['registered_work_settled'] is False


def test_failed_thread_start_does_not_erase_registered_work(monkeypatch):
    from agent import title_generator as title
    class CannotStart:
        def __init__(self,*args,**kwargs):
            pass
        def start(self):
            raise RuntimeError('fixture start failure')
    monkeypatch.setattr(title.threading,'Thread',CannotStart)
    monkeypatch.setattr(title,'_auto_title_enabled',lambda:True)
    monkeypatch.setattr(title,'is_titleable_user_message',lambda message:True)
    monkeypatch.setattr(title,'apply_instant_title',lambda *args,**kwargs:None)
    with physical.capture_physical_dispatch() as capture:
        with pytest.raises(RuntimeError,match='fixture start failure'):
            title.maybe_auto_title(object(),'fixture-session','Explain fixture',[])
    report = evidence(capture)
    assert report['requests'] == []
    assert report['handoffs'][0]['state'] == 'scheduled'
    assert report['registered_work_settled'] is False


def test_returned_coroutine_is_not_mislabeled_settled_work():
    async def later():
        return None
    with physical.capture_physical_dispatch() as capture:
        callback = bind(lambda:later())
        coroutine = callback()
        try:
            report = evidence(capture)
            assert report['handoffs'][0]['state'] == 'returned_deferred'
            assert report['registered_work_settled'] is False
        finally:
            coroutine.close()


def test_returned_generator_is_not_mislabeled_settled_work():
    def later():
        yield None
    with physical.capture_physical_dispatch() as capture:
        generator = bind(later)()
        try:
            report = evidence(capture)
            assert report['handoffs'][0]['state'] == 'returned_deferred'
            assert report['registered_work_settled'] is False
        finally:
            generator.close()
