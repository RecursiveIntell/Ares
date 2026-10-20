"""Regression tests for xAI provider label disambiguation."""

from hermes_cli.providers import get_label


def test_xai_labels_remain_distinct_without_optional_catalog(monkeypatch):
    from hermes_cli import providers

    monkeypatch.setattr(providers, "get_provider", lambda *args, **kwargs: None)
    assert providers.get_label("xai") == "xAI"
    assert providers.get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert providers.get_label("grok-oauth") == providers.get_label("xai-oauth")


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label():
    """The model picker must distinguish xAI API-key and OAuth providers."""
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"


