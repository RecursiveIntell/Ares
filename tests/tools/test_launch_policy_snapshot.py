"""Real launch-policy regressions; all inputs belong to temporary homes."""

import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent import secret_scope as ss
from tools.environments import local
from tools import env_passthrough as ep
from hermes_cli import config as cfg
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", False)
    home_token = set_hermes_home_override(None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    token = ss.set_secret_scope(None)
    ep.clear_env_passthrough()
    yield tmp_path
    ep.clear_env_passthrough()
    ss.reset_secret_scope(token)
    reset_hermes_home_override(home_token)


def test_corrupt_config_denies_fresh_launch(isolated_policy):
    config = isolated_policy / "config.yaml"
    config.write_text(
        "secrets:\n  bitwarden:\n    access_token_env: UNMARKED_BOOTSTRAP\n"
    )
    assert "UNMARKED_BOOTSTRAP" not in local._make_run_env({
        "UNMARKED_BOOTSTRAP": "neutral-sentinel"
    })
    config.write_text("secrets: [unterminated\n")
    with pytest.raises(RuntimeError):
        local._make_run_env({"UNMARKED_BOOTSTRAP": "neutral-sentinel"})


@pytest.mark.parametrize(
    "document,status",
    [
        ("[one, two]", cfg.ConfigPolicyStatus.NONMAPPING),
        ("null", cfg.ConfigPolicyStatus.NONMAPPING),
        ("", cfg.ConfigPolicyStatus.NONMAPPING),
        ("terminal: [unterminated", cfg.ConfigPolicyStatus.MALFORMED),
        ("terminal: nope", cfg.ConfigPolicyStatus.NONMAPPING),
        ("terminal:\n  env_passthrough: nope", cfg.ConfigPolicyStatus.MALFORMED),
    ],
)
def test_typed_policy_failure_is_not_empty(isolated_policy, document, status):
    (isolated_policy / "config.yaml").write_text(document)
    with pytest.raises(cfg.ConfigPolicyError) as error:
        cfg.read_launch_config_policy()
    assert error.value.status is status
    assert document not in str(error.value) or not document


def test_optional_absence_and_known_removal(isolated_policy):
    assert cfg.read_launch_config_policy().status is cfg.ConfigPolicyStatus.ABSENT
    path = isolated_policy / "config.yaml"
    path.write_text("{}")
    policy = cfg.read_launch_config_policy()
    assert policy.status is cfg.ConfigPolicyStatus.VALID
    assert policy.bws_token_env == "BWS_ACCESS_TOKEN"
    path.unlink()
    with pytest.raises(cfg.ConfigPolicyError) as error:
        local._make_run_env({})
    assert error.value.status is cfg.ConfigPolicyStatus.REMOVED


def test_policy_projection_does_not_hold_values_or_mutate_legacy_reader(
    isolated_policy,
):
    path = isolated_policy / "config.yaml"
    path.write_text(
        "secrets:\n  bitwarden:\n    access_token_env: NEUTRAL_BOOTSTRAP\nprivate_value: neutral-sensitive-value\n"
    )
    legacy = cfg.read_raw_config()
    legacy["secrets"]["bitwarden"]["access_token_env"] = "LOCAL_MUTATION"
    policy = cfg.read_launch_config_policy()
    assert policy.bws_token_env == "NEUTRAL_BOOTSTRAP"
    assert "neutral-sensitive-value" not in repr(policy)
    assert (
        cfg.read_raw_config()["secrets"]["bitwarden"]["access_token_env"]
        == policy.bws_token_env
    )


def test_explicit_unavailable_managed_directory_is_not_absence(
    isolated_policy, monkeypatch
):
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(isolated_policy / "missing-managed"))
    with pytest.raises(cfg.ConfigPolicyError) as error:
        local._make_run_env({})
    assert error.value.status is cfg.ConfigPolicyStatus.UNAVAILABLE


def test_dangling_config_symlink_is_not_optional_absence(isolated_policy):
    (isolated_policy / "config.yaml").symlink_to(isolated_policy / "missing-policy")
    with pytest.raises(cfg.ConfigPolicyError) as error:
        local._make_run_env({})
    assert error.value.status is cfg.ConfigPolicyStatus.UNAVAILABLE


