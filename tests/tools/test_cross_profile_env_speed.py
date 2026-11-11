"""Target profile policy must protect its own vault bootstrap name."""

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.environments.local import _make_run_env, _sanitize_subprocess_env



def test_foreground_cold_target_hydrates_from_its_bound_policy(tmp_path, monkeypatch):
    """Exercise owner binding through environment, scope and hydration."""
    from agent import secret_scope
    from agent.secret_sources import registry
    from hermes_cli import env_loader

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / 'config.yaml').write_text('{}\n')
    (target / 'config.yaml').write_text(
        'secrets:\n  synthetic_source:\n    enabled: true\n'
    )
    (source / '.env').write_text('')
    (target / '.env').write_text('')
    monkeypatch.setenv('HERMES_HOME', str(source))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    env_loader.reset_secret_source_cache()
    calls = []
    original_load = env_loader._load_secrets_config

    def refuse_target_reread(home):
        if home == target:
            pytest.fail('target hydration read an independent policy')
        return original_load(home)

    def apply(cfg, home, environ=None):
        calls.append((cfg, home))
        return registry.ApplyReport(sources=[], provenance={})

    monkeypatch.setattr(env_loader, '_load_secrets_config', refuse_target_reread)
    monkeypatch.setattr(registry, 'apply_all', apply)
    token = set_hermes_home_override(target)
    try:
        child = _make_run_env({'SAFE_SYNTHETIC_CONTROL': 'safe'})
    finally:
        reset_hermes_home_override(token)
        env_loader.reset_secret_source_cache()
    assert child['SAFE_SYNTHETIC_CONTROL'] == 'safe'
    assert calls == [({'synthetic_source': {'enabled': True}}, target)]


@pytest.mark.parametrize('injection', ['ambient', 'forced'])
def test_foreground_multiplex_blocks_source_bootstrap_name(tmp_path, monkeypatch, injection):
    """A source token named only by source config must not cross to target."""
    from agent import secret_scope

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / 'config.yaml').write_text(
        'secrets:\n  bitwarden:\n    access_token_env: SOURCE_BOOTSTRAP_CANARY\n'
    )
    (target / 'config.yaml').write_text('{}\n')
    (source / '.env').write_text('')
    (target / '.env').write_text('')
    monkeypatch.setenv('HERMES_HOME', str(source))
    monkeypatch.setenv('SAFE_SYNTHETIC_CONTROL', 'safe')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    if injection == 'ambient':
        monkeypatch.setenv('SOURCE_BOOTSTRAP_CANARY', 'synthetic-secret')
        extra = {}
    else:
        extra = {'_HERMES_FORCE_SOURCE_BOOTSTRAP_CANARY': 'synthetic-secret'}
    token = set_hermes_home_override(target)
    try:
        child = _make_run_env(extra)
    finally:
        reset_hermes_home_override(token)
    assert 'SOURCE_BOOTSTRAP_CANARY' not in child
    assert child['SAFE_SYNTHETIC_CONTROL'] == 'safe'


