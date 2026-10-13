"""Behavior contracts for opt-in native-bearing managed releases."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares_runtime.local_runtime import (
    AresLocalPaths,
    AresLocalRuntime,
    AresLocalRuntimeError,
    _parser,
)


def runtime(tmp_path):
    return AresLocalRuntime(AresLocalPaths(
        state_root=tmp_path / "state",
        data_root=tmp_path / "runtime",
        agent_home=tmp_path / "home",
        launcher_path=tmp_path / "bin" / "ares",
        unit_path=tmp_path / "units" / "fixture-gateway.service",
    ))


def base_release(owner, revision="a" * 40):
    source = owner.paths.releases_dir / revision / "source"
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("inert completeness fixture", encoding="utf-8")
    record = {
        "revision": revision,
        "runtime_binding": {
            "schema": "AresLocalRuntimeBindingV1",
            "source": str(source.resolve()),
            "controller_contract": 1,
        },
    }
    descriptor = source.parent / "release.json"
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    return source, record, descriptor


def test_setup_accepts_explicit_governor_source(tmp_path):
    args = _parser().parse_args(["setup", "--source", str(tmp_path),
                                "--governor-source", str(tmp_path / "native")])
    assert args.governor_source == tmp_path / "native"


@pytest.mark.parametrize("native", [None, {}, {"schema": "untrusted-native"}])
def test_base_binding_may_not_admit_claimed_native_recipe(tmp_path, native):
    owner = runtime(tmp_path)
    source, record, descriptor = base_release(owner)
    record["governor"] = native
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(AresLocalRuntimeError):
        owner._require_complete_release(source, desktop=False)


def test_base_release_without_native_keeps_existing_completeness_contract(tmp_path):
    owner = runtime(tmp_path)
    source, _, _ = base_release(owner)
    owner._require_complete_release(source, desktop=False)


def test_native_binding_without_native_recipe_is_refused(tmp_path):
    owner = runtime(tmp_path)
    source, record, descriptor = base_release(owner)
    record["runtime_binding"]["controller_contract"] = 2
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(AresLocalRuntimeError):
        owner._require_complete_release(source, desktop=False)