def test_io_failure_denies_before_spawn(isolated_policy, monkeypatch):
    path = isolated_policy / "config.yaml"
    path.write_text("{}")
    real_open = open

    def denied_open(file, *args, **kwargs):
        if Path(file) == path:
            raise PermissionError("neutral failure")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied_open)
    spawn = Mock(side_effect=AssertionError("spawn reached"))
    monkeypatch.setattr(local.subprocess, "Popen", spawn)
    environment = local.LocalEnvironment(cwd=str(isolated_policy), env={})
    with pytest.raises(cfg.ConfigPolicyError) as error:
        environment._run_bash("true")
    assert error.value.status is cfg.ConfigPolicyStatus.UNAVAILABLE
    spawn.assert_not_called()


@pytest.mark.parametrize("builder", ["foreground", "background", "shared"])
def test_forbidden_names_remap_force_and_wrappers(
    isolated_policy, monkeypatch, builder
):
    forbidden = {
        "UNMARKED_BOOTSTRAP",
        "BWS_ACCESS_TOKEN",
        "AUXILIARY_TEST_API_KEY",
        "AUXILIARY_TEST_BASE_URL",
        "GATEWAY_RELAY_TEST_SECRET",
        "GATEWAY_RELAY_TEST_KEY",
        "GATEWAY_RELAY_TEST_TOKEN",
        "OP_SESSION_test",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_CONNECT_TOKEN",
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "MY_BWS_TOKEN",
    }
    (isolated_policy / "config.yaml").write_text(
        json.dumps({
            "secrets": {"bitwarden": {"access_token_env": "UNMARKED_BOOTSTRAP"}},
            "terminal": {"env_passthrough": sorted(forbidden)},
        })
    )
    inputs = {name: "neutral-sentinel" for name in forbidden}
    inputs.update({"APPTAINERENV_" + name: "neutral-sentinel" for name in forbidden})
    inputs.update({
        "SINGULARITYENV_" + name.lower(): "neutral-sentinel" for name in forbidden
    })
    inputs[local._HERMES_PROVIDER_ENV_FORCE_PREFIX + "UNMARKED_BOOTSTRAP"] = (
        "neutral-sentinel"
    )
    inputs["GATEWAY_RELAY_URL"] = "neutral-route"
    inputs["AUXILIARY_TEST_MODEL"] = "neutral-model"
    if builder == "foreground":
        result = local._make_run_env(inputs)
    elif builder == "background":
        result = local._sanitize_subprocess_env({}, inputs)
    else:
        for name, value in inputs.items():
            monkeypatch.setenv(name, value)
        result = local.hermes_subprocess_env()
    assert not any(
        local._credential_target_env_name(name).upper()
        in {key.upper() for key in forbidden}
        for name in result
    )
    assert result["GATEWAY_RELAY_URL"] == "neutral-route"
    assert result["AUXILIARY_TEST_MODEL"] == "neutral-model"


def test_launch_snapshots_are_names_only_and_fresh(isolated_policy):
    path = isolated_policy / "config.yaml"
    path.write_text("terminal:\n  env_passthrough: [FIRST]\n")
    first = local._resolve_launch_env_policy()
    ep.register_env_passthrough(["SKILL_NAME", "LATER_BOOTSTRAP"])
    path.write_text(
        "terminal:\n  env_passthrough: [SECOND]\nsecrets:\n  bitwarden:\n    access_token_env: LATER_BOOTSTRAP\n"
    )
    second = local._resolve_launch_env_policy()
    assert first.passthrough_names == frozenset({"FIRST"})
    assert second.passthrough_names == frozenset({"SECOND", "SKILL_NAME"})
    assert not ep.is_env_passthrough("LATER_BOOTSTRAP")
    assert second.internal("LATER_BOOTSTRAP")
    with pytest.raises(FrozenInstanceError):
        second.config.bws_token_env = "changed"
    with pytest.raises(cfg.ConfigPolicyError):
        first.config.validate()
    ep.clear_env_passthrough()
    assert local._resolve_launch_env_policy().passthrough_names == frozenset({"SECOND"})


