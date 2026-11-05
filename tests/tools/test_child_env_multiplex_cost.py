"""Scratch witness: config-read bound during actual cross-profile materialization."""
import pytest
import yaml
from agent.secret_scope import set_multiplex_active
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import config
from tools.environments.local import _make_run_env


@pytest.mark.parametrize('count', [1, 30, 300, 600])
def test_cross_profile_materialization_has_bounded_config_reads(tmp_path, monkeypatch, count):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir(); target.mkdir()
    names = [f'BENCH_TARGET_{i}' for i in range(count)]
    (source / '.env').write_text('')
    (target / '.env').write_text(''.join(f'{name}=target-value\n' for name in names) + 'LOCAL_BOOTSTRAP=private\n')
    (target / 'config.yaml').write_text(yaml.safe_dump({
        'terminal': {'env_passthrough': names + ['LOCAL_BOOTSTRAP']},
        'secrets': {'bitwarden': {'access_token_env': 'LOCAL_BOOTSTRAP'}},
    }))
    monkeypatch.setenv('HERMES_HOME', str(source))
    reads = 0
    from collections import Counter
    import inspect
    sites = Counter()
    original = config.read_raw_config
    original_security = config.read_security_policy_config
    original_capture = config.capture_security_policy_config
    from tools.environments import local
    original_blocklist = local._build_provider_env_blocklist
    blocklist_builds = []

    def counted_blocklist():
        blocklist_builds.append(None)
        return original_blocklist()

    monkeypatch.setattr(local, '_build_provider_env_blocklist', counted_blocklist)
    def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        frame = inspect.currentframe().f_back
        sites[f'{frame.f_code.co_filename}:{frame.f_code.co_name}:{frame.f_lineno}'] += 1
        return original(*args, **kwargs)
    def counted_security(*args, **kwargs):
        nonlocal reads
        reads += 1
        sites['strict-security-policy'] += 1
        return original_security(*args, **kwargs)
    def counted_capture(*args, **kwargs):
        nonlocal reads
        reads += 1
        sites['captured-security-policy'] += 1
        return original_capture(*args, **kwargs)
    monkeypatch.setattr(config, 'read_raw_config', counted)
    monkeypatch.setattr(config, 'read_security_policy_config', counted_security)
    monkeypatch.setattr(config, 'capture_security_policy_config', counted_capture)
    set_multiplex_active(True)
    token = set_hermes_home_override(target)
    try:
        child = _make_run_env({})
    finally:
        reset_hermes_home_override(token)
        set_multiplex_active(False)
    print(f'profile names={count}, policy/config reads={reads}, sites={dict(sites)}')
    assert all(child.get(name) == 'target-value' for name in names)
    assert 'LOCAL_BOOTSTRAP' not in child
    assert reads <= 2, f'config read count during cross-profile spawn: {reads}; sites={sites}'
    assert 1 <= len(blocklist_builds) <= 2, f'provider policy built {len(blocklist_builds)} times'
