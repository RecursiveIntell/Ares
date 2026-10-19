"""Real compute-host join at the producer's pre-start handoff barrier."""
import io
import json
import threading
from types import SimpleNamespace
import pytest
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost

class ObservedLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.reader_waiting = threading.Event()
    def __enter__(self):
        if threading.current_thread().name == "host-reader":
            self.reader_waiting.set()
        self.lock.acquire()
        return self
    def __exit__(self, *args):
        self.lock.release()


def test_real_host_waits_for_published_successor_to_start(monkeypatch):
    lock = ObservedLock()
    monkeypatch.setattr(server, "_sessions_lock", lock)
    session = {"agent":SimpleNamespace(session_id="stored",model="fixture",provider="fixture"),
        "session_key":"stored", "history":[], "history_lock":threading.Lock(),
        "history_version":0, "running":False, "_turn_cancel_requested":False}
    monkeypatch.setattr(server,"_sessions",{"s":session})
    for name in ["_ensure_session_db_row","_persist_branch_seed","_start_inflight_turn","_begin_turn_outcome","_clear_inflight_turn"]:
        monkeypatch.setattr(server,name,lambda *a,**kw:None)
    monkeypatch.setattr(server,"_session_info",lambda *a,**kw:{"model":"fixture"})
    published,release,submit_returned=threading.Event(),threading.Event(),threading.Event()
    failures=[]
    def child():
        with session["history_lock"]:session["running"]=False
    successor=threading.Thread(target=child,name="successor")
    def publish():
        try:
            # Exact production handoff order, held at the intentional pre-start
            # point; the consumer must share this existing serialization lock.
            with lock:
                session["_run_thread"]=successor
                published.set()
                if not release.wait(10):raise AssertionError("controller did not release start")
                successor.start()
        except BaseException as e:failures.append(e)
    def submit(*args,**kwargs):
        assert published.wait(5)
        submit_returned.set()
    monkeypatch.setattr(server,"_run_prompt_submit",submit)
    output=io.StringIO();host=ComputeHost(stdout=output,heartbeat_secs=0)
    monkeypatch.setattr(host,"_ensure_server_session",lambda *a:session)
    writer=threading.Thread(target=publish,name="publisher")
    reader=threading.Thread(target=lambda:host._run_real_turn({"sid":"s","request_id":"r","text":"fixture"}),name="host-reader")
    try:
        writer.start();assert published.wait(5)
        reader.start();assert submit_returned.wait(5)
        # A positive acquire-attempt witness, not an arbitrary sleep. On old
        # code the reader instead joins the real unstarted Thread and errors.
        observed=lock.reader_waiting.wait(3)
        release.set();writer.join(5);reader.join(5)
        if successor.ident is not None:successor.join(5)
        assert not writer.is_alive() and not reader.is_alive()
        assert not failures
        frames=[json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        assert [f["type"] for f in frames]==["turn.started","turn.end"],json.dumps(frames)
        assert observed
    finally:
        release.set()
        writer.join(5)
        if reader.ident is not None:reader.join(5)
        if successor.ident is not None:successor.join(5)
        host.close()


def test_start_publication_is_serialized_and_start_failure_restores_previous(monkeypatch):
    lock=threading.RLock();monkeypatch.setattr(server,"_sessions_lock",lock)
    previous=object();session={"_run_thread":previous}
    class CannotStart:
        def start(self):
            assert lock._is_owned()
            assert session["_run_thread"] is self
            raise RuntimeError("thread creation refused")
    with pytest.raises(RuntimeError,match="thread creation refused"):
        server._start_run_thread(session,CannotStart())
    assert session["_run_thread"] is previous


def test_first_start_failure_does_not_leave_an_unstarted_handle(monkeypatch):
    session={}
    bad=SimpleNamespace(start=lambda:(_ for _ in ()).throw(RuntimeError("start failed")))
    with pytest.raises(RuntimeError,match="start failed"):
        server._start_run_thread(session,bad)
    assert "_run_thread" not in session


def test_started_target_is_retained_if_start_ack_is_interrupted(monkeypatch):
    session={};finished=threading.Event()
    class InterruptedStart(threading.Thread):
        def start(self):
            super().start()
            raise KeyboardInterrupt("after native bootstrap")
    worker=InterruptedStart(target=finished.set)
    with pytest.raises(KeyboardInterrupt,match="after native bootstrap"):
        server._start_run_thread(session,worker)
    worker.join(5)
    assert finished.is_set() and not worker.is_alive()
    assert server._get_run_thread(session) is worker


def test_immediate_successor_is_not_overwritten_by_its_predecessor():
    session={};ran=threading.Event()
    successor=threading.Thread(target=ran.set)
    predecessor=threading.Thread(target=lambda:server._start_run_thread(session,successor))
    server._start_run_thread(session,predecessor)
    predecessor.join(5)
    assert not predecessor.is_alive()
    successor.join(5)
    assert ran.is_set() and not successor.is_alive()
    assert server._get_run_thread(session) is successor