def test_managed_policy_wins_and_cannot_disappear(isolated_policy, monkeypatch):
    managed = isolated_policy / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (isolated_policy / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    access_token_env: USER_BOOTSTRAP\nterminal:\n  env_passthrough: [USER_NAME]\n"
    )
    managed_file = managed / "config.yaml"
    managed_file.write_text(
        "secrets:\n  bitwarden:\n    access_token_env: ADMIN_BOOTSTRAP\nterminal:\n  env_passthrough: [ADMIN_NAME]\n"
    )
    policy = local._resolve_launch_env_policy()
    assert policy.config.bws_token_env == "ADMIN_BOOTSTRAP"
    assert policy.passthrough_names == frozenset({"ADMIN_NAME"})
    assert "ADMIN_BOOTSTRAP" not in local._make_run_env({"ADMIN_BOOTSTRAP": "neutral"})
    managed_file.write_text("[invalid]")
    with pytest.raises(cfg.ConfigPolicyError):
        local._make_run_env({"ADMIN_BOOTSTRAP": "neutral"})
    managed_file.unlink()
    with pytest.raises(cfg.ConfigPolicyError):
        cfg.read_launch_config_policy()


@pytest.mark.parametrize("candidate_count", [1, 50, 500])
@pytest.mark.parametrize("builder", ["foreground", "background", "shared"])
def test_resolution_counts_are_independent_of_candidates(
    isolated_policy, monkeypatch, candidate_count, builder
):
    (isolated_policy / "config.yaml").write_text("{}")
    monkeypatch.setattr(
        os,
        "environ",
        {"HOME": str(isolated_policy), "HERMES_HOME": str(isolated_policy)},
    )
    neutral = {f"NEUTRAL_{i}": f"value-{i}" for i in range(candidate_count)}
    if builder == "foreground":
        baseline = local._make_run_env({})
    elif builder == "background":
        baseline = local._sanitize_subprocess_env({"HOME": str(isolated_policy)})
    else:
        baseline = local.hermes_subprocess_env()
    read = Mock(wraps=cfg.read_launch_config_policy)
    blocklist = Mock(wraps=local._build_provider_env_blocklist)
    plugins = Mock(wraps=local._plugin_terminal_env_strip_keys)
    copy = Mock(wraps=cfg.copy.deepcopy)
    monkeypatch.setattr(cfg, "read_launch_config_policy", read)
    monkeypatch.setattr(local, "_build_provider_env_blocklist", blocklist)
    monkeypatch.setattr(local, "_plugin_terminal_env_strip_keys", plugins)
    monkeypatch.setattr(cfg.copy, "deepcopy", copy)
    if builder == "foreground":
        result = local._make_run_env(neutral)
    elif builder == "background":
        result = local._sanitize_subprocess_env(
            neutral | {"HOME": str(isolated_policy)}
        )
    else:
        os.environ.update(neutral)
        result = local.hermes_subprocess_env()
    assert result == baseline | neutral
    assert read.call_count == blocklist.call_count == plugins.call_count == 1
    assert copy.call_count == 0


