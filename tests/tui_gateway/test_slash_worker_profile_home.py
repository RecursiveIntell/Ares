"""Tests for TUI gateway slash_worker profile_home propagation (#40677)."""

from unittest.mock import MagicMock, patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_slash_worker_accepts_profile_home(tmp_path):
    """A real temporary policy owner agrees across all environment readers."""
    ambient = tmp_path / "ambient"
    target = tmp_path / "profiles" / "work"
    ambient.mkdir()
    target.mkdir(parents=True)
    (ambient / "config.yaml").write_text("{}\n", encoding="utf-8")
    (target / "config.yaml").write_text("{}\n", encoding="utf-8")
    token = set_hermes_home_override(ambient)
    try:
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.stdout = MagicMock()
            mock_popen.return_value.stderr = MagicMock()
            from tui_gateway.server import _SlashWorker

            _SlashWorker(session_key="test_key", model="test-model", profile_home=str(target))
            assert mock_popen.called
            call_kwargs = mock_popen.call_args[1]
            assert "env" in call_kwargs
            assert call_kwargs["env"]["HERMES_HOME"] == str(target)
    finally:
        reset_hermes_home_override(token)
