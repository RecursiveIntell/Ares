"""Real constructor/reconciliation ownership under a controlled Linux helper fault."""
from concurrent.futures import Future
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from unittest.mock import patch
import uuid

import pytest

from hermes_state import SessionDB
from plugins.context_engine._context_governor import ContextGovernorEngine
from plugins.context_engine._context_governor.protocol import ContextGovernorCancellationIndeterminate
from tools import delegate_tool as delegate


def _support(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_host = _support('governor_constructor_host', Path(__file__).parents[1] / 'run_agent/test_ares_context_initialization.py')
_parent = _support('governor_constructor_parent', Path(__file__).with_name('test_delegate.py'))


@pytest.mark.linux_only
@pytest.mark.parametrize('outcome', ['indeterminate', 'settled_timeout', 'success'])
def test_failed_real_constructor_retains_only_unsettled_governor(tmp_path, monkeypatch, outcome):
    parent_db = SessionDB(tmp_path / 'state.db')
    parent = _parent._make_mock_parent()
    parent.session_id = 'governor-constructor-' + uuid.uuid4().hex
    parent._session_db = parent_db
    parent.enabled_toolsets = ['file']
    parent.disabled_toolsets = []
    parent._close_cleanup_state = None
    parent_retried = threading.Event()
    parent.close = parent_retried.set
    owner_done = Future()
    helpers, settlers = [], []
    original_popen, real_thread = subprocess.Popen, threading.Thread

    class Binding:
        pass_fds = ()

        def command_args(self):
            return ()

        def close(self):
            pass

    def spawn(command, *args, **kwargs):
        assert command[1] == 'pending-v2', command
        body = ('import json,sys; json.load(sys.stdin); print("[]")' if outcome == 'success'
                else 'import time; time.sleep(60)')
        proc = original_popen([sys.executable, '-c', body], *args, **kwargs)
        helpers.append(proc)
        return proc

    def refuse_containment(proc):
        assert proc in helpers and proc.poll() is None
        raise ContextGovernorCancellationIndeterminate('controlled exact helper containment refusal')

    def observe_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        if kwargs.get('name', '').startswith('initialization-settlement-'):
            settlers.append(worker)
        return worker

    try:
        with ExitStack() as stack:
            stack.enter_context(patch('hermes_cli.config.load_config', return_value={}))
            engine = ContextGovernorEngine(binary=sys.executable, store_dir=str(tmp_path / 'store'))
            engine.timeout_sec = 2 if outcome == 'success' else 0.1
            engine._capabilities = {'failure_envelope': {'schema': 'ContextGovernorFailureV1',
                'flag': '--failure-envelope-v1', 'stream': 'stderr'}}
            engine._key_state = type('KeyState', (), {'active_binding': staticmethod(Binding)})()
            engine.probe_activation = lambda: engine._capabilities
            monkeypatch.setattr(engine, 'on_session_end', lambda *args, **kwargs: pytest.fail('failed constructor ended a logical session'))
            if outcome == 'indeterminate':
                monkeypatch.setattr(engine, '_terminate_process_group', refuse_containment)
            stack.enter_context(_host._host_init(engine))
            stack.enter_context(patch('agent.model_metadata.get_model_context_length', return_value=32_000))
            stack.enter_context(patch('agent.context_compressor.get_model_context_length', return_value=32_000))
            monkeypatch.setattr(delegate, '_load_config', lambda: {})
            monkeypatch.setattr(delegate, '_get_child_lifetime_policy', lambda: delegate.DelegationLifetimePolicy('total', None))
            monkeypatch.setattr(delegate, '_get_max_concurrent_children', lambda: 1)
            monkeypatch.setattr(subprocess, 'Popen', spawn)
            monkeypatch.setattr(threading, 'Thread', observe_thread)
            with pytest.raises(ValueError, match='below the minimum'):
                delegate._build_child_preserving_parent_tools(task_index=0,
                    goal='neutral actual governor initialization fault', context=None,
                    toolsets=None, model=None, max_iterations=1, task_count=1,
                    parent_agent=parent, _caller_initialization_done=owner_done)
            assert len(helpers) == 1
            with delegate._active_subagents_lock:
                before_ack = {key: row for key, row in delegate._active_subagents.items()
                    if row.get('owner_agent_session_id') == parent.session_id}
            assert len(before_ack) == 1
            assert not parent_retried.is_set()
            parent._close_cleanup_state = 'incomplete'
            owner_done.set_result(None)
            assert len(settlers) == 1
            settlers[0].join(5)
            assert not settlers[0].is_alive()
            with delegate._active_subagents_lock:
                records = {key: row for key, row in delegate._active_subagents.items()
                    if row.get('owner_agent_session_id') == parent.session_id}
            if outcome == 'indeterminate':
                assert helpers[0].poll() is None
                assert len(records) == 1, 'live uncertain Governor lost its canonical reservation'
                row = next(iter(records.values()))
                assert row['capacity_occupied'] is True
                assert row['cleanup_state'] == 'incomplete'
                assert row['initialization_resources'].agent.context_compressor is engine
                assert engine.has_unsettled_work() is True
                with pytest.raises(ContextGovernorCancellationIndeterminate):
                    engine.shutdown()
                assert helpers[0].poll() is None
                assert not parent_retried.is_set(), 'parent retirement entered with live child-owned work'
                json.dumps(delegate.list_active_subagents())
                with pytest.raises(ContextGovernorCancellationIndeterminate):
                    engine._run_json(['pending-v2'], {})
                assert len(helpers) == 1, 'unsettled engine admitted another subprocess'
                # Only the controller retires this harmless process. An explicit
                # engine shutdown must observe settlement before forgetting it.
                os.killpg(helpers[0].pid, signal.SIGKILL)
                helpers[0].communicate(timeout=3)
                monkeypatch.setattr(engine, '_terminate_process_group', ContextGovernorEngine._terminate_process_group)
                engine.shutdown()
                assert engine.has_unsettled_work() is False
            else:
                assert records == {}, 'settled constructor retained false capacity'
                assert parent_retried.is_set()
                assert helpers[0].poll() is not None
    finally:
        for proc in helpers:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=3)
        parent_db.close()
        with delegate._active_subagents_lock:
            for key, row in list(delegate._active_subagents.items()):
                if row.get('owner_agent_session_id') == parent.session_id:
                    delegate._active_subagents.pop(key, None)
