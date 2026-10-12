"""Profile Models effort writes and reads use the target profile, never a backend pool."""
from types import SimpleNamespace
import threading

import pytest
import yaml

from tui_gateway import server


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(source))
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda _name: target)
    (source / 'config.yaml').write_text(yaml.safe_dump({'agent': {'reasoning_effort': 'low'}}))
    (target / 'config.yaml').write_text(yaml.safe_dump({
        'model': {'provider': 'openai-codex', 'default': 'gpt-6-luna'},
        'agent': {'reasoning_effort': 'high'}, 'display': {'fixture': 'preserved'},
    }))
    return source, target


@pytest.mark.parametrize('value,expected', [('xhigh', 'xhigh'), (' HIGH ', 'high'), ('none', 'none'), (False, 'none')])
def test_effort_write_persists_and_reports_target_owned_success(profiles, value, expected):
    source, target = profiles
    before = (source / 'config.yaml').read_bytes()
    response = server._methods['profiles.configure']('fixture', {'name': 'fixture', 'reasoning_effort': value})
    assert response['result']['applied']['reasoning_effort'] is True
    after = yaml.safe_load((target / 'config.yaml').read_text())
    assert after['agent']['reasoning_effort'] == expected
    assert after['model'] == {'provider': 'openai-codex', 'default': 'gpt-6-luna'}
    assert after['display']['fixture'] == 'preserved'
    assert (source / 'config.yaml').read_bytes() == before


@pytest.mark.parametrize('value', ['invalid', '', None, True, {}, []])
def test_invalid_effort_is_rejected_without_mutating_any_profile(profiles, value):
    source, target = profiles
    before = [(p / 'config.yaml').read_bytes() for p in (source, target)]
    response = server._methods['profiles.configure']('fixture', {'name': 'fixture', 'reasoning_effort': value})
    assert response.get('error'), response
    assert [(p / 'config.yaml').read_bytes() for p in (source, target)] == before


@pytest.mark.parametrize('value,expected', [
    ('high', 'high'), (' HIGH ', 'high'), (False, 'none'), ('disabled', 'none'),
    ('xhigh', 'xhigh'), ('invalid', None), (None, None),
])
def test_profile_roster_reads_effort_from_each_target_without_live_backend(profiles, monkeypatch, value, expected):
    source, target = profiles
    cfg = yaml.safe_load((target / 'config.yaml').read_text())
    cfg['agent']['reasoning_effort'] = value
    (target / 'config.yaml').write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr('hermes_cli.profiles.list_profiles', lambda: [SimpleNamespace(
        name='fixture', path=target, is_default=False, model='gpt-6-luna', provider='openai-codex',
    )])
    before = [(p / 'config.yaml').read_bytes() for p in (source, target)]
    response = server._methods['profiles.list']('list', {'include_sessions': False})
    assert response['result']['profiles'][0]['reasoning_effort'] == expected
    assert [(p / 'config.yaml').read_bytes() for p in (source, target)] == before


def test_failed_save_never_reports_applied(profiles, monkeypatch):
    monkeypatch.setattr('hermes_cli.config.save_config', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('fixture')))
    response = server._methods['profiles.configure']('fixture', {'name': 'fixture', 'reasoning_effort': 'xhigh'})
    assert response['result']['applied']['reasoning_effort'] is False
    assert response['result']['ok'] is False


def test_roster_scope_is_context_local_during_another_profiles_write(profiles, tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_cli import config as config_owner

    source, target = profiles
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'config.yaml').write_text(yaml.safe_dump({'agent': {'reasoning_effort': 'low'}}))
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda _name: other)
    monkeypatch.setattr('hermes_cli.profiles.list_profiles', lambda: [SimpleNamespace(
        name='fixture', path=target, is_default=False, model='gpt-6-luna', provider='openai-codex',
    )])
    entered = threading.Event()
    release = threading.Event()
    original_read = config_owner.read_user_config_raw
    output = {}

    def blocked_read(*args, **kwargs):
        if threading.current_thread().name == 'roster-fixture':
            entered.set()
            assert release.wait(10), 'fixture writer did not release roster read'
        return original_read(*args, **kwargs)

    monkeypatch.setattr(config_owner, 'read_user_config_raw', blocked_read)

    def read_roster():
        output['response'] = server._methods['profiles.list']('roster', {'include_sessions': False})
        output['restored_home'] = get_hermes_home()

    reader = threading.Thread(target=read_roster, name='roster-fixture')
    reader.start()
    try:
        assert entered.wait(10)
        # The roster thread currently holds the target override. It must not
        # alter the caller thread's owner or a simultaneous unrelated write.
        assert get_hermes_home() == source
        written = server._methods['profiles.configure']('writer', {'name': 'other', 'reasoning_effort': 'xhigh'})
        assert written['result']['applied']['reasoning_effort'] is True
        assert get_hermes_home() == source
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive()
    assert output['restored_home'] == source
    assert output['response']['result']['profiles'][0]['reasoning_effort'] == 'high'
    assert yaml.safe_load((target / 'config.yaml').read_text())['agent']['reasoning_effort'] == 'high'
    assert yaml.safe_load((other / 'config.yaml').read_text())['agent']['reasoning_effort'] == 'xhigh'


def test_roster_read_failure_restores_scope_without_borrowing_source_effort(profiles, monkeypatch):
    from hermes_constants import get_hermes_home

    source, target = profiles
    monkeypatch.setattr('hermes_cli.profiles.list_profiles', lambda: [SimpleNamespace(
        name='fixture', path=target, is_default=False, model='gpt-6-luna', provider='openai-codex',
    )])
    monkeypatch.setattr('hermes_cli.config.read_user_config_raw', lambda: (_ for _ in ()).throw(OSError('fixture')))
    response = server._methods['profiles.list']('list', {'include_sessions': False})
    assert response['result']['profiles'][0]['reasoning_effort'] is None
    assert get_hermes_home() == source