def _profile(home, value, bootstrap):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        json.dumps({
            "secrets": {"bitwarden": {"access_token_env": bootstrap}},
            "terminal": {"env_passthrough": ["SERVICE_TOKEN"]},
        })
    )
    (home / ".env").write_text(
        f"SERVICE_TOKEN={value}\n{bootstrap}=neutral-bootstrap\n"
    )
    return home


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_interleaved_profiles_launch_local_resolution(
    isolated_policy, monkeypatch, workers
):
    root = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    a = _profile(root / "profiles" / "a", "a-neutral", "A_BOOTSTRAP")
    b = _profile(root / "profiles" / "b", "b-neutral", "B_BOOTSTRAP")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("SERVICE_TOKEN", "ambient-must-not-win")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", True)
    barrier = threading.Barrier(workers)
    read = Mock(wraps=cfg.read_launch_config_policy)
    blocklist = Mock(wraps=local._build_provider_env_blocklist)
    plugins = Mock(wraps=local._plugin_terminal_env_strip_keys)
    copy = Mock(wraps=cfg.copy.deepcopy)
    monkeypatch.setattr(cfg, "read_launch_config_policy", read)
    monkeypatch.setattr(local, "_build_provider_env_blocklist", blocklist)
    monkeypatch.setattr(local, "_plugin_terminal_env_strip_keys", plugins)
    monkeypatch.setattr(cfg.copy, "deepcopy", copy)

    def launch(index):
        home = (root, a, b)[index % 3]
        home_token = set_hermes_home_override(home)
        scope = ss.build_profile_secret_scope(home, fail_closed_external=True)
        scope_token = ss.set_secret_scope(scope)
        try:
            barrier.wait(timeout=30)
            neutral = {
                f"NEUTRAL_{i}": f"value-{i}" for i in range((1, 50, 500)[index % 3])
            }
            result = local._make_run_env(neutral)
            assert all(result[name] == value for name, value in neutral.items())
            assert (
                result["SERVICE_TOKEN"]
                == ("root-neutral", "a-neutral", "b-neutral")[index % 3]
            )
            assert result["HERMES_HOME"] == str(home)
            assert not any(
                name in result
                for name in ("ROOT_BOOTSTRAP", "A_BOOTSTRAP", "B_BOOTSTRAP")
            )
            return result["SERVICE_TOKEN"]
        finally:
            ss.reset_secret_scope(scope_token)
            reset_hermes_home_override(home_token)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(launch, range(12)))
        cold_copies = copy.call_count
        warm_results = list(pool.map(launch, range(12)))
    assert results == ["root-neutral", "a-neutral", "b-neutral"] * 4
    assert warm_results == results
    assert (
        read.call_count
        == blocklist.call_count
        == plugins.call_count
        == len(results) + len(warm_results)
    )
    # Cold source hydration/imports retain their existing owner caches. Bound
    # that initialization cost by fixture size and concurrency; warmed launches
    # must add no whole-config copies.
    assert cold_copies <= len({root, a, b}) * workers
    assert copy.call_count == cold_copies


def test_registry_change_is_fresh_and_plugin_keys_override_force(
    isolated_policy, monkeypatch
):
    from agent import terminal_env_registry as registry
    from agent.terminal_env_provider import TerminalEnvironmentProvider

    class Provider(TerminalEnvironmentProvider):
        name = "policy_test_backend"
        strip_env_keys = frozenset({"PLUGIN_STRIP_NAME"})

        def is_available(self):
            return True

        def create_environment(self, **kwargs):
            raise AssertionError("not used")

    (isolated_policy / "config.yaml").write_text("{}")
    assert "PLUGIN_STRIP_NAME" in local._make_run_env({"PLUGIN_STRIP_NAME": "neutral"})
    generation = registry.registry_generation()
    provider = Provider()
    registry.register_provider(provider)
    try:
        assert registry.registry_generation() != generation
        result = local._make_run_env({
            "PLUGIN_STRIP_NAME": "neutral",
            local._HERMES_PROVIDER_ENV_FORCE_PREFIX + "PLUGIN_STRIP_NAME": "neutral",
            "APPTAINERENV_PLUGIN_STRIP_NAME": "neutral",
        })
        assert not any("PLUGIN_STRIP_NAME" in name for name in result)
    finally:
        assert registry.restore_registration(provider.name, provider, None)
    assert "PLUGIN_STRIP_NAME" in local._make_run_env({"PLUGIN_STRIP_NAME": "neutral"})
    monkeypatch.setitem(
        cfg.OPTIONAL_ENV_VARS,
        "NEW_PROVIDER_CREDENTIAL",
        {
            "password": True,
            "category": "tool",
        },
    )
    assert "NEW_PROVIDER_CREDENTIAL" not in local._make_run_env({
        "NEW_PROVIDER_CREDENTIAL": "neutral"
    })


def test_explicit_force_is_preserved_for_noninternal_provider(isolated_policy):
    (isolated_policy / "config.yaml").write_text("{}")
    result = local._make_run_env({
        local._HERMES_PROVIDER_ENV_FORCE_PREFIX + "OPENAI_API_KEY": "neutral"
    })
    assert result["OPENAI_API_KEY"] == "neutral"


def test_in_launch_config_change_fails_closed(isolated_policy, monkeypatch):
    path = isolated_policy / "config.yaml"
    path.write_text("{}")
    original = local._finalize_child_env_policy

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text(
            "secrets:\n  bitwarden:\n    access_token_env: UNMARKED_BOOTSTRAP\n"
        )
        return result

    monkeypatch.setattr(local, "_finalize_child_env_policy", changed)
    with pytest.raises(cfg.ConfigPolicyError) as error:
        local._make_run_env({"UNMARKED_BOOTSTRAP": "neutral"})
    assert error.value.status is cfg.ConfigPolicyStatus.CHANGED


