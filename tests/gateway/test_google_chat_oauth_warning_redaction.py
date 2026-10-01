"""Token-load diagnostics preserve credential fallback without echoing data."""

import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.platforms.google_chat import oauth


PAYLOAD = "SYNTHETIC_TOKEN_PAYLOAD_94b2"


@pytest.fixture
def token_loader(tmp_path, monkeypatch):
    # Mock only the optional SDK boundary. Execute the real OAuth loader,
    # path existence check and credential-file permission check on a temp file.
    modules = {}
    for name in ("google", "google.oauth2", "google.oauth2.credentials", "google.auth",
                 "google.auth.transport", "google.auth.transport.requests"):
        module = ModuleType(name)
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(modules[parent], child, module)

    credentials = Mock()
    request = Mock(side_effect=AssertionError("Token loading must not call a live refresh"))
    modules["google.oauth2.credentials"].Credentials = credentials
    modules["google.auth.transport.requests"].Request = request
    path = tmp_path / "synthetic-token.json"
    monkeypatch.setattr(oauth, "_token_path", lambda email: path)
    return path, credentials.from_authorized_user_file, request


class UnprintableError(Exception):
    def __str__(self):
        raise AssertionError("Credential exceptions must never be formatted")


@pytest.mark.parametrize("error", [ValueError(PAYLOAD), UnprintableError(PAYLOAD)],
                         ids=["payload-in-exception", "unprintable-exception"])
def test_corrupt_token_warning_does_not_format_exception(token_loader, caplog, error):
    path, load, request = token_loader
    path.write_text(PAYLOAD, encoding="utf-8")
    path.chmod(0o600)
    load.side_effect = error

    with caplog.at_level(logging.WARNING, logger=oauth.logger.name):
        assert oauth.load_user_credentials("synthetic@example.test") is None

    load.assert_called_once_with(str(path))
    request.assert_not_called()
    assert path.read_text(encoding="utf-8") == PAYLOAD
    records = [record for record in caplog.records if record.name == oauth.logger.name]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "token" in message and "corrupt" in message
    assert PAYLOAD not in message
    assert not records[0].args
    assert records[0].exc_info is None
    assert records[0].stack_info is None


def test_missing_token_returns_none_without_sdk_loading(token_loader, caplog):
    path, load, request = token_loader
    assert not path.exists()
    assert oauth.load_user_credentials() is None
    load.assert_not_called()
    request.assert_not_called()
    assert not caplog.records


def test_valid_token_returns_same_credentials_without_refresh_or_rewrite(token_loader, caplog):
    path, load, request = token_loader
    path.write_text(PAYLOAD, encoding="utf-8")
    path.chmod(0o600)
    creds = SimpleNamespace(valid=True, refresh=Mock())
    load.return_value = creds

    assert oauth.load_user_credentials() is creds
    load.assert_called_once_with(str(path))
    request.assert_not_called()
    creds.refresh.assert_not_called()
    assert path.read_text(encoding="utf-8") == PAYLOAD
    assert not caplog.records
