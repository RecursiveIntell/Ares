import sqlite3
import json
import os
import pytest
import threading
from types import SimpleNamespace

from tools import delegate_tool as delegate


def test_unstarted_close_refusal_preserves_admission_reservation():
    child = SimpleNamespace(_subagent_id='sa-unstarted-retained')
    def close():
        child._close_cleanup_state = 'incomplete'
    child.close = close
    parent = SimpleNamespace(_active_children=[child], _active_children_lock=threading.Lock())
    delegate._register_subagent({'subagent_id': child._subagent_id, 'agent': child,
        'status': 'admitted', 'capacity_occupied': True})
    try:
        delegate._retire_unstarted_child(child, parent)
        assert child in parent._active_children
        record = next(r for r in delegate.list_active_subagents() if r['subagent_id'] == child._subagent_id)
        assert record['cleanup_state'] == 'incomplete'
        assert record['capacity_occupied'] is True
    finally:
        delegate._unregister_subagent(child._subagent_id, agent=child)


@pytest.mark.parametrize('failure', ['credential', 'close'])
def test_retirement_failure_keeps_occupied_owner_and_defers_terminal(monkeypatch, failure):
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: None)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    events, trace = [], []
    class Pool:
        def acquire_lease(self):
            return 'owned-lease'
        def current(self):
            return None
        def release_lease(self, lease):
            trace.append('release')
            if failure == 'credential':
                raise RuntimeError('release outcome unknown')
    class Child:
        _subagent_id = 'sa-retirement-error'
        session_id = _subagent_id
        _credential_pool = Pool()
        def tool_progress_callback(self, event, *args, **kwargs):
            events.append((event, kwargs))
        def run_conversation(self, **kwargs):
            return {'final_response': 'task completed', 'completed': True}
        def close(self):
            trace.append('close')
            raise RuntimeError('close outcome unknown')
    child = Child()
    parent = SimpleNamespace(session_id='retirement-parent', _current_task_id=None,
        _active_children=[child], _active_children_lock=threading.Lock())
    try:
        result = delegate._run_single_child(0, 'test', child, parent)
        assert result['status'] == 'completed'
        assert result['cleanup']['status'] == 'incomplete'
        assert child in parent._active_children
        record = next(r for r in delegate.list_active_subagents() if r['subagent_id'] == child._subagent_id)
        assert record['capacity_occupied'] is True
        assert record['cleanup_age_seconds'] >= 0
        assert not child._delegate_settled_event.is_set()
        assert not any(event == 'subagent.complete' for event, _ in events)
        if failure == 'credential':
            assert 'close' not in trace
    finally:
        delegate._unregister_subagent(child._subagent_id, agent=child)


def test_timeout_keeps_db_credential_capacity_until_final_persistence(tmp_path, monkeypatch):
    # Import costs belong to initialization. Warm them before accelerating the
    # admitted worker deadline so this case actually witnesses live execution.
    import model_tools  # noqa: F401
    import agent.delegation_context  # noqa: F401
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: 0.05)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    release = threading.Event()
    retired = threading.Event()
    trace = []
    db = sqlite3.connect(tmp_path / 'child.db', check_same_thread=False)
    db.execute('create table turns(value text)')
    class Pool:
        def acquire_lease(self):
            trace.append('acquire')
            return 'credential'
        def current(self):
            return None
        def release_lease(self, _id):
            trace.append('release')
    class Child:
        _subagent_id = 'sa-settlement'
        session_id = _subagent_id
        _delegate_role = 'leaf'
        tool_progress_callback = None
        _credential_pool = Pool()
        def run_conversation(self, **kwargs):
            release.wait(5)
            db.execute("insert into turns values('late final')")
            db.commit()
            trace.append('persist')
            return {'final_response': 'late', 'api_calls': 1}
        def interrupt(self, message=None, **kwargs):
            trace.append('stop')
        def get_activity_summary(self):
            return {'api_call_count': 1}
        def close(self):
            trace.append('close')
            db.close()
            retired.set()
    child = Child()
    parent = SimpleNamespace(session_id='parent', _current_task_id=None,
        _active_children=[child], _active_children_lock=threading.Lock())
    try:
        result = delegate._run_single_child(0, 'test', child, parent)
        assert result['status'] == 'timeout'
        assert result['cleanup']['status'] == 'incomplete'
        assert child in parent._active_children
        record = next(r for r in delegate.list_active_subagents() if r['subagent_id'] == child._subagent_id)
        assert record['capacity_occupied'] is True
        assert record['cleanup_age_seconds'] >= 0
        assert 'release' not in trace and 'close' not in trace
        monkeypatch.setattr(delegate, '_get_max_concurrent_children', lambda: 1)
        with pytest.raises(ValueError, match='DELEGATION_CAPACITY_OCCUPIED'):
            delegate._build_child_preserving_parent_tools(goal='refused', parent_agent=parent)
        assert delegate.interrupt_subagent(child._subagent_id)
        assert delegate.interrupt_subagent(child._subagent_id)
    finally:
        release.set()
    assert retired.wait(3)
    assert trace.index('persist') < trace.index('release') < trace.index('close')
    assert child not in parent._active_children
    assert result['cleanup']['status'] == 'incomplete'


