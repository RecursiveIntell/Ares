"""Real plugin imports: concurrency, failure ownership, and safe diagnostics."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import importlib
import os
from pathlib import Path
import sys
import threading
import types
import uuid

import pytest

from plugins import context_engine as engines


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    name = 'loading_test_' + uuid.uuid4().hex
    directory = tmp_path / name
    directory.mkdir()
    monkeypatch.setattr(engines, '_CONTEXT_ENGINE_PLUGINS_DIR', tmp_path)
    monkeypatch.setattr(engines, '__path__', [str(tmp_path)])
    sync_name = '_loading_sync_' + uuid.uuid4().hex
    sync = types.ModuleType(sync_name)
    sync.entered = threading.Event()
    sync.release = threading.Event()
    sync.calls = 0
    sync.fail = True
    monkeypatch.setitem(sys.modules, sync_name, sync)

    def write(source):
        (directory / '__init__.py').write_text(source)
        importlib.invalidate_caches()
        return name

    yield name, directory, sync_name, sync, write
    sync.release.set()
    prefix = 'plugins.context_engine.' + name
    for key in list(sys.modules):
        if key == prefix or key.startswith(prefix + '.'):
            sys.modules.pop(key, None)
    if hasattr(engines, name):
        delattr(engines, name)


SUCCESS = '''from types import SimpleNamespace
def register(ctx):
    ctx.register_context_engine(SimpleNamespace(name="fixture", probe_activation=lambda: None))
'''


def test_concurrent_load_waits_for_import_and_creates_distinct_instances(plugin):
    name, _, sync_name, sync, write = plugin
    write(f'import {sync_name} as sync\nsync.entered.set()\nassert sync.release.wait(8)\n' + SUCCESS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(engines.load_context_engine_strict, name)
        assert sync.entered.wait(5)
        second = pool.submit(engines.load_context_engine_strict, name)
        try:
            # Broken loader returns its partial module immediately. The fixed
            # import waits; the controller releases it without a timing-based
            # assertion about an intermediate state.
            try:
                second.result(timeout=2)
            except TimeoutError:
                pass
        finally:
            sync.release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a.name == b.name == 'fixture'
    assert a is not b
    assert engines.load_context_engine_strict(name) is not a


def test_register_failure_is_not_retried_as_another_constructor(plugin):
    name, _, sync_name, sync, write = plugin
    write(f'''import {sync_name} as sync
from agent.context_engine import ContextEngine
class Fallback(ContextEngine):
    name = "fallback"
    def update_from_response(self, usage): pass
    def should_compress(self, prompt_tokens=None): return False
    def compress(self, messages, current_tokens=None): return messages
    def __init__(self): sync.calls += 1
def register(ctx):
    raise ValueError("PRIVATE_CREDENTIAL_SENTINEL")
''')
    with pytest.raises(engines.ContextEngineActivationError) as failure:
        engines.load_context_engine_strict(name)
    assert isinstance(failure.value.__cause__, ValueError)
    assert failure.value.stage == 'registration'
    assert failure.value.diagnostic_id
    assert 'PRIVATE_CREDENTIAL_SENTINEL' not in str(failure.value)
    assert sync.calls == 0
    assert engines.load_context_engine(name) is None
    assert sync.calls == 0


@pytest.mark.parametrize('stage', ['import', 'construction', 'activation probe'])
def test_strict_errors_keep_cause_but_not_private_text(plugin, stage):
    name, _, _, _, write = plugin
    if stage == 'import':
        source = 'raise ImportError("PRIVATE_CREDENTIAL_SENTINEL")\n'
    elif stage == 'construction':
        source = '''from agent.context_engine import ContextEngine
class Broken(ContextEngine):
    name = "broken"
    def update_from_response(self, usage): pass
    def should_compress(self, prompt_tokens=None): return False
    def compress(self, messages, current_tokens=None): return messages
    def __init__(self): raise ValueError("PRIVATE_CREDENTIAL_SENTINEL")
'''
    else:
        source = '''from types import SimpleNamespace
def fail(): raise RuntimeError("PRIVATE_CREDENTIAL_SENTINEL")
def register(ctx): ctx.register_context_engine(SimpleNamespace(probe_activation=fail))
'''
    write(source)
    with pytest.raises(engines.ContextEngineActivationError) as failure:
        engines.load_context_engine_strict(name)
    assert failure.value.__cause__ is not None
    assert failure.value.stage == stage
    assert failure.value.diagnostic_id
    assert 'PRIVATE_CREDENTIAL_SENTINEL' not in str(failure.value)


def test_failed_submodule_is_not_cached_as_a_successful_partial_module(plugin):
    name, directory, sync_name, sync, write = plugin
    (directory / 'part.py').write_text(f'''import {sync_name} as sync
sync.calls += 1
if sync.fail: raise RuntimeError("private first failure")
VALUE = "ready"
''')
    write('from .part import VALUE\n' + SUCCESS)
    with pytest.raises(engines.ContextEngineActivationError):
        engines.load_context_engine_strict(name)
    assert f'plugins.context_engine.{name}.part' not in sys.modules
    assert f'plugins.context_engine.{name}' not in sys.modules
    sync.fail = False
    assert engines.load_context_engine_strict(name).name == 'fixture'
    assert sync.calls == 2


def test_successful_sibling_import_survives_parent_failure(plugin):
    name, directory, sync_name, sync, write = plugin
    (directory / 'good.py').write_text('VALUE = object()\n')
    write(f'''from .good import VALUE
import {sync_name} as sync
if sync.fail: raise RuntimeError("parent failed")
''' + SUCCESS)
    with pytest.raises(engines.ContextEngineActivationError):
        engines.load_context_engine_strict(name)
    sibling = sys.modules[f'plugins.context_engine.{name}.good']
    sync.fail = False
    assert engines.load_context_engine_strict(name).name == 'fixture'
    assert sys.modules[f'plugins.context_engine.{name}.good'] is sibling


def test_discovery_is_best_effort_after_strict_failure(plugin):
    name, _, _, _, write = plugin
    write('raise RuntimeError("private configuration detail")\n')
    assert engines.load_context_engine(name) is None
    assert engines.discover_context_engines() == [(name, '', False)]


@pytest.mark.linux_only
def test_actual_governor_concurrent_activation_and_reopen(tmp_path, monkeypatch):
    binary = os.environ.get('HERMES_TEST_CONTEXT_GOVERNOR_BIN')
    if not binary:
        pytest.skip('paired qualification must supply the exact native binary')
    assert Path(binary).is_file()
    from hermes_state import SessionDB
    from plugins.context_engine._context_governor.key_state import ContextGovernorKeyState

    home = tmp_path / 'fixture-profile'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CONTEXT_GOVERNOR_BIN', binary)
    ContextGovernorKeyState(home, binary).initialize_first_install().close()
    selection = home / 'context-governor/keys/current.json'
    original_selection = selection.read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: engines.load_context_engine_strict('ri-context-governor'), range(2)))
    assert a is not b
    assert a.name == b.name == 'ri-context-governor'
    with SessionDB(home / 'state.db') as db:
        db.create_session('fixture-session', source='desktop')
        db.append_message('fixture-session', 'user', 'Preserve this fixture input')
        a.on_session_start('fixture-session', session_db=db)
        b.on_session_start('fixture-session', session_db=db)
    with SessionDB(home / 'state.db') as reopened:
        c = engines.load_context_engine_strict('ri-context-governor')
        c.on_session_start('fixture-session', session_db=reopened)
        assert reopened.get_messages_as_conversation('fixture-session')[0]['content'] == 'Preserve this fixture input'
    assert selection.read_bytes() == original_selection
