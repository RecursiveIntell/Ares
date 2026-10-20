"""Pressure evidence must come from a matching request/response, never a predicate."""
import copy
import json
from unittest.mock import patch

import pytest

from plugins.context_engine._context_governor import ContextGovernorEngine


@pytest.fixture
def governor(tmp_path):
    with patch("hermes_cli.config.load_config", return_value={}):
        engine = ContextGovernorEngine(
            binary=str(tmp_path / "no-native-governor"),
            store_dir=str(tmp_path / "governor"),
        )
    engine.update_model("test-model", 100_000, max_tokens=10_000,
                        provider="test", api_mode="chat_completions",
                        threshold_percent=0.5)
    return engine


def seed_pair(engine, rough=46_000, real=20_000):
    # The hostile audit's public-state seed, not a native receipt claim.
    engine.last_real_prompt_tokens = real
    engine.last_rough_tokens_when_real_prompt_fit = rough


def observe(engine, rough, real):
    engine.note_request_rough_estimate(rough)
    engine.update_from_response({"prompt_tokens": real})


def test_gradual_missing_usage_does_not_ratchet(governor):
    seed_pair(governor)
    decisions = []
    for rough in range(49_000, 118_001, 3_000):
        decisions.append(governor.should_defer_preflight_to_real_usage(rough))
        governor.update_from_response({})
    print(json.dumps({"rough_final": 118_000, "all_deferred": all(decisions),
                      "anchor": governor.last_rough_tokens_when_real_prompt_fit}))
    assert not all(decisions)
    assert governor.last_rough_tokens_when_real_prompt_fit == 46_000


def test_repeated_predicate_is_pure(governor):
    seed_pair(governor)
    decisions = [governor.should_defer_preflight_to_real_usage(49_000) for _ in range(8)]
    assert all(decisions)
    assert governor.last_rough_tokens_when_real_prompt_fit == 46_000


@pytest.mark.parametrize("awaiting,ineffective", [(False, 0), (True, 0), (False, 2), (True, 2)])
@pytest.mark.parametrize("rough", [81_000, 90_000, 100_000])
def test_every_deferral_branch_yields_at_emergency(governor, awaiting, ineffective, rough):
    seed_pair(governor, rough=rough - 1000)
    governor.awaiting_real_usage_after_compression = awaiting
    governor._ineffective_compression_count = ineffective
    assert not governor.should_defer_preflight_to_real_usage(rough)
    assert governor.should_compress(rough)


def test_matching_fresh_usage_replaces_pair_and_not_predicate(governor):
    observe(governor, 60_000, 15_000)
    assert governor.last_rough_tokens_when_real_prompt_fit == 60_000
    assert governor.last_real_prompt_tokens == 15_000
    assert governor.should_defer_preflight_to_real_usage(70_000)
    assert governor.last_rough_tokens_when_real_prompt_fit == 60_000
    observe(governor, 70_000, 25_000)
    assert governor.last_rough_tokens_when_real_prompt_fit == 70_000
    assert governor.last_real_prompt_tokens == 25_000


@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": 0}])
def test_missing_usage_preserves_old_pair_but_cannot_pair_a_later_unmatched_reading(governor, usage):
    observe(governor, 46_000, 20_000)
    governor.note_request_rough_estimate(50_000)
    governor.update_from_response(usage)
    assert governor.last_rough_tokens_when_real_prompt_fit == 46_000
    assert governor.last_real_prompt_tokens == 20_000
    governor.update_from_response({"prompt_tokens": 22_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 0
    assert not governor.should_defer_preflight_to_real_usage(52_000)


def test_compaction_diagnostic_does_not_mint_a_matching_pair(governor):
    governor.last_compression_rough_tokens = 60_000
    governor.awaiting_real_usage_after_compression = True
    governor.update_from_response({"prompt_tokens": 20_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 0
    assert not governor.should_defer_preflight_to_real_usage(62_000)


def test_latest_request_note_replaces_unsent_or_missing_usage_request(governor):
    governor.note_request_rough_estimate(70_000)
    governor.note_request_rough_estimate(50_000)
    governor.update_from_response({"input_tokens": 20_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 50_000


def test_known_noisy_estimate_retains_runway_until_cumulative_growth(governor):
    observe(governor, 60_000, 30_000)
    assert governor.should_defer_preflight_to_real_usage(72_000)
    assert not governor.should_defer_preflight_to_real_usage(75_000)
    assert governor.last_rough_tokens_when_real_prompt_fit == 60_000


def test_one_post_compaction_measurement_opportunity_is_bounded(governor):
    governor.awaiting_real_usage_after_compression = True
    assert governor.should_defer_preflight_to_real_usage(50_000)
    governor.update_from_response({})
    assert not governor.should_defer_preflight_to_real_usage(50_000)


def test_pressured_real_usage_clears_fit(governor):
    observe(governor, 60_000, 45_000)
    assert governor.last_rough_tokens_when_real_prompt_fit == 0
    assert not governor.should_defer_preflight_to_real_usage(60_000)


def test_clone_and_reset_do_not_transfer_pending_request(governor):
    observe(governor, 60_000, 20_000)
    governor.note_request_rough_estimate(70_000)
    with patch("hermes_cli.config.load_config", return_value={}):
        clone = copy.deepcopy(governor)
    assert clone.last_rough_tokens_when_real_prompt_fit == 60_000
    clone.update_from_response({"prompt_tokens": 25_000})
    assert clone.last_rough_tokens_when_real_prompt_fit == 0
    assert governor.last_rough_tokens_when_real_prompt_fit == 60_000
    governor.on_session_reset()
    governor.update_from_response({"prompt_tokens": 25_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 0


@pytest.mark.parametrize("changed", [
    {"model": "other"}, {"provider": "other"}, {"api_mode": "codex_responses"},
    {"base_url": "https://other.invalid"}, {"context_length": 80_000},
    {"max_tokens": 20_000}, {"threshold_percent": 0.6},
])
def test_route_or_budget_rebinding_invalidates_old_pressure_evidence(governor, changed):
    observe(governor, 60_000, 20_000)
    governor.note_request_rough_estimate(70_000)
    values = dict(model="test-model", context_length=100_000, max_tokens=10_000,
                  provider="test", api_mode="chat_completions", base_url="",
                  threshold_percent=0.5)
    values.update(changed)
    governor.update_model(**values)
    assert governor.last_rough_tokens_when_real_prompt_fit == 0
    governor.update_from_response({"prompt_tokens": 20_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 0


def test_same_route_update_preserves_a_matching_pending_request(governor):
    governor.note_request_rough_estimate(60_000)
    governor.update_model("test-model", 100_000, max_tokens=10_000,
                          provider="test", api_mode="chat_completions",
                          threshold_percent=0.5)
    governor.update_from_response({"prompt_tokens": 20_000})
    assert governor.last_rough_tokens_when_real_prompt_fit == 60_000
