"""Respect the Rust LineageTipProjectionV1 default-zero wire contract."""
import copy
from pathlib import Path

import pytest
from plugins.context_engine._context_governor import ContextGovernorEngine


def fixture_engine(tmp_path, epoch_field):
    engine = object.__new__(ContextGovernorEngine)
    engine.session_id = "epoch-wire-fixture"
    engine._lineage_session_id = engine.session_id
    engine.store_dir = Path(tmp_path)
    engine._capabilities = {"supports_lineage_tip_projection": True}
    canonical = [{"role": "assistant", "id": "summary_fixture", "name": "context_governor", "content": "summary"},
                 {"role": "user", "content": "request\n"}]
    tip = {"schema": "LineageTipProjectionV1", "verified": True, "session_id": engine.session_id,
           "receipt_id": "ctxr_fixture", "generation": 1, "compacted_messages": canonical, **epoch_field}
    engine._run_certified_json = lambda args, payload: tip
    incoming = [{"role": "assistant", "content": "summary"}, {"role": "user", "content": "request"}]
    return engine, canonical, tip, incoming


@pytest.mark.parametrize("epoch_field", [{}, {"lineage_epoch": 0}, {"lineage_epoch": 1}])
def test_authenticated_epoch_zero_omission_restores_only_known_replay_projection(tmp_path, epoch_field):
    engine, canonical, tip, incoming = fixture_engine(tmp_path, epoch_field)
    original = copy.deepcopy(incoming)
    assert engine._rehydrate_legacy_parent_prefix(incoming) == canonical
    assert incoming == original


@pytest.mark.parametrize("epoch", [None, True, False, -1, "0", 0.0, {}])
def test_explicit_malformed_epoch_is_not_coerced_to_zero(tmp_path, epoch):
    engine, canonical, tip, incoming = fixture_engine(tmp_path, {"lineage_epoch": epoch})
    assert engine._rehydrate_legacy_parent_prefix(incoming) == incoming


@pytest.mark.parametrize("damage", ["unverified", "wrong-session", "content-append"])
def test_omitted_zero_does_not_weaken_authentication_or_content_checks(tmp_path, damage):
    engine, canonical, tip, incoming = fixture_engine(tmp_path, {})
    if damage == "unverified":
        tip["verified"] = False
    elif damage == "wrong-session":
        tip["session_id"] = "different"
    else:
        incoming[-1]["content"] += " extra human content"
    assert engine._rehydrate_legacy_parent_prefix(incoming) == incoming
