"""Bound config reads while retaining child-environment security decisions."""
from hermes_cli import config
from tools.environments.local import _make_run_env, _sanitize_subprocess_env


def test_subprocess_sanitizer_config_reads_do_not_scale(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    (tmp_path / 'config.yaml').write_text('terminal:\n  env_passthrough: []\n')
    token = set_hermes_home_override(str(tmp_path))
    try:
        reads = 0
        original = config.read_raw_config

        def counted():
            nonlocal reads
            reads += 1
            return original()

        monkeypatch.setattr(config, 'read_raw_config', counted)
        child = _sanitize_subprocess_env({f'BENCH_NONSECRET_{i}': 'x' for i in range(300)})
        assert child['BENCH_NONSECRET_1'] == 'x'
        assert reads <= 2, f'config reads scaled with environment size: {reads}'
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


def test_environment_config_reads_do_not_scale_with_variable_count(monkeypatch):
    reads = 0
    original = config.read_raw_config

    def counted():
        nonlocal reads
        reads += 1
        return original()

    monkeypatch.setattr(config, 'read_raw_config', counted)
    env = {f'RELIABILITY_NONSECRET_{i}': 'test' for i in range(300)}
    child = _make_run_env(env)
    assert child['RELIABILITY_NONSECRET_1'] == 'test'
    assert reads <= 2, f'config reads scaled with environment size: {reads}'


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
