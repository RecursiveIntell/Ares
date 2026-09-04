"""Dashboard bearer-token provider for server-enrolled mobile devices.

This module registers only a credential verifier.  It intentionally does not
register any token-authable route: each mobile endpoint must opt in through the
existing exact-path token-auth seam.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from hermes_constants import hermes_home_key
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from hermes_cli.dashboard_auth.registry import get_provider, register_provider
from hermes_state import SessionDB


class MobileDeviceAuthProvider(DashboardAuthProvider):
    """Verify active, server-enrolled mobile-device bearer tokens."""

    name = "mobile-device"
    display_name = "Mobile Device (service credential)"
    supports_session = False
    supports_token = True

    def __init__(self, *, db_factory: Callable[..., SessionDB] = SessionDB) -> None:
        self._db_factory = db_factory

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        """Resolve a bearer through SessionDB, the canonical token owner."""
        if not token:
            return None
        db = self._db_factory()
        try:
            device = db.mobile_device_verify_token(token)
        finally:
            db.close()
        if device is None:
            return None
        return TokenPrincipal(
            principal=str(device["device_id"]),
            provider=self.name,
            scopes=("mobile:device",),
        )

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("MobileDeviceAuthProvider is non-interactive.")

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError("MobileDeviceAuthProvider is non-interactive.")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("MobileDeviceAuthProvider is non-interactive.")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


def register_mobile_device_provider() -> MobileDeviceAuthProvider:
    """Register the provider for the active Hermes-home scope, once."""
    scope = hermes_home_key()
    existing = get_provider(MobileDeviceAuthProvider.name, scope=scope)
    if isinstance(existing, MobileDeviceAuthProvider):
        return existing
    provider = MobileDeviceAuthProvider()
    register_provider(provider, scope=scope)
    return provider
