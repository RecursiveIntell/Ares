"""Environment variable passthrough registry.

Skills that declare ``required_environment_variables`` in their frontmatter
need those vars available in sandboxed execution environments (execute_code,
terminal).  By default both sandboxes strip secrets from the child process
environment for security.  This module provides a session-scoped allowlist
so skill-declared vars (and user-configured overrides) pass through.

Two sources feed the allowlist:

1. **Skill declarations** — when a skill is loaded via ``skill_view``, its
   ``required_environment_variables`` are registered here automatically.
2. **User config** — ``terminal.env_passthrough`` in config.yaml lets users
   explicitly allowlist vars for non-skill use cases.

Both ``code_execution_tool.py`` and ``tools/environments/local.py`` consult
:func:`is_env_passthrough` before stripping a variable.
When profile multiplexing is active, their forwarded values are resolved
through the current profile's secret scope rather than the process environment.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from hermes_cli.config import LaunchConfigPolicy

logger = logging.getLogger(__name__)

# Session-scoped set of env var names that should pass through to sandboxes.
# Backed by ContextVar to prevent cross-session data bleed in the gateway pipeline.
_allowed_env_vars_var: ContextVar[set[str]] = ContextVar("_allowed_env_vars")


def _get_allowed() -> set[str]:
    """Get or create the allowed env vars set for the current context/session."""
    try:
        return _allowed_env_vars_var.get()
    except LookupError:
        val: set[str] = set()
        _allowed_env_vars_var.set(val)
        return val


# Last observed config projection. Kept for test/debug compatibility only; it
# is never an authorization cache because the active profile may change on
# every multiplexed turn.
_config_passthrough: frozenset[str] | None = None


def _is_hermes_provider_credential(name: str) -> bool:
    """True if ``name`` is a Hermes-managed provider credential (API key,
    token, or similar) per ``_HERMES_PROVIDER_ENV_BLOCKLIST``.

    Skill-declared ``required_environment_variables`` frontmatter must
    not be able to override this list — that was the bypass in
    GHSA-rhgp-j443-p4rf where a malicious skill registered
    ``ANTHROPIC_TOKEN`` / ``OPENAI_API_KEY`` as passthrough and received
    the credential in the ``execute_code`` child process, defeating the
    sandbox's scrubbing guarantee.

    Non-Hermes API keys (TENOR_API_KEY, NOTION_TOKEN, etc.) are NOT
    in the blocklist and remain legitimately registerable — skills that
    wrap third-party APIs still work.

    Fail closed: if the authoritative blocklist cannot be imported (partial
    install, import-time error, etc.) we treat the name as a protected
    provider credential and refuse passthrough, rather than fall open and
    let a skill tunnel a Hermes credential into the execute_code child.
    """
    try:
        from tools.environments.local import (
            _is_blocked_provider_env,
            _is_hermes_internal_secret,
        )
    except Exception as e:
        logger.warning(
            "env passthrough: provider credential blocklist import failed; "
            "failing closed and refusing passthrough registration for %r: %s",
            name,
            e,
        )
        return True
    # Dynamically-generated Hermes-internal secrets (AUXILIARY_*_API_KEY /
    # _BASE_URL side-LLM credentials, GATEWAY_RELAY_* relay-auth) are provider
    # credentials the static blocklist can't enumerate — they're injected per
    # task/relay at gateway startup. A skill must not be able to register them
    # as passthrough and tunnel them into an execute_code / terminal child.
    if _is_hermes_internal_secret(name):
        return True
    return _is_blocked_provider_env(name)


def register_env_passthrough(var_names: Iterable[str]) -> None:
    """Register environment variable names as allowed in sandboxed environments.

    Typically called when a skill declares ``required_environment_variables``.

    Variables that are Hermes-managed provider credentials (from
    ``_HERMES_PROVIDER_ENV_BLOCKLIST``) are rejected here to preserve
    the ``execute_code`` sandbox's credential-scrubbing guarantee per
    GHSA-rhgp-j443-p4rf. A skill that needs to talk to a Hermes-managed
    provider should do so via the agent's main-process tools (web_search,
    web_extract, etc.) where the credential remains safely in the main
    process.

    Non-Hermes third-party API keys (TENOR_API_KEY, NOTION_TOKEN, etc.)
    pass through normally — they were never in the sandbox scrub list.
    """
    for name in var_names:
        name = name.strip()
        if not name:
            continue
        if _is_hermes_provider_credential(name):
            logger.warning(
                "env passthrough: refusing to register Hermes provider "
                "credential %r (blocked by _HERMES_PROVIDER_ENV_BLOCKLIST). "
                "Skills must not override the execute_code sandbox's "
                "credential scrubbing; see GHSA-rhgp-j443-p4rf.",
                name,
            )
            continue
        _get_allowed().add(name)
        logger.debug("env passthrough: registered %s", name)


def _compile_protected(config_policy: LaunchConfigPolicy) -> Callable[[str], bool]:
    """Reuse local's name classifiers without resolving config for every name."""
    try:
        from tools.environments.local import (
            _build_provider_env_blocklist,
            _is_blocked_provider_env,
            _is_hermes_internal_secret,
        )
        blocked = frozenset(name.upper() for name in _build_provider_env_blocklist())
    except ImportError:
        return lambda name: True

    def protected(name: str) -> bool:
        return (
            _is_hermes_internal_secret(name, bws_token_env=config_policy.bws_token_env)
            or _is_blocked_provider_env(name, blocked_names=blocked)
        )

    return protected