@pytest.mark.parametrize('replace_generation', [False, True])
def test_terminal_projection_waits_for_settlement_and_stale_generation_is_ignored(monkeypatch, replace_generation):
    import model_tools  # noqa: F401
    import agent.delegation_context  # noqa: F401
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: 0.05)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    release, closed = threading.Event(), threading.Event()
    events = []
    class Child:
        _subagent_id = 'sa-projection'
        session_id = _subagent_id
        _delegate_role = 'leaf'
        def tool_progress_callback(self, event, *a, **kwargs):
            events.append((event, kwargs))
        def run_conversation(self, **kwargs):
            release.wait(5)
            return {'final_response': 'late', 'api_calls': 1}
        def interrupt(self, message=None, **kwargs):
            pass
        def get_activity_summary(self):
            return {'api_call_count': 1}
        def close(self):
            closed.set()
    child = Child()
    parent = SimpleNamespace(session_id='projection-parent', _current_task_id=None,
        _active_children=[child], _active_children_lock=threading.Lock())
    try:
        result = delegate._run_single_child(0, 'projection', child, parent)
        assert result['cleanup']['status'] == 'incomplete'
        assert not any(event == 'subagent.complete' for event, _ in events)
        assert any(kw.get('cleanup', {}).get('status') == 'incomplete' for _, kw in events)
        if replace_generation:
            replacement = SimpleNamespace()
            delegate._register_subagent({'subagent_id': child._subagent_id, 'agent': replacement,
                'status': 'running', 'goal': 'replacement'})
    finally:
        release.set()
    assert closed.wait(3)
    assert child._delegate_settled_event.wait(3)
    if replace_generation:
        assert not any(event == 'subagent.complete' for event, _ in events)
        with delegate._active_subagents_lock:
            assert delegate._active_subagents[child._subagent_id]['agent'] is replacement
        delegate._unregister_subagent(child._subagent_id, agent=replacement)
    else:
        terminals = [kw for event, kw in events if event == 'subagent.complete']
        assert len(terminals) == 1
        assert terminals[0]['status'] == 'timeout'
        assert terminals[0]['cleanup']['status'] == 'complete'
        assert result['status'] == 'timeout'