@pytest.mark.parametrize('change', ['remap', 'malformed', 'deleted'])
def test_foreground_multiplex_refuses_source_policy_change_during_filter(tmp_path, monkeypatch, change):
    from agent import secret_scope
    from tools.environments import local

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    source_policy = source / 'config.yaml'
    source_policy.write_text('secrets:\n  bitwarden:\n    access_token_env: OLD_SOURCE_BOOT\n')
    (target / 'config.yaml').write_text('{}\n')
    (source / '.env').write_text('')
    (target / '.env').write_text('')
    monkeypatch.setenv('HERMES_HOME', str(source))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    original = local._finalize_child_env_policy
    changes = []

    def mutate_after_filter(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'deleted':
            source_policy.unlink()
        elif change == 'malformed':
            source_policy.write_text('secrets: [broken\n')
        else:
            source_policy.write_text('secrets:\n  bitwarden:\n    access_token_env: NEW_SOURCE_BOOT\n')
        changes.append(change)
        return result

    monkeypatch.setattr(local, '_finalize_child_env_policy', mutate_after_filter)
    token = set_hermes_home_override(target)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _make_run_env({'_HERMES_FORCE_NEW_SOURCE_BOOT': 'synthetic-secret'})
    finally:
        reset_hermes_home_override(token)
    assert changes == [change]


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
@pytest.mark.parametrize('change', ['remap', 'malformed', 'deleted'])
def test_ambient_policy_change_during_filter_refuses_spawn(tmp_path, monkeypatch, builder, change):
    """The first snapshot must still belong to the policy at the return fence."""
    from agent import secret_scope
    from tools.environments import local

    path = tmp_path / 'config.yaml'
    path.write_text('secrets:\n  bitwarden:\n    access_token_env: OLD_BOOT\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    original = local._finalize_child_env_policy
    changed = []

    def after_filter(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'remap':
            path.write_text('secrets:\n  bitwarden:\n    access_token_env: NEW_BOOT\n')
        elif change == 'malformed':
            path.write_text('secrets: [broken\n')
        else:
            path.unlink()
        changed.append(True)
        return result

    monkeypatch.setattr(local, '_finalize_child_env_policy', after_filter)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            builder({'NEW_BOOT': 'synthetic-canary', 'SAFE_VALUE': 'safe'})
        assert changed == [True]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('owner', ['source', 'target'])
@pytest.mark.parametrize('change', ['remap', 'malformed', 'deleted'])
def test_explicit_policy_change_during_filter_refuses_spawn(tmp_path, monkeypatch, owner, change):
    """Nonambient source and target policy identities must survive filtering."""
    from agent import secret_scope
    from tools.environments import local

    ambient, source, target = (tmp_path / name for name in ('ambient', 'source', 'target'))
    for home in (ambient, source, target):
        home.mkdir()
        (home / 'config.yaml').write_text(
            f'secrets:\n  bitwarden:\n    access_token_env: {home.name.upper()}_OLD_BOOT\n'
        )
    path = (source if owner == 'source' else target) / 'config.yaml'
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    original = local._finalize_child_env_policy
    changed = []

    def after_filter(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'remap':
            path.write_text('secrets:\n  bitwarden:\n    access_token_env: NEW_BOOT\n')
        elif change == 'malformed':
            path.write_text('secrets: [broken\n')
        else:
            path.unlink()
        changed.append(True)
        return result

    monkeypatch.setattr(local, '_finalize_child_env_policy', after_filter)
    token = set_hermes_home_override(ambient)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _sanitize_subprocess_env({'NEW_BOOT': 'synthetic-canary', 'SAFE_VALUE': 'safe'},
                                     source_profile_home=source, profile_home=target)
        assert changed == [True]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize(('source_kind', 'target_kind', 'expected'), [
    ('ambient', 'ambient', ['ambient']),
    ('source', 'source', ['ambient', 'source']),
    ('source', 'target', ['ambient', 'target', 'source']),
    (None, 'target', ['ambient', 'target']),
    ('source', None, ['ambient', 'source']),
])
def test_explicit_policy_snapshots_are_deduplicated_and_all_rechecked(
        tmp_path, monkeypatch, source_kind, target_kind, expected):
    from agent import secret_scope
    from hermes_cli import config

    homes = {name: tmp_path / name for name in ('ambient', 'source', 'target')}
    for name, home in homes.items():
        home.mkdir()
        (home / 'config.yaml').write_text(
            f'secrets:\n  bitwarden:\n    access_token_env: {name.upper()}_BOOTSTRAP\n'
        )
    monkeypatch.setenv('HERMES_HOME', str(homes['ambient']))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    captures, rechecks = [], []
    capture = config.capture_security_policy_config
    recheck = config.SecurityPolicySnapshot.assert_current

    def counted_capture(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        captures.append(snapshot._path.parent.name)
        return snapshot

    def counted_recheck(snapshot):
        rechecks.append(snapshot._path.parent.name)
        return recheck(snapshot)

    monkeypatch.setattr(config, 'capture_security_policy_config', counted_capture)
    monkeypatch.setattr(config.SecurityPolicySnapshot, 'assert_current', counted_recheck)
    token = set_hermes_home_override(homes['ambient'])
    try:
        child = _sanitize_subprocess_env(
            {'SAFE_VALUE': 'safe'},
            source_profile_home=homes[source_kind] if source_kind else None,
            profile_home=homes[target_kind] if target_kind else None,
        )
    finally:
        reset_hermes_home_override(token)
    assert child['SAFE_VALUE'] == 'safe'
    assert captures == expected
    assert rechecks == expected


@pytest.mark.parametrize('disable_owner_check', [False, True])
def test_ambient_owner_switch_during_capture_refuses_cross_profile_spawn(
        tmp_path, monkeypatch, disable_owner_check):
    """A snapshot from B must never be indexed as the earlier A owner."""
    from agent import secret_scope
    from hermes_cli import config

    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'config.yaml').write_text('secrets:\n  bitwarden:\n    access_token_env: ALPHA_PRIVATE\n')
    (b / 'config.yaml').write_text('secrets:\n  bitwarden:\n    access_token_env: BETA_PRIVATE\n')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setenv('HERMES_HOME', str(a))
    original_capture = config.capture_security_policy_config
    switched = []

    def switch_before_capture(*args, **kwargs):
        if not switched and not args and not kwargs:
            switched.append(set_hermes_home_override(b))
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(config, 'capture_security_policy_config', switch_before_capture)
    if disable_owner_check:
        # Mutation witness: prove why ignoring this identity mismatch matters.
        monkeypatch.setattr(config.SecurityPolicySnapshot, 'assert_profile_owner', lambda *args: None)
    token = set_hermes_home_override(a)
    try:
        if disable_owner_check:
            child = _sanitize_subprocess_env(
                {}, extra_env={'_HERMES_FORCE_ALPHA_PRIVATE': 'synthetic-canary'},
                profile_home=a, source_profile_home=a,
            )
            assert child['ALPHA_PRIVATE'] == 'synthetic-canary'
        else:
            with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
                _sanitize_subprocess_env({}, extra_env={'_HERMES_FORCE_ALPHA_PRIVATE': 'synthetic-canary'},
                                         profile_home=a, source_profile_home=a)
        assert len(switched) == 1
    finally:
        if switched:
            reset_hermes_home_override(switched[0])
        reset_hermes_home_override(token)


def test_explicit_owner_snapshot_mismatch_refuses_cross_profile_spawn(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_cli import config

    ambient, target = tmp_path / 'ambient', tmp_path / 'target'
    ambient.mkdir()
    target.mkdir()
    (ambient / 'config.yaml').write_text('{}\n')
    (target / 'config.yaml').write_text('{}\n')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    capture = config.capture_security_policy_config

    def wrong_owner(*args, **kwargs):
        return capture(ambient) if args and args[0] == target else capture(*args, **kwargs)

    monkeypatch.setattr(config, 'capture_security_policy_config', wrong_owner)
    token = set_hermes_home_override(ambient)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _sanitize_subprocess_env({'SAFE_VALUE': 'safe'}, profile_home=target)
    finally:
        reset_hermes_home_override(token)


def test_explicit_target_bws_name_cannot_be_allowlisted(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / '.env').write_text('SOURCE_BOOT=source-secret\n')
    (target / '.env').write_text('TARGET_BOOT=target-secret\nSAFE_VALUE=ok\n')
    (source / 'config.yaml').write_text(yaml.safe_dump({
        'secrets': {'bitwarden': {'access_token_env': 'SOURCE_BOOT'}},
    }))
    (target / 'config.yaml').write_text(yaml.safe_dump({
        'secrets': {'bitwarden': {'access_token_env': 'TARGET_BOOT'}},
        'terminal': {'env_passthrough': ['TARGET_BOOT', 'SAFE_VALUE']},
    }))
    monkeypatch.setenv('HERMES_HOME', str(source))
    token = set_hermes_home_override(source)
    try:
        child = _sanitize_subprocess_env(
            {}, extra_env={'_HERMES_FORCE_SOURCE_BOOT': 'source-secret',
                           '_HERMES_FORCE_TARGET_BOOT': 'target-secret'},
            profile_home=target, source_profile_home=source,
            enforce_profile_boundary=True,
        )
    finally:
        reset_hermes_home_override(token)
    assert child['SAFE_VALUE'] == 'ok'
    assert 'TARGET_BOOT' not in child
    assert 'SOURCE_BOOT' not in child


@pytest.mark.parametrize('injection', ['base', 'force'])
def test_explicit_source_policy_protects_bootstrap_when_ambient_home_differs(tmp_path, monkeypatch, injection):
    from agent import secret_scope
    from tools.environments.local import build_subprocess_env
    import json
    import subprocess
    import sys

    ambient = tmp_path / 'ambient'
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    for home in (ambient, source, target):
        home.mkdir()
    for home, name in ((ambient, 'AMBIENT_BOOTSTRAP_CUSTOM'),
                       (source, 'SOURCE_BOOTSTRAP_CUSTOM'),
                       (target, 'TARGET_BOOTSTRAP_CUSTOM')):
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'secrets': {'bitwarden': {'access_token_env': name}},
        }))
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(ambient)
    canaries = {name: 'synthetic-canary' for name in (
        'AMBIENT_BOOTSTRAP_CUSTOM', 'SOURCE_BOOTSTRAP_CUSTOM', 'TARGET_BOOTSTRAP_CUSTOM',
    )}
    base = {'SAFE_CONTROL_VALUE': 'safe'}
    extra = {}
    if injection == 'base':
        base.update(canaries)
    else:
        extra = {'_HERMES_FORCE_' + key: value for key, value in canaries.items()}
    try:
        child = build_subprocess_env(
            base, extra=extra, source_profile_home=source, profile_home=target,
            enforce_profile_boundary=True,
        )
        probe = subprocess.run(
            [sys.executable, '-c',
             'import json, os; print(json.dumps({k: k in os.environ for k in '
             "('AMBIENT_BOOTSTRAP_CUSTOM', 'SOURCE_BOOTSTRAP_CUSTOM', "
             "'TARGET_BOOTSTRAP_CUSTOM', 'SAFE_CONTROL_VALUE')}))"],
            env=child, text=True, capture_output=True, check=True, timeout=10,
        )
    finally:
        reset_hermes_home_override(token)
    assert child['SAFE_CONTROL_VALUE'] == 'safe'
    assert not (canaries.keys() & child.keys())
    assert json.loads(probe.stdout) == {
        'AMBIENT_BOOTSTRAP_CUSTOM': False,
        'SOURCE_BOOTSTRAP_CUSTOM': False,
        'TARGET_BOOTSTRAP_CUSTOM': False,
        'SAFE_CONTROL_VALUE': True,
    }


@pytest.mark.parametrize('count', [1, 30, 300])
def test_explicit_source_matching_ambient_has_bounded_policy_reads(tmp_path, monkeypatch, count):
    from hermes_cli import config

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / 'config.yaml').write_text(
        'secrets:\n  bitwarden:\n    access_token_env: SOURCE_BOOTSTRAP_CUSTOM\n'
    )
    (target / 'config.yaml').write_text(
        'secrets:\n  bitwarden:\n    access_token_env: TARGET_BOOTSTRAP_CUSTOM\n'
    )
    monkeypatch.setenv('HERMES_HOME', str(source))
    original = config.read_security_policy_config
    original_capture = config.capture_security_policy_config
    reads = []

    def counted(*args, **kwargs):
        reads.append(('strict', args[0] if args else None))
        return original(*args, **kwargs)

    def counted_capture(*args, **kwargs):
        reads.append(('capture', args[0] if args else None))
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(config, 'read_security_policy_config', counted)
    monkeypatch.setattr(config, 'capture_security_policy_config', counted_capture)
    token = set_hermes_home_override(source)
    base = {f'SAFE_ENTRY_{i}': 'safe' for i in range(count)}
    base['SOURCE_BOOTSTRAP_CUSTOM'] = 'synthetic-canary'
    base['TARGET_BOOTSTRAP_CUSTOM'] = 'synthetic-canary'
    try:
        child = _sanitize_subprocess_env(base, source_profile_home=source,
                                         profile_home=target, enforce_profile_boundary=True)
    finally:
        reset_hermes_home_override(token)
    assert child['SAFE_ENTRY_0'] == 'safe'
    assert 'SOURCE_BOOTSTRAP_CUSTOM' not in child
    assert 'TARGET_BOOTSTRAP_CUSTOM' not in child
    # Count both strict parsers; owner-bound identity checks still read bytes,
    # and other config readers are outside this narrower parse counter.
    assert len(reads) == 2, f'expected two parsed security policies: {reads}'


@pytest.mark.parametrize('changed_to_invalid', [False, True])
@pytest.mark.parametrize('simulate_naive_dedup', [False, True])
def test_source_policy_race_and_naive_dedup_mutation(
        tmp_path, monkeypatch, changed_to_invalid, simulate_naive_dedup):
    from hermes_cli import config

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    source_config = source / 'config.yaml'
    source_config.write_text('secrets:\n  bitwarden:\n    access_token_env: OLD_SOURCE_BOOT\n')
    (target / 'config.yaml').write_text(
        'secrets:\n  bitwarden:\n    access_token_env: TARGET_BOOT\n'
    )
    monkeypatch.setenv('HERMES_HOME', str(source))
    original = config.capture_security_policy_config
    changed = []

    def change_source_after_target(*args, **kwargs):
        result = original(*args, **kwargs)
        if args and args[0] == target and not changed:
            source_config.write_text(
                'secrets: [broken\n' if changed_to_invalid else
                'secrets:\n  bitwarden:\n    access_token_env: NEW_SOURCE_BOOT\n'
            )
            changed.append(True)
        return result

    monkeypatch.setattr(config, 'capture_security_policy_config', change_source_after_target)
    if simulate_naive_dedup:
        # Deliberately disable the new change check to falsify the optimization.
        monkeypatch.setattr(config.SecurityPolicySnapshot, 'assert_current', lambda self: None)
    token = set_hermes_home_override(source)
    extra = {'_HERMES_FORCE_' + name: 'synthetic-canary' for name in (
        'OLD_SOURCE_BOOT', 'NEW_SOURCE_BOOT', 'TARGET_BOOT',
    )}
    try:
        if not simulate_naive_dedup:
            with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
                _sanitize_subprocess_env({'SAFE_CONTROL_VALUE': 'safe'}, extra_env=extra,
                    source_profile_home=source, profile_home=target, enforce_profile_boundary=True)
        else:
            child = _sanitize_subprocess_env({'SAFE_CONTROL_VALUE': 'safe'}, extra_env=extra,
                source_profile_home=source, profile_home=target, enforce_profile_boundary=True)
            assert child['SAFE_CONTROL_VALUE'] == 'safe'
            assert 'OLD_SOURCE_BOOT' not in child
            assert 'TARGET_BOOT' not in child
            # Mutation witness: ignoring the identity change forwards the new
            # synthetic name, even after a now-malformed source policy.
            assert child['NEW_SOURCE_BOOT'] == 'synthetic-canary'
        assert changed == [True]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('replacement', [
    'secrets:\n  bitwarden:\n    access_token_env: NEW_SOURCE_BOOT\n',
    'secrets: [broken\n',
    None,
])
def test_same_profile_snapshot_refuses_change_after_filter(tmp_path, monkeypatch, replacement):
    from tools.environments import local

    path = tmp_path / 'config.yaml'
    path.write_text('secrets:\n  bitwarden:\n    access_token_env: OLD_SOURCE_BOOT\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    original = local._finalize_child_env_policy
    changed = []

    def mutate_after_filter(*args, **kwargs):
        result = original(*args, **kwargs)
        if replacement is None:
            path.unlink()
        else:
            path.write_text(replacement)
        changed.append(True)
        return result

    monkeypatch.setattr(local, '_finalize_child_env_policy', mutate_after_filter)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _sanitize_subprocess_env({'SAFE_CONTROL_VALUE': 'safe'},
                source_profile_home=tmp_path, profile_home=tmp_path,
                enforce_profile_boundary=True)
        assert changed == [True]
    finally:
        reset_hermes_home_override(token)


def test_explicit_source_policy_read_failure_blocks_spawn(tmp_path, monkeypatch):
    ambient, source, target = (tmp_path / name for name in ('ambient', 'source', 'target'))
    for home in (ambient, source, target):
        home.mkdir()
    (ambient / 'config.yaml').write_text('{}\n')
    (source / 'config.yaml').write_text('secrets: [broken\n')
    (target / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    token = set_hermes_home_override(ambient)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _sanitize_subprocess_env({}, profile_home=target, source_profile_home=source,
                                     enforce_profile_boundary=True)
    finally:
        reset_hermes_home_override(token)


def test_target_policy_read_failure_blocks_spawn(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (source / 'config.yaml').write_text('{}\n')
    (target / 'config.yaml').write_text('secrets: [broken\n')
    monkeypatch.setenv('HERMES_HOME', str(source))
    token = set_hermes_home_override(source)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            _sanitize_subprocess_env({}, profile_home=target)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
@pytest.mark.parametrize('invalid', ['secrets: [broken\n', '- not-a-mapping\n', 'secrets: {}\nsecrets: {}\n'])
def test_invalid_active_policy_refuses_child_after_valid_remap(tmp_path, monkeypatch, builder, invalid):
    from agent import secret_scope

    path = tmp_path / 'config.yaml'
    path.write_text('secrets:\n  bitwarden:\n    access_token_env: SYNTHETIC_BOOTSTRAP\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('SYNTHETIC_BOOTSTRAP', 'synthetic-canary')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        assert 'SYNTHETIC_BOOTSTRAP' not in builder({})
        path.write_text(invalid)
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            builder({})
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_unreadable_active_policy_refuses_child(tmp_path, monkeypatch, builder):
    from pathlib import Path
    from agent import secret_scope

    path = tmp_path / 'config.yaml'
    path.write_text('secrets: {}\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    original = Path.read_bytes

    def denied(self):
        if self == path:
            raise PermissionError('synthetic denial')
        return original(self)

    monkeypatch.setattr(Path, 'read_bytes', denied)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            builder({})
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_dangling_policy_link_refuses_child(tmp_path, monkeypatch, builder):
    from agent import secret_scope

    try:
        (tmp_path / 'config.yaml').symlink_to(tmp_path / 'missing-target')
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this host')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            builder({})
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_missing_active_policy_keeps_default_bootstrap_protected(tmp_path, monkeypatch, builder):
    from agent import secret_scope

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('BWS_ACCESS_TOKEN', 'synthetic-canary')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        assert 'BWS_ACCESS_TOKEN' not in builder({})
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
@pytest.mark.parametrize('configured', [
    123456789,
    ' SYNTHETIC_BOOTSTRAP ',
    'APPTAINERENV_SYNTHETIC_BOOTSTRAP',
    'SINGULARITYENV_SYNTHETIC_BOOTSTRAP',
    'APPTAINERENV_SINGULARITYENV_SYNTHETIC_BOOTSTRAP',
])
def test_bootstrap_name_matches_bitwarden_source_exactly(tmp_path, monkeypatch, builder, configured):
    """The source uses str(value or default), without trimming the env key."""
    from agent import secret_scope
    from agent.secret_sources.bitwarden import BitwardenSource

    name = str(configured)
    assert BitwardenSource().protected_env_vars({'access_token_env': configured}) == frozenset({name})
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'secrets': {'bitwarden': {'access_token_env': configured}},
    }))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(name, 'synthetic-canary')
    monkeypatch.setenv('SAFE_CONTROL_VALUE', 'not-secret')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    try:
        names = [name, 'SINGULARITYENV_' + name,
                 'APPTAINERENV_SINGULARITYENV_' + name]
        supplied = {key: 'synthetic-canary' for key in names}
        supplied['SAFE_CONTROL_VALUE'] = 'not-secret'
        child = builder(supplied)
        assert child['SAFE_CONTROL_VALUE'] == 'not-secret'
        assert all(key not in child for key in names)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_expanding_source_protection_refuses_spawn(tmp_path, monkeypatch, builder):
    from agent import secret_scope
    from agent.secret_sources.bitwarden import BitwardenSource

    (tmp_path / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setattr(BitwardenSource, 'protected_env_vars', lambda self, cfg: {'FIRST', 'SECOND'})
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Bitwarden token policy unavailable'):
            builder({})
    finally:
        reset_hermes_home_override(token)


def test_final_policy_checks_all_forwarding_layers():
    from tools.environments.local import _finalize_child_env_policy

    name = 'APPTAINERENV_SYNTHETIC_BOOTSTRAP'
    keys = [name, 'SINGULARITYENV_' + name]
    supplied = {key: 'synthetic-canary' for key in keys}
    supplied['SAFE_CONTROL_VALUE'] = 'not-secret'
    result = _finalize_child_env_policy(supplied, lambda _: True, bws_token_env=name)
    assert result == {'SAFE_CONTROL_VALUE': 'not-secret'}


@pytest.mark.parametrize('builder', [_make_run_env, _sanitize_subprocess_env])
def test_force_prefix_cannot_reintroduce_container_named_bootstrap(tmp_path, monkeypatch, builder):
    from agent import secret_scope

    name = 'APPTAINERENV_SYNTHETIC_BOOTSTRAP'
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'secrets': {'bitwarden': {'access_token_env': name}},
    }))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    token = set_hermes_home_override(tmp_path)
    force = {'_HERMES_FORCE_' + name: 'synthetic-canary'}
    try:
        child = (builder({}, extra_env=force) if builder is _sanitize_subprocess_env
                 else builder(force))
        assert name not in child
        assert '_HERMES_FORCE_' + name not in child
    finally:
        reset_hermes_home_override(token)
