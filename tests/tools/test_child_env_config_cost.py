"""Bound config reads while retaining child-environment security decisions."""
from hermes_cli import config
from tools.environments.local import _make_run_env, _sanitize_subprocess_env
import pytest


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
@pytest.mark.parametrize('count', [1, 30, 300])
def test_provider_blocklist_builds_are_bounded_per_spawn(tmp_path, monkeypatch, builder, count):
    import yaml
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments import local

    names = [f'SYNTHETIC_ALLOWED_{i}' for i in range(count)]
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'terminal': {'env_passthrough': names},
    }))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    original = local._build_provider_env_blocklist
    builds = []

    def counted():
        builds.append(None)
        return original()

    monkeypatch.setattr(local, '_build_provider_env_blocklist', counted)
    try:
        child = builder({name: 'safe-value' for name in names})
        assert all(child.get(name) == 'safe-value' for name in names)
        assert 1 <= len(builds) <= 2, f'provider policy built {len(builds)} times'
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_provider_blocklist_refreshes_for_next_spawn(tmp_path, monkeypatch, builder):
    import yaml
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    name = 'SYNTHETIC_DYNAMIC_CREDENTIAL'
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'terminal': {'env_passthrough': [name]},
    }))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        assert builder({name: 'synthetic-value'})[name] == 'synthetic-value'
        monkeypatch.setitem(config.OPTIONAL_ENV_VARS, name, {'category': 'tool'})
        assert name not in builder({name: 'synthetic-value'})
        monkeypatch.delitem(config.OPTIONAL_ENV_VARS, name)
        assert builder({name: 'synthetic-value'})[name] == 'synthetic-value'
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_provider_blocklist_build_failure_refuses_spawn(tmp_path, monkeypatch, builder):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments import local

    (tmp_path / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    def fail():
        raise RuntimeError('synthetic provider policy failure')
    monkeypatch.setattr(local, '_build_provider_env_blocklist', fail)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='synthetic provider policy failure'):
            builder({'OPENAI_API_KEY': 'synthetic-canary'})
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_provider_policy_change_during_spawn_refuses_environment(tmp_path, monkeypatch, builder):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments import local

    name = 'SYNTHETIC_LATE_PROVIDER_CREDENTIAL'
    (tmp_path / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    original = local._finalize_child_env_policy
    def change_after_filter(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setitem(config.OPTIONAL_ENV_VARS, name, {'category': 'tool'})
        return result
    monkeypatch.setattr(local, '_finalize_child_env_policy', change_after_filter)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='provider environment policy changed'):
            builder({name: 'synthetic-canary'})
    finally:
        reset_hermes_home_override(token)


def test_subprocess_sanitizer_config_reads_do_not_scale(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    (tmp_path / 'config.yaml').write_text('terminal:\n  env_passthrough: []\n')
    token = set_hermes_home_override(str(tmp_path))
    try:
        reads = 0
        original = config.read_raw_config
        original_security = config.read_security_policy_config
        original_capture = config.capture_security_policy_config

        def counted():
            nonlocal reads
            reads += 1
            return original()

        def counted_security(*args, **kwargs):
            nonlocal reads
            reads += 1
            return original_security(*args, **kwargs)

        def counted_capture(*args, **kwargs):
            nonlocal reads
            reads += 1
            return original_capture(*args, **kwargs)

        monkeypatch.setattr(config, 'read_raw_config', counted)
        monkeypatch.setattr(config, 'read_security_policy_config', counted_security)
        monkeypatch.setattr(config, 'capture_security_policy_config', counted_capture)
        child = _sanitize_subprocess_env({f'BENCH_NONSECRET_{i}': 'x' for i in range(300)})
        assert child['BENCH_NONSECRET_1'] == 'x'
        assert 1 <= reads <= 2, f'config parses outside bounded range: {reads}'
    finally:
        reset_hermes_home_override(token)


def test_concurrent_profile_contexts_keep_token_and_passthrough_separate(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import yaml
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.env_passthrough import get_all_passthrough

    homes = [tmp_path / 'a', tmp_path / 'b']
    for home, name in zip(homes, ('A', 'B')):
        home.mkdir()
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'secrets': {'bitwarden': {'access_token_env': f'{name}_PRIVATE'}},
            'terminal': {'env_passthrough': [f'{name}_PASSWORD']},
        }))
    barrier = Barrier(2)
    def worker(home, name):
        token = set_hermes_home_override(str(home))
        try:
            barrier.wait(timeout=10)
            for _ in range(10):
                child = _sanitize_subprocess_env({f'{name}_PRIVATE': 'secret', f'{name}_PASSWORD': 'allowed'})
                assert f'{name}_PRIVATE' not in child
                assert child[f'{name}_PASSWORD'] == 'allowed'
                assert f'{name}_PASSWORD' in get_all_passthrough()
                assert f'{"B" if name == "A" else "A"}_PASSWORD' not in get_all_passthrough()
        finally:
            reset_hermes_home_override(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, home, name) for home, name in zip(homes, ('A', 'B'))]
        for future in futures:
            future.result(timeout=30)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_six_hundred_values_use_bounded_policy_accessors_and_block_bootstrap(tmp_path, monkeypatch, builder):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    (tmp_path / 'config.yaml').write_text(
        'secrets:\n  bitwarden:\n    access_token_env: SYNTHETIC_BOOTSTRAP\n'
    )
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    calls = []
    for name in ('read_raw_config', 'read_security_policy_config', 'capture_security_policy_config'):
        original = getattr(config, name)
        def counted(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(config, name, counted)
    values = {f'PLAN_SAFE_VALUE_{i}': 'fixture-value' for i in range(600)}
    values['SYNTHETIC_BOOTSTRAP'] = 'fixture-private'
    token = set_hermes_home_override(tmp_path)
    try:
        child = builder(values)
    finally:
        reset_hermes_home_override(token)
    assert all(child.get(key) == value for key, value in values.items()
               if key != 'SYNTHETIC_BOOTSTRAP')
    assert 'SYNTHETIC_BOOTSTRAP' not in child
    assert 1 <= len(calls) <= 2, f'selected policy/config accessor calls: {calls}'


def test_environment_config_reads_do_not_scale_with_variable_count(monkeypatch):
    reads = 0
    original = config.read_raw_config
    original_security = config.read_security_policy_config
    original_capture = config.capture_security_policy_config

    def counted():
        nonlocal reads
        reads += 1
        return original()

    def counted_security(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original_security(*args, **kwargs)

    def counted_capture(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(config, 'read_raw_config', counted)
    monkeypatch.setattr(config, 'read_security_policy_config', counted_security)
    monkeypatch.setattr(config, 'capture_security_policy_config', counted_capture)
    env = {f'RELIABILITY_NONSECRET_{i}': 'test' for i in range(300)}
    child = _make_run_env(env)
    assert child['RELIABILITY_NONSECRET_1'] == 'test'
    assert 1 <= reads <= 2, f'config parses outside bounded range: {reads}'


def test_sequential_profiles_and_revision_remain_isolated(tmp_path, monkeypatch):
    import yaml
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.env_passthrough import clear_env_passthrough, get_all_passthrough

    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    for home, token_name, allowed in ((a, 'ALPHA_PRIVATE', 'ALPHA_PASSWORD'),
                                      (b, 'BETA_PRIVATE', 'BETA_PASSWORD')):
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'secrets': {'bitwarden': {'access_token_env': token_name}},
            'terminal': {'env_passthrough': [allowed]},
        }))
    for key in ('ALPHA_PRIVATE', 'BETA_PRIVATE', 'ALPHA_PASSWORD', 'BETA_PASSWORD'):
        monkeypatch.setenv(key, 'fixture')
    clear_env_passthrough()
    for home, secret in ((a, 'ALPHA_PRIVATE'), (b, 'BETA_PRIVATE'), (a, 'ALPHA_PRIVATE')):
        token = set_hermes_home_override(str(home))
        try:
            child = _make_run_env({})
            assert secret not in child
            expected = 'ALPHA_PASSWORD' if home == a else 'BETA_PASSWORD'
            other = 'BETA_PASSWORD' if home == a else 'ALPHA_PASSWORD'
            assert expected in get_all_passthrough()
            assert other not in get_all_passthrough()
        finally:
            reset_hermes_home_override(token)
    token = set_hermes_home_override(str(a))
    try:
        (a / 'config.yaml').write_text(yaml.safe_dump({
            'secrets': {'bitwarden': {'access_token_env': 'NEW_PRIVATE_VALUE'}},
            'terminal': {'env_passthrough': ['NEW_PASSWORD']},
        }))
        monkeypatch.setenv('NEW_PRIVATE_VALUE', 'fixture')
        assert 'NEW_PRIVATE_VALUE' not in _make_run_env({})
        assert 'NEW_PASSWORD' in get_all_passthrough()
        assert 'ALPHA_PASSWORD' not in get_all_passthrough()
    finally:
        reset_hermes_home_override(token)
        clear_env_passthrough()