def test_owned_rpc_result_retains_exact_worker_and_turn_retirement(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from agent import agent_runtime_helpers
    from tools.code_execution_tool import _SandboxExecution
    release = threading.Event()
    entered = threading.Event()
    retired = threading.Event()
    db = sqlite3.connect(tmp_path / 'rpc.db', check_same_thread=False)
    db.execute('create table turns(value text)')
    execution = _SandboxExecution(threading.Event(), [], [0], 10)
    def rpc():
        execution.attach_worker()
        entered.set()
        release.wait(5)
        db.execute("insert into turns values('owned rpc')")
        db.commit()
        execution.finish_worker()
    worker = threading.Thread(target=rpc, name='sandbox-rpc-lifecycle-witness', daemon=True)
    worker.sandbox_execution = execution
    worker.start()
    assert entered.wait(2)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = 'child'
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.interrupt = lambda **kw: None
    agent._active_session_turn_lease_holder = 'lease'
    agent._session_db = db
    agent._owns_session_db = True
    monkeypatch.setattr(agent_runtime_helpers, 'invoke_tool', lambda *a: json.dumps({
        'cleanup': {'status': 'incomplete', 'supervision_required': True,
            'rpc_thread_name': worker.name}}))
    try:
        agent._invoke_tool('execute_code', {}, 'child')
        assert agent._has_owned_tool_work()
        agent.close()
        assert agent._owns_session_db is True
        assert db.execute('select count(*) from turns').fetchone()[0] == 0
        def retire():
            assert db.execute('select count(*) from turns').fetchone()[0] == 1
            agent._active_session_turn_lease_holder = None
            db.close()
            retired.set()
        assert agent._defer_owned_tool_retirement(retire)
        assert not retired.is_set()
    finally:
        release.set()
    assert retired.wait(3)
    worker.join(2)


def test_lost_rpc_witness_keeps_real_live_reader_db_open(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from agent import agent_runtime_helpers
    db = sqlite3.connect(tmp_path / 'lost-rpc.db', check_same_thread=False)
    db.execute('create table writes(value text)')
    read_fd, write_fd = os.pipe()
    started, persisted = threading.Event(), threading.Event()
    def reader():
        started.set()
        os.read(read_fd, 1)
        db.execute("insert into writes values('reader settled')")
        db.commit()
        persisted.set()
    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    assert started.wait(2)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = 'lost-rpc'
    agent._session_db = db
    agent._owns_session_db = True
    agent.interrupt = lambda **kwargs: None
    # The named RPC exited after producing this snapshot; its independent
    # output reader still owns DB-capable work. Absence is not settlement.
    monkeypatch.setattr(agent_runtime_helpers, 'invoke_tool', lambda *a: json.dumps({
        'cleanup': {'status': 'incomplete', 'supervision_required': True,
            'rpc_thread_name': 'sandbox-rpc-absent-witness', 'output_drained': False}}))
    try:
        agent._invoke_tool('execute_code', {}, agent.session_id)
        agent.close()
        assert agent._close_cleanup_state == 'incomplete'
        assert agent._owns_session_db is True
        assert db.execute('select count(*) from writes').fetchone()[0] == 0
    finally:
        os.write(write_fd, b'x')
        worker.join(2)
        os.close(read_fd)
        os.close(write_fd)
    assert persisted.is_set()
    db.close()


def test_parent_close_retains_child_supervisor_and_sibling_client(monkeypatch):
    from run_agent import AIAgent
    from concurrent.futures import Future
    parent = AIAgent.__new__(AIAgent)
    child = AIAgent.__new__(AIAgent)
    future = Future()
    future.set_running_or_notify_cancel()
    child._delegate_worker_future = future
    child._delegate_cleanup_state = 'incomplete'
    child._active_children = []
    child._active_children_lock = threading.Lock()
    child.interrupt = lambda **kw: None
    parent._active_children = [child]
    parent._active_children_lock = threading.Lock()
    parent.interrupt = lambda **kw: None
    class Client:
        closed = False
        def close(self):
            self.closed = True
    shared = Client()
    parent.client = shared
    parent.close()
    child.close()
    assert parent._active_children == [child]
    assert parent.client is shared and not shared.closed
    assert parent._close_cleanup_state == child._close_cleanup_state == 'incomplete'
    future.set_result(None)


def test_no_cap_stop_retains_real_blocking_pipe_and_sibling(monkeypatch):
    import model_tools  # noqa: F401
    import agent.delegation_context  # noqa: F401
    monkeypatch.setattr(delegate, '_get_child_timeout', lambda: None)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    read_fd, write_fd = os.pipe()
    entered, retired, returned = threading.Event(), threading.Event(), threading.Event()
    trace = []
    results = []
    class Child:
        _subagent_id = 'sa-real-pipe'
        session_id = _subagent_id
        _delegate_role = 'leaf'
        tool_progress_callback = None
        def run_conversation(self, **kwargs):
            entered.set()
            trace.append(os.read(read_fd, 1))
            return {'final_response': 'late', 'api_calls': 1}
        def interrupt(self, message=None, **kwargs):
            trace.append('stop')
        def get_activity_summary(self):
            return {'api_call_count': 1}
        def close(self):
            os.close(read_fd)
            retired.set()
    child = Child()
    sibling = SimpleNamespace(closed=False)
    parent = SimpleNamespace(session_id='pipe-parent', _current_task_id=None,
        _active_children=[child, sibling], _active_children_lock=threading.Lock())
    def call():
        results.append(delegate._run_single_child(0, 'pipe', child, parent))
        returned.set()
    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    assert entered.wait(2)
    try:
        assert delegate.interrupt_subagent(child._subagent_id)
        assert returned.wait(3), 'Stop must release its caller while retaining execution'
        assert results[0]['status'] == 'interrupted'
        assert results[0]['cleanup']['status'] == 'incomplete'
        assert child in parent._active_children
        assert not retired.is_set()
        assert delegate.interrupt_subagent(child._subagent_id)
        assert trace.count('stop') == 2  # control request plus finalizer-owned stop
        assert not sibling.closed
    finally:
        os.write(write_fd, b'x')
        os.close(write_fd)
    assert retired.wait(3)
    caller.join(2)
    assert parent._active_children == [sibling]
    assert trace[-1] == b'x'


@pytest.mark.parametrize('release_fails', [False, True])
def test_actual_turn_retains_native_lease_until_owned_rpc_persists(tmp_path, monkeypatch, release_fails):
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.code_execution_tool import _SandboxExecution
    from agent import agent_runtime_helpers
    db = SessionDB(tmp_path / 'native.db')
    db.create_session('child-turn', source='subagent')
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = 'child-turn'
    agent.platform = 'subagent'
    agent.model = 'harness'
    agent._session_db = db
    agent._owns_session_db = True
    agent._session_db_created = True
    agent._session_init_model_config = None
    agent._cached_system_prompt = None
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: 'child-turn'
    agent.log_prefix = ''
    agent._vprint = lambda *a, **kw: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._session_turn_lease_refresh_interval = 0.02
    terminal_returned, refreshed = threading.Event(), threading.Event()
    refresh = db.refresh_session_turn_lease
    def refresh_owned(*args, **kwargs):
        result = refresh(*args, **kwargs)
        if terminal_returned.is_set():
            refreshed.set()
        return result
    monkeypatch.setattr(db, 'refresh_session_turn_lease', refresh_owned)
    if release_fails:
        def refuse_release(*args, **kwargs):
            raise OSError('native release outcome unknown')
        monkeypatch.setattr(db, 'release_session_turn_lease', refuse_release)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    execution = _SandboxExecution(threading.Event(), [], [0], 10)
    def rpc():
        execution.attach_worker()
        entered.set()
        release.wait(5)
        db.set_meta('owned-late-write', 'persisted')
        execution.finish_worker()
        done.set()
    worker = threading.Thread(target=rpc, name='sandbox-rpc-native-turn-witness', daemon=True)
    worker.sandbox_execution = execution
    worker.start()
    assert entered.wait(2)
    def loop(current, *a, **kw):
        current._invoke_tool('execute_code', {}, 'child-turn')
        return {'final_response': 'harness terminal', 'messages': []}
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    monkeypatch.setattr(agent_runtime_helpers, 'invoke_tool', lambda *a: json.dumps({
        'cleanup': {'status': 'incomplete', 'supervision_required': True,
            'rpc_thread_name': worker.name}}))
    try:
        result = agent.run_conversation('harness', conversation_history=[])
        terminal_returned.set()
        assert result['final_response'] == 'harness terminal'
        assert db._conn.execute('select count(*) from session_turn_leases').fetchone()[0] == 1
        assert agent._active_session_turn_lease_holder
        assert db.get_meta('owned-late-write') is None
        assert refreshed.wait(1), 'owned live RPC lease stopped renewing after terminal result'
    finally:
        release.set()
    assert done.wait(3)
    if release_fails:
        assert not agent._owned_turn_retired.wait(0.3)
        assert agent._active_session_turn_lease_holder
        assert db._conn.execute('select count(*) from session_turn_leases').fetchone()[0] == 1
        agent.interrupt = lambda **kw: None
        agent.close()
        assert agent._close_cleanup_state == 'incomplete'
        assert agent._owns_session_db is True
    else:
        assert agent._owned_turn_retired.wait(3)
        assert db._conn.execute('select count(*) from session_turn_leases').fetchone()[0] == 0
        assert agent._active_session_turn_lease_holder is None
    assert db.get_meta('owned-late-write') == 'persisted'
    worker.join(2)
    db.close()
