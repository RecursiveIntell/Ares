"""Route admission limits and signature-compatible model budget handoffs."""

from unittest.mock import patch

import pytest

from agent.auxiliary_client import _update_compressor_model
from plugins.context_engine._context_governor import ContextGovernorEngine


@pytest.fixture
def governor(tmp_path):
    with patch("hermes_cli.config.load_config", return_value={}):
        return ContextGovernorEngine(
            binary=str(tmp_path / "fake-governor"),
            store_dir=str(tmp_path / "governor-store"),
        )


@pytest.mark.parametrize(
    ("policy_budget", "window", "reserve", "expected"),
    [
        (128_000, 64_000, 4096, 59_904),
        # CHANGED: reserve None now derives a real reservation (3200 at this
        # window) instead of being treated as zero, so the input clamp tightens.
        (128_000, 64_000, None, 60_800),
        (8000, 64_000, 4096, 8000),
        (128_000, 256, 128, 128),
        # CHANGED: with no explicit token_budget the target comes from
        # ContextBudget (0.45 x effective window) instead of a bare 20%.
        (None, 64_000, 4096, 26_956),
        (None, 256, 128, 128),
        (128_000, 0, 4096, 128_000),
        (128_000, 1_050_000, 4096, 128_000),
        # CHANGED: derived reservation (32768) is subtracted before the clamp.
        (1_050_000, 1_050_000, None, 1_017_232),
        (1_050_000, 1_048_576, 4096, 1_044_480),
    ],
)
def test_policy_target_cannot_exceed_known_input_window(
    governor, policy_budget, window, reserve, expected
):
    governor._policy["token_budget"] = policy_budget
    governor.update_model("test-model", window, max_tokens=reserve)
    assert governor._target_tokens(100_000) == expected
    if window:
        assert expected <= window - (reserve or 0)
    else:
        assert governor.threshold_tokens == 0


@pytest.mark.parametrize("reserve", [64_000, 64_001])
def test_exhausted_input_budget_refuses_update_without_rebinding(governor, reserve):
    governor.update_model("valid-model", 64_000, max_tokens=4096)
    with pytest.raises(ValueError, match="no input budget"):
        governor.update_model("invalid-model", 64_000, max_tokens=reserve)
    assert governor.model == "valid-model"
    assert governor.max_tokens == 4096
    assert governor.context_length == 64_000


def test_handoff_forwards_and_clears_the_engine_response_reserve(governor):
    governor._policy["token_budget"] = 128_000
    # CHANGED: a cleared reservation (None) is no longer treated as zero — the
    # budget owner derives 3200 for this window, so the input clamp tightens.
    for reserve, target in [(4096, 59_904), (None, 60_800)]:
        _update_compressor_model(
            governor,
            model="test-model",
            context_length=64_000,
            max_tokens=reserve,
            threshold_percent=0.75,
        )
        assert governor.max_tokens == reserve
        assert governor._target_tokens(100_000) == target


def test_legacy_engine_receives_only_its_declared_update_api():
    calls = []

    class LegacyEngine:
        def update_model(
            self, model, context_length, base_url="", api_key="", provider="", api_mode=""
        ):
            calls.append((model, context_length))

    _update_compressor_model(
        LegacyEngine(),
        model="legacy-model",
        context_length=64_000,
        max_tokens=4096,
        threshold_percent=0.75,
    )
    assert calls == [("legacy-model", 64_000)]