def test_spawn_config_snapshot_is_read_once_and_not_mutated(tmp_path, monkeypatch):
    import copy
    from tools.env_passthrough import clear_env_passthrough
    from agent import secret_scope

    snapshot = {
        'secrets': {'bitwarden': {'access_token_env': 'CUSTOM_BOOTSTRAP'}},
        'terminal': {'env_passthrough': ['CUSTOM_BOOTSTRAP', 'SAFE_PERMITTED']},
    }
    before = copy.deepcopy(snapshot)
    reads = []

    path = tmp_path / 'config.yaml'
    import yaml
    path.write_text(yaml.safe_dump(snapshot))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    original_capture = config.capture_security_policy_config

    def capture_snapshot():
        reads.append(True)
        return original_capture()

    monkeypatch.setattr(config, 'capture_security_policy_config', capture_snapshot)
    monkeypatch.setenv('CUSTOM_BOOTSTRAP', 'synthetic-private')
    monkeypatch.setenv('SAFE_PERMITTED', 'synthetic-allowed')
    clear_env_passthrough()
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        child = _make_run_env({})
    finally:
        reset_hermes_home_override(token)
    assert reads == [True]
    assert yaml.safe_load(path.read_text()) == before
    assert 'CUSTOM_BOOTSTRAP' not in child
    assert child['SAFE_PERMITTED'] == 'synthetic-allowed'


def test_spawn_config_read_failure_refuses_to_run(monkeypatch):
    import pytest
    from agent import secret_scope

    def fail():
        raise OSError('fixture config failure')

    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setattr(config, 'capture_security_policy_config', fail)
    with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
        _make_run_env({})


def test_explicit_profile_cannot_be_combined_with_snapshot(tmp_path):
    import pytest
    from tools.env_passthrough import get_all_passthrough

    with pytest.raises(ValueError, match='cannot be combined'):
        get_all_passthrough(profile_home=tmp_path, _config_snapshot={})