@pytest.mark.parametrize(
    "multiplex,failure",
    [
        (True, "revoked"),
        (True, "generation"),
        (True, "identity"),
        (False, "revoked"),
        (False, "generation"),
        (False, "identity"),
    ],
)
@pytest.mark.parametrize("builder", ["foreground", "background", "shared"])
def test_invalid_or_revoked_scope_denies_launch(
    isolated_policy, monkeypatch, multiplex, failure, builder
):
    root = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    target = _profile(
        root / "profiles" / "target", "target-neutral", "TARGET_BOOTSTRAP"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("SERVICE_TOKEN", "ambient-must-not-win")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", multiplex)
    home_token = set_hermes_home_override(target)
    scope = ss.build_profile_secret_scope(target, fail_closed_external=True)
    if failure == "revoked":
        (target / ".env").write_text("")
    elif failure == "generation":
        scope = replace(scope, generation="invalid-generation")
    elif failure == "identity":
        scope = replace(scope, profile_home=root)
    token = ss.set_secret_scope(scope)
    try:
        with pytest.raises(RuntimeError):
            if builder == "foreground":
                local._make_run_env({})
            elif builder == "background":
                local._sanitize_subprocess_env({"SERVICE_TOKEN": "ambient-neutral"})
            else:
                local.hermes_subprocess_env()
    finally:
        ss.reset_secret_scope(token)
        reset_hermes_home_override(home_token)


@pytest.mark.parametrize("builder", ["foreground", "background"])
def test_unscoped_passthrough_resolution_denies_launch(
    isolated_policy, monkeypatch, builder
):
    root = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    target = _profile(
        root / "profiles" / "target", "target-neutral", "TARGET_BOOTSTRAP"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("SERVICE_TOKEN", "ambient-neutral")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", True)
    token = set_hermes_home_override(target)
    try:
        with pytest.raises(RuntimeError):
            if builder == "foreground":
                local._make_run_env({})
            else:
                local._sanitize_subprocess_env({"SERVICE_TOKEN": "ambient-neutral"})
    finally:
        reset_hermes_home_override(token)


def test_scope_revocation_during_launch_denies_before_return(
    isolated_policy, monkeypatch
):
    home = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", True)
    token = ss.set_secret_scope(
        ss.build_profile_secret_scope(home, fail_closed_external=True)
    )
    original = local._finalize_child_env_policy

    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        (home / ".env").write_text("")
        return result

    monkeypatch.setattr(local, "_finalize_child_env_policy", revoked)
    try:
        with pytest.raises(RuntimeError):
            local._make_run_env({"SERVICE_TOKEN": "ambient-neutral"})
    finally:
        ss.reset_secret_scope(token)


def test_explicit_generated_boundary_invalid_generation_is_denied(
    isolated_policy, monkeypatch
):
    home = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    monkeypatch.setenv("HERMES_HOME", str(home))
    boundary = ss.build_profile_env_boundary(home, home)
    invalid = replace(boundary, target_generation="invalid-generation")
    with pytest.raises(RuntimeError, match="generation changed"):
        local.hermes_subprocess_env(profile_boundary=invalid)


def test_missing_target_value_never_borrows_parent_or_sibling(
    isolated_policy, monkeypatch
):
    root = _profile(isolated_policy / "root", "root-neutral", "ROOT_BOOTSTRAP")
    target = _profile(root / "profiles" / "target", "unused", "TARGET_BOOTSTRAP")
    _profile(root / "profiles" / "sibling", "sibling-neutral", "SIBLING_BOOTSTRAP")
    (target / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("SERVICE_TOKEN", "ambient-neutral")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", True)
    home_token = set_hermes_home_override(target)
    token = ss.set_secret_scope(
        ss.build_profile_secret_scope(target, fail_closed_external=True)
    )
    try:
        assert "SERVICE_TOKEN" not in local._make_run_env({})
    finally:
        ss.reset_secret_scope(token)
        reset_hermes_home_override(home_token)
