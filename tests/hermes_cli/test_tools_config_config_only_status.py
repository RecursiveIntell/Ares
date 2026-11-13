"""Status reporting for config-only toolsets.

``stt`` is a config-only capability: it ships zero tool schemas and is never a
member of ``platform_toolsets`` -- its switch is ``stt.enabled``. Reporting it
by platform membership renders it permanently disabled, contradicting the
config the operator actually set (observed live: ``stt.enabled: true`` while
``hermes tools list`` printed ``disabled``).
"""
from __future__ import annotations

from hermes_cli.tools_config import _config_only_toolset_status, _print_tools_list


def test_config_only_status_follows_its_own_section():
    assert _config_only_toolset_status("stt", {"stt": {"enabled": True}}) is True
    assert _config_only_toolset_status("stt", {"stt": {"enabled": False}}) is False


def test_config_only_status_defers_when_section_is_silent():
    """No explicit switch means 'no opinion' so the caller falls back."""
    assert _config_only_toolset_status("stt", {}) is None
    assert _config_only_toolset_status("stt", {"stt": {}}) is None
    assert _config_only_toolset_status("stt", {"stt": None}) is None


def test_platform_toolsets_are_unaffected():
    """Only the declared config-only set may bypass membership reporting."""
    assert _config_only_toolset_status("web", {"web": {"enabled": False}}) is None
    assert _config_only_toolset_status("terminal", {"terminal": {"enabled": True}}) is None


def test_tools_list_reports_stt_from_config_not_membership(capsys):
    """The reported surface must agree with the config, not with membership."""
    _print_tools_list(set(), {}, "cli", {"stt": {"enabled": True}})
    out = capsys.readouterr().out
    stt_lines = [ln for ln in out.splitlines() if "  stt  " in ln]
    assert stt_lines, f"stt missing from tools list output:\n{out}"
    assert "enabled" in stt_lines[0] and "disabled" not in stt_lines[0], stt_lines[0]

    # And the inverse: an explicitly disabled stt still reports disabled.
    _print_tools_list(set(), {}, "cli", {"stt": {"enabled": False}})
    out = capsys.readouterr().out
    stt_lines = [ln for ln in out.splitlines() if "  stt  " in ln]
    assert stt_lines and "disabled" in stt_lines[0], stt_lines
