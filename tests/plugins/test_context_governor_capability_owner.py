"""The Context Governor capability contract has exactly one owner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import os

import pytest

from ares_runtime import governor_install
from plugins.context_engine import _context_governor as governor

OWNER = "ares_runtime.governor_install"
# If the owner constants are missing or renamed, each parametrised test still runs once and fails,
# instead of collecting zero cases and being skipped.
FLAGS = getattr(governor_install, "REQUIRED_CAPABILITY_FLAGS", None) or ("<REQUIRED_CAPABILITY_FLAGS missing>",)
FIELDS = sorted(getattr(governor_install, "REQUIRED_CAPABILITY_FIELDS", None) or {"<REQUIRED_CAPABILITY_FIELDS missing>": None})


def _qualified() -> dict:
    document = dict(governor_install.REQUIRED_CAPABILITY_FIELDS)
    document.update({flag: True for flag in governor_install.REQUIRED_CAPABILITY_FLAGS})
    document["failure_envelope"] = {
        "flag": "--failure-envelope-v1",
        "schema": "ContextGovernorFailureV1",
        "stream": "stderr",
    }
    return document


def _activate(monkeypatch, tmp_path: Path, document: dict) -> bool:
    """Run the plugin's real activation guard on `document`, with no native binary.

    Everything before and after the capability check is stubbed: the managed-binary check, the key state,
    and the `capabilities` subprocess call (which returns `document`). The guard either raises
    ContextGovernorActivationError (rejected) or reaches the protocol probe (accepted). Returns True when the
    protocol probe was reached; the stub then fails on purpose so nothing after it runs.
    """
    binary = tmp_path / "context-governor"
    binary.write_text("")
    engine = governor.ContextGovernorEngine.__new__(governor.ContextGovernorEngine)
    engine.binary = str(binary)
    engine.timeout_sec = 5
    engine._unsafe_configured_hmac_path = False
    engine._capabilities = None
    monkeypatch.setattr(engine, "_verify_managed_native", lambda: None, raising=False)
    reached = []

    def protocol_probe():
        reached.append(True)
        raise RuntimeError("stop after the capability guard")

    monkeypatch.setattr(engine, "_probe_protocol_contract", protocol_probe, raising=False)
    binding = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(governor, "ContextGovernorKeyState",
                        lambda *a, **k: SimpleNamespace(active_binding=lambda: binding))
    monkeypatch.setattr(governor.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(document), ""))
    try:
        engine.probe_activation()
    except governor.ContextGovernorActivationError:
        if reached:
            return True
        raise
    return bool(reached)


def test_the_contract_is_published_and_includes_pending_activation():
    assert "supports_pending_activation" in governor_install.REQUIRED_CAPABILITY_FLAGS
    assert len(governor_install.REQUIRED_CAPABILITY_FLAGS) >= 6
    assert governor_install.REQUIRED_CAPABILITY_FIELDS["schema"] == "ContextGovernorCapabilitiesV1"


def test_recipe_accepts_a_fully_qualified_capability_document():
    assert governor_install.require_capabilities(_qualified()) == _qualified()


requires_posix = pytest.mark.skipif(
    os.name != "posix",
    reason="the activation guard executes a binary and relies on POSIX process semantics",
)


@pytest.mark.parametrize("flag", FLAGS)
def test_recipe_rejects_a_document_missing_any_required_flag(flag):
    document = _qualified()
    del document[flag]
    with pytest.raises(governor_install.GovernorInstallError):
        governor_install.require_capabilities(document)


@requires_posix
def test_activation_guard_accepts_a_fully_qualified_document(monkeypatch, tmp_path):
    assert _activate(monkeypatch, tmp_path, _qualified()) is True


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("change", ["missing", "truthy-but-not-true"])
@requires_posix
def test_activation_guard_rejects_every_flag_the_recipe_requires(monkeypatch, tmp_path, flag, change):
    document = _qualified()
    if change == "missing":
        del document[flag]
    else:
        document[flag] = 1
    with pytest.raises(governor.ContextGovernorActivationError, match="governor capability mismatch"):
        _activate(monkeypatch, tmp_path, document)


@pytest.mark.parametrize("field", FIELDS)
@requires_posix
def test_activation_guard_rejects_every_field_the_recipe_requires(monkeypatch, tmp_path, field):
    document = _qualified()
    document[field] = "something-else"
    with pytest.raises(governor.ContextGovernorActivationError, match="governor capability mismatch"):
        _activate(monkeypatch, tmp_path, document)
