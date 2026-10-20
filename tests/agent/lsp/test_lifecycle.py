"""Tests for profile service lifecycle: atexit handler, idempotent shutdown.

These cover the exit-cleanup behavior added to plug the language-server
process leak — without the atexit hook, ``hermes chat`` exits while
pyright/gopls/etc. are still alive on the host.
"""
from __future__ import annotations

import atexit
from unittest.mock import MagicMock

import pytest

from agent import lsp as lsp_module


@pytest.fixture(autouse=True)
def _reset_services(monkeypatch):
    """Isolate the production registry and drain only this test's fake services."""
    with lsp_module._service_lock:
        monkeypatch.setattr(lsp_module, "_services", {})
        monkeypatch.setattr(lsp_module, "_atexit_registered", False)
    yield
    lsp_module._atexit_shutdown()
    assert lsp_module._services == {}


def test_get_service_registers_atexit_handler_once(monkeypatch):
    """First call to ``get_service`` must register an atexit handler;
    subsequent calls must NOT register another one (Python's ``atexit``
    runs every registered callable, so a duplicate would shutdown
    twice — harmless but wasteful)."""
    fake_svc = MagicMock()
    fake_svc.is_active.return_value = True
    creations = []

    def create(cls, *, config, profile_boundary):
        from agent.secret_scope import ProfileEnvBoundary
        from hermes_constants import get_hermes_home

        assert isinstance(config, dict)
        assert isinstance(profile_boundary, ProfileEnvBoundary)
        assert profile_boundary.target_home == get_hermes_home().resolve()
        assert profile_boundary.target_generation
        creations.append((config, profile_boundary))
        return fake_svc

    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(create)
    )

    registrations = []

    def fake_register(fn):
        registrations.append(fn)

    monkeypatch.setattr(atexit, "register", fake_register)

    a = lsp_module.get_service()
    b = lsp_module.get_service()
    c = lsp_module.get_service()

    assert a is fake_svc
    assert b is fake_svc
    assert c is fake_svc
    assert len(creations) == 1
    assert len(lsp_module._services) == 1
    assert len(registrations) == 1
    # The registered callable must be our internal shutdown wrapper.
    assert registrations[0] is lsp_module._atexit_shutdown




def test_atexit_shutdown_swallows_exceptions_and_drains_other_services(tmp_path):
    from hermes_constants import hermes_home_key

    failing = MagicMock()
    sibling = MagicMock()
    registry_empty_at_shutdown = []

    def boom():
        registry_empty_at_shutdown.append(lsp_module._services == {})
        raise RuntimeError("server already dead")

    failing.shutdown.side_effect = boom
    with lsp_module._service_lock:
        lsp_module._services[hermes_home_key(tmp_path / "first")] = (("g1", "cfg"), failing)
        lsp_module._services[hermes_home_key(tmp_path / "second")] = (("g2", "cfg"), sibling)
    lsp_module._atexit_shutdown()
    failing.shutdown.assert_called_once_with()
    sibling.shutdown.assert_called_once_with()
    assert registry_empty_at_shutdown == [True]
    assert lsp_module._services == {}
    lsp_module._atexit_shutdown()
    assert failing.shutdown.call_count == 1
    assert sibling.shutdown.call_count == 1


def test_shutdown_service_idempotent(monkeypatch, tmp_path):
    """Calling shutdown twice must be safe — first call cleans up,
    second call no-ops (nothing to shut down)."""
    fake_svc = MagicMock()
    fake_svc.is_active.return_value = True
    fake_svc.shutdown = MagicMock()
    sibling = MagicMock()
    sibling.is_active.return_value = True
    creations = []

    def create(cls, *, config, profile_boundary):
        from agent.secret_scope import ProfileEnvBoundary
        from hermes_constants import get_hermes_home

        assert isinstance(config, dict)
        assert isinstance(profile_boundary, ProfileEnvBoundary)
        assert profile_boundary.target_home == get_hermes_home().resolve()
        assert profile_boundary.target_generation
        creations.append(profile_boundary.identity)
        return fake_svc if len(creations) == 1 else sibling

    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(create)
    )
    monkeypatch.setattr(atexit, "register", lambda fn: None)

    from hermes_constants import get_hermes_home, hermes_home_key

    first_home = get_hermes_home()
    assert lsp_module.get_service() is fake_svc
    sibling_home = tmp_path / "sibling"
    sibling_home.mkdir()
    with monkeypatch.context() as sibling_context:
        sibling_context.setenv("HERMES_HOME", str(sibling_home))
        assert lsp_module.get_service() is sibling
    assert len(creations) == 2
    lsp_module.shutdown_service()
    lsp_module.shutdown_service()  # must not raise

    assert fake_svc.shutdown.call_count == 1
    assert hermes_home_key(first_home) not in lsp_module._services
    assert lsp_module._services[hermes_home_key(sibling_home)][1] is sibling
    sibling.shutdown.assert_not_called()
    lsp_module.shutdown_service(profile_home=sibling_home)
    lsp_module.shutdown_service(profile_home=sibling_home)
    sibling.shutdown.assert_called_once_with()
    assert lsp_module._services == {}








