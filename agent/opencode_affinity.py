"""``x-opencode-session`` — OpenCode relay session-affinity header.

OpenCode requires this header as a backend-affinity hint. Session-bound main
and auxiliary builders share this owner. The ambient conversation root takes
precedence over the explicit session scope, matching this Ares version's
existing conversation-context contract. Cron timestamps are normalized through
the existing cache-scope helper. No process-global identity is invented for
callers outside a conversation.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    """True when *provider* or *base_url* addresses the OpenCode relay.

    Matches the built-in opencode-zen/go/free providers, custom
    ``opencode-<family>-*`` providers, and any base_url hosted on opencode.ai.
    """
    from hermes_cli.models import opencode_provider_family
    from utils import base_url_host_matches

    return (
        opencode_provider_family(provider) is not None
        or base_url_host_matches(str(base_url or ""), "opencode.ai")
    )


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{"x-opencode-session": <key>}`` for OpenCode targets, else ``{}``."""
    if not is_opencode_target(provider, base_url):
        return {}
    from agent.portal_tags import get_conversation_context
    from agent.transports.codex import _cache_scope_from_session_id

    key = _cache_scope_from_session_id(get_conversation_context() or session_id)
    if not key:
        return {}
    # Session APIs may admit Unicode IDs. Keep canonical identity unchanged,
    # but encode its wire projection as a bounded ASCII affinity token.
    token = "oc1_" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return {OPENCODE_SESSION_HEADER: token}


def merge_opencode_session_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Merge the affinity header into ``kwargs["extra_headers"]`` (in place).

    Existing per-request headers win, so a caller-pinned value is preserved.
    Non-OpenCode targets are left untouched.
    """
    headers = opencode_session_headers(provider, base_url, session_id)
    if headers:
        existing = kwargs.get("extra_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in headers.items():
            merged.setdefault(key, value)
        kwargs["extra_headers"] = merged
    return kwargs