def _load_config_passthrough(
    profile_home: str | os.PathLike[str] | None = None,
    *,
    config_policy: LaunchConfigPolicy | None = None,
    is_protected: Callable[[str], bool] | None = None,
) -> frozenset[str]:
    """Load the active profile's ``terminal.env_passthrough`` exactly now.

    Launch callers supply the typed config projection and compiled predicate.
    Standalone callers resolve them fresh through the canonical owners.
    """
    global _config_passthrough
    if config_policy is None:
        from hermes_cli.config import read_launch_config_policy

        config_policy = read_launch_config_policy(profile_home)
    if is_protected is None:
        is_protected = _compile_protected(config_policy)
    result = frozenset(
        name for name in config_policy.passthrough_names if not is_protected(name)
    )
    _config_passthrough = result
    return result


def is_env_passthrough(
    var_name: str,
    *,
    profile_home: str | os.PathLike[str] | None = None,
) -> bool:
    """Check whether *var_name* is allowed to pass through to sandboxes.

    Returns ``True`` if the variable was registered by a skill or listed in
    the user's ``tools.env_passthrough`` config.
    """
    return var_name in get_all_passthrough(profile_home=profile_home)


def get_all_passthrough(
    *,
    profile_home: str | os.PathLike[str] | None = None,
    config_policy: LaunchConfigPolicy | None = None,
    is_protected: Callable[[str], bool] | None = None,
) -> frozenset[str]:
    """Return the union of skill-registered and config-based passthrough vars."""
    if config_policy is None:
        from hermes_cli.config import read_launch_config_policy

        config_policy = read_launch_config_policy(profile_home)
    if is_protected is None:
        is_protected = _compile_protected(config_policy)
    configured = _load_config_passthrough(
        profile_home, config_policy=config_policy, is_protected=is_protected,
    )
    allowed = frozenset(_get_allowed())
    allowed = frozenset(name for name in allowed if not is_protected(name))
    return allowed | configured


def resolve_passthrough_value(
    name: str,
    fallback: str | None = None,
) -> str | None:
    """Resolve an allowlisted variable without crossing profile boundaries.

    ``fallback`` is the value the caller would have forwarded before profile
    secret scopes existed (typically a snapshot of ``os.environ`` or the
    current profile's ``.env``).  An active multiplex scope is authoritative:
    a missing key returns ``None`` and never falls back to the process-global
    environment.  An unscoped read while multiplexing is active raises the
    fail-closed ``UnscopedSecretError`` from :mod:`agent.secret_scope`.

    Outside multiplexing, an installed scope keeps the existing overlay
    semantics and an unscoped caller keeps its already-resolved fallback.
    """
    from agent.secret_scope import (
        _is_global_env,
        current_secret_scope,
        get_secret,
        is_multiplex_active,
    )

    # Global terminal/runtime settings are not profile secrets.  ``fallback``
    # is already the caller's effective value (including an explicit per-call
    # override), so preserve it instead of replacing it with the process-wide
    # value while a multiplex scope is active.
    if _is_global_env(name) and fallback is not None:
        return fallback

    scope = current_secret_scope()
    multiplex_active = is_multiplex_active()
    if scope is None:
        if multiplex_active:
            return get_secret(name)
        return fallback
    return get_secret(name, None if multiplex_active else fallback)


def clear_env_passthrough() -> None:
    """Reset the skill-scoped allowlist (e.g. on session reset)."""
    _get_allowed().clear()
