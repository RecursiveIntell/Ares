"""Local execution environment — spawn-per-call with session snapshot."""

import contextlib
import logging
import ntpath
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from hermes_constants import get_process_hermes_home
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _pipe_stdin
from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments.local_env_policy import (
    _ALWAYS_STRIP_KEYS, _HERMES_PROVIDER_ENV_BLOCKLIST, _HERMES_PROVIDER_ENV_FORCE_PREFIX,
    _is_hermes_internal_secret, _is_terminal_first_party_env,
    _matches_terminal_first_party_prefix, _plugin_terminal_env_strip_keys)
from tools.environments.local_gitbash_probe import (
    _bash_probe_details_cache, _bash_starts, _git_bash_aslr_help,
    _looks_like_msys_spawn_failure, _mandatory_aslr_enabled)
from tools.environments.local_pythonpath import (
    _build_hermes_repo_root_aliases, _strip_hermes_owned_pythonpath_and_runtime_markers)


if TYPE_CHECKING:
    from agent.secret_scope import ProfileEnvBoundary

_IS_WINDOWS = platform.system() == "Windows"

logger = logging.getLogger(__name__)

# --- Terminal temp-cache pruning ---
# get_temp_dir() defaults to HERMES_HOME/cache/terminal (real storage, not tmpfs), so
# stale artifacts don't vanish on reboot: the gateway housekeeping loop prunes hourly
# and a once-per-process sweep covers CLI-only installs.
TERMINAL_TEMP_MAX_AGE_HOURS = 72
_terminal_temp_prune_lock = threading.Lock()
_terminal_temp_pruned_once = False
# Background artifacts come in triplets (hermes_bg_<id>.log/.pid/.exit). A live
# server's .pid never changes mtime while its .log does, so age is judged per
# GROUP (newest mtime sharing a stem) to keep pid/exit files of live sessions.
_BG_GROUP_RE = re.compile(r"^(hermes_bg_[A-Za-z0-9_-]+)\.(log|pid|exit)$")


def _default_terminal_temp_dir() -> "Path | None":
    """Return HERMES_HOME/cache/terminal, or None if unresolvable."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "cache" / "terminal"
    except Exception:
        return None


def cleanup_terminal_temp_cache(max_age_hours: int = TERMINAL_TEMP_MAX_AGE_HOURS) -> int:
    """Delete session temp artifacts older than *max_age_hours*; return count.
    Only the managed default dir is pruned — never a user-pointed ``terminal.temp_dir``."""
    root = _default_terminal_temp_dir()
    if root is None:
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0

    mtimes: dict[Path, float] = {}
    group_newest: dict[str, float] = {}
    for f in entries:
        try:
            mtimes[f] = mt = f.stat().st_mtime
        except OSError:
            continue
        if m := _BG_GROUP_RE.match(f.name):
            group_newest[m.group(1)] = max(group_newest.get(m.group(1), 0.0), mt)

    removed = 0
    for f, mt in mtimes.items():
        m = _BG_GROUP_RE.match(f.name)
        if (group_newest[m.group(1)] if m else mt) >= cutoff:
            continue
        try:
            shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _prune_terminal_temp_once() -> None:
    """Best-effort prune, at most once per process (CLI-only installs)."""
    global _terminal_temp_pruned_once
    with _terminal_temp_prune_lock:
        if _terminal_temp_pruned_once:
            return
        _terminal_temp_pruned_once = True
    try:
        cleanup_terminal_temp_cache()
    except Exception as exc:
        logger.debug("Terminal temp prune failed: %s", exc)


# --- Windows / MSYS path translation ---
def _msys_to_windows_path(cwd: str) -> str:
    """``/c/Users/x`` / ``/cygdrive/c/..`` / ``/mnt/c/..`` -> native ``C:\\Users\\x`` so
    ``isdir``/``Popen(cwd=)`` find it. No-op off Windows, for empty input and for
    multi-segment POSIX paths like ``/home/x``; idempotent on native paths."""
    m = _IS_WINDOWS and cwd and re.match(r'^/(?:(?:cygdrive|mnt)/)?([a-zA-Z])(/.*)?$', cwd)
    if not m:
        return cwd
    tail = (m.group(2) or "").replace('/', '\\')
    return f"{m.group(1).upper()}:{tail or chr(92)}"  # chr(92) = backslash


def _resolve_local_initial_cwd(cwd: str) -> str:
    """Resolve the initial cwd to an absolute host path. A relative ``TERMINAL_CWD``
    naming the launch directory would otherwise make the wrapper ``cd`` *inside*
    the project; anchor it once so ``Popen(cwd=)`` and the in-shell ``cd`` agree."""
    expanded = os.path.expanduser(cwd) if cwd else os.getcwd()
    if _IS_WINDOWS:
        expanded = _msys_to_windows_path(expanded)
        # ntpath explicitly: with _IS_WINDOWS patched on a POSIX host,
        # os.path.isabs would reject ``C:\Users\x`` and mangle it below.
        if ntpath.isabs(expanded):
            return expanded
    if os.path.isabs(expanded):
        return expanded
    candidate = os.path.abspath(expanded)
    current = os.getcwd()
    # Relative name matching the tail of the current dir: use the current dir.
    if not os.path.isdir(candidate):
        wanted, have = Path(expanded).parts, Path(current).parts
        if wanted and len(wanted) <= len(have) and have[-len(wanted):] == wanted:
            return current
    return candidate


def _windows_to_msys_path(cwd: str) -> str:
    """Native ``C:\\Users\\x`` -> Git Bash ``/c/Users/x`` so ``builtin cd`` resolves
    it. No-op off Windows / for non-drive paths."""
    m = _IS_WINDOWS and cwd and re.match(r'^([a-zA-Z]):[\\/]*(.*)$', cwd)
    if not m:
        return cwd
    tail = (m.group(2) or "").replace('\\', '/').lstrip('/')
    return f"/{m.group(1).lower()}/{tail}"


def _bash_safe_path(path: str) -> str:
    """*path* safe to embed in a Git Bash script: ``C:\\Users\\x`` / ``C:/Users/x``
    become ``/c/Users/x`` (MSYS argument conversion mangles ``C:/`` forms) and
    leftover backslashes are normalized so bash does not eat ``\\U``. No-op off Windows."""
    return _windows_to_msys_path(path).replace("\\", "/") if _IS_WINDOWS and path else path


def _quote_bash_path(path: str) -> str:
    """Quote *path* for safe interpolation into a Git Bash script on Windows."""
    import shlex
    return shlex.quote(_bash_safe_path(path))


def _cwd_usable(path: str) -> bool:
    """True when *path* is a directory this process can actually chdir into
    (``isdir`` alone passes ``/root`` for a non-root user; ``Popen(cwd=)`` then dies)."""
    return os.path.isdir(path) and os.access(path, os.X_OK)


def _resolve_safe_cwd(cwd: str) -> str:
    """``cwd`` if enterable, else the nearest usable ancestor, else
    ``tempfile.gettempdir()``. MSYS paths are normalized first on Windows so a valid
    ``pwd -P`` result is not rejected. Lets ``_run_bash`` recover from a deleted or
    inaccessible cwd instead of ``Popen`` raising and wedging every later call.

    Used by ``_run_bash`` to recover when the configured cwd is gone — most commonly because a previous tool
    call deleted its own working directory (issue #17558) — or inaccessible to this user, e.g. ``/root``
    leaking from a root-launched CLI session into a non-root gateway's cron jobs (issue #65583). Without
    this guard, ``subprocess.Popen(..., cwd=...)`` raises ``FileNotFoundError``/``PermissionError`` before
    bash starts, wedging every subsequent terminal call until the gateway restarts.
    """
    cwd = _msys_to_windows_path(cwd)
    if cwd and _cwd_usable(cwd):
        return cwd
    if cwd and os.path.isdir(cwd):
        logger.warning(
            "Configured terminal cwd %r exists but is not accessible to "
            "this user (uid=%s) — falling back to the nearest usable "
            "directory. If this is a gateway/cron process, check for "
            "root-owned paths leaking into terminal.cwd / TERMINAL_CWD "
            "(#65583).",
            cwd, getattr(os, "getuid", lambda: "?")())
    parent = os.path.dirname(cwd) if cwd else ""
    while parent and not _cwd_usable(parent):
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            return tempfile.gettempdir()  # filesystem root itself is unusable
        parent = next_parent
    return parent or tempfile.gettempdir()


# Hermes-internal env vars that should NOT leak into terminal subprocesses.
_HERMES_PROVIDER_ENV_FORCE_PREFIX = "_HERMES_FORCE_"

# Apptainer/Singularity rename these host variables before injecting them into
# a container.  Evaluate the target name as well as the wrapper name so
# ``APPTAINERENV_GH_TOKEN`` cannot tunnel a blocked credential past the common
# child-process sanitizer.
_CONTAINER_ENV_FORWARD_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")


def _credential_target_env_name(key: str) -> str:
    """Return the effective credential name after nested forwarding wrappers."""
    value = str(key)
    changed = True
    while changed:
        changed = False
        upper = value.upper()
        for prefix in _CONTAINER_ENV_FORWARD_PREFIXES:
            if upper.startswith(prefix):
                value = value[len(prefix):]
                changed = True
                break
    return value

# Hermes-managed AWS *inference* credentials for ``auth_type="aws_sdk"``
# providers (Bedrock).  Scoped DELIBERATELY NARROW: this lists only the
# Bedrock-specific bearer token, which is a Hermes inference secret exactly
# analogous to ``OPENAI_API_KEY`` — nobody drives the ``aws``/``terraform``/
# ``boto3`` toolchain off it, so stripping it from terminal/execute_code
# subprocesses costs no user capability.
#
# The GENERAL AWS credential chain (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AWS_SESSION_TOKEN, AWS_PROFILE, and the config/role pointers) is INTENTIONALLY
# left inheritable.  Per SECURITY.md §3.2 the local terminal is the user's
# trusted operator shell; the agent having the same general AWS access the
# user's own shell has is the intended posture, not a leak.  Hard-blocklisting
# those vars would (a) regress every user who runs aws/terraform/cdk/boto3 in
# the agent terminal — not just Bedrock users, since the registry is iterated
# unconditionally — and (b) be unrecoverable, because env_passthrough.py
# refuses to re-allow anything in this blocklist (GHSA-rhgp-j443-p4rf).  See
# issue #32314 discussion.
_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({
    "AWS_BEARER_TOKEN_BEDROCK",
})


def _build_provider_env_blocklist() -> frozenset:
    """Derive the blocklist from provider, tool, and gateway config."""
    blocked: set[str] = set()

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.auth_type == "aws_sdk":
                blocked.update(_AWS_SDK_CREDENTIAL_ENV_VARS)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass

    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS
        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if category in {"tool", "messaging"}:
                blocked.add(name)
            elif category == "setting" and metadata.get("password"):
                blocked.add(name)
    except ImportError:
        pass

    blocked.update({
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        # Path to a GCP service-account JSON, not a bare key, so
        # OPTIONAL_ENV_VARS marks it password=False and the loop above skips it.
        "VERTEX_CREDENTIALS_PATH",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "PERPLEXITY_API_KEY",
        "COHERE_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "HELICONE_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_ALLOWED_USERS",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_HOME_CHANNEL_NAME",
        "SIGNAL_IGNORE_STORIES",
        "HASS_TOKEN",
        "HASS_URL",
        "EMAIL_ADDRESS",
        "EMAIL_PASSWORD",
        "EMAIL_IMAP_HOST",
        "EMAIL_SMTP_HOST",
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "HERMES_DASHBOARD_SESSION_TOKEN",
        "GATEWAY_ALLOWED_USERS",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "GATEWAY_RELAY_ID",
        "GATEWAY_RELAY_SECRET",
        "GATEWAY_RELAY_DELIVERY_KEY",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    })
    # CLAUDE_CODE_OAUTH_TOKEN is deliberately NOT stripped.  It is set and
    # owned by the user's Claude Code install (subscription OAuth), not a
    # Hermes-managed inference credential — Claude subscription auth is not a
    # working Hermes provider path.  Stripping it broke agent-spawned
    # ``claude`` CLIs: the child fell through to the shared macOS Keychain /
    # ``~/.claude/.credentials.json`` store and, on auth failure, cleared it,
    # logging the user out of their interactive Claude sessions (#55878).
    # It arrives via the registry loop above (anthropic api_key_env_vars),
    # so remove it explicitly.
    blocked.discard("CLAUDE_CODE_OAUTH_TOKEN")
    return frozenset(blocked)


_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()
_HERMES_PROVIDER_ENV_BLOCKLIST_UPPER = frozenset(
    key.upper() for key in _HERMES_PROVIDER_ENV_BLOCKLIST
)


def _build_model_provider_env_names() -> frozenset[str]:
    """Return exact model-provider credential and endpoint env names."""
    names: set[str] = {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_EC2_METADATA_DISABLED",
    }
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        for provider in PROVIDER_REGISTRY.values():
            names.update(str(item).upper() for item in provider.api_key_env_vars)
            if provider.base_url_env_var:
                names.add(str(provider.base_url_env_var).upper())
    except ImportError:
        pass
    return frozenset(name for name in names if name)


_MODEL_PROVIDER_ENV_NAMES_UPPER = _build_model_provider_env_names()


def _is_blocked_provider_env(key: str) -> bool:
    """Match provider credentials case-insensitively and through wrappers.

    Windows environment keys are case-insensitive, and Apptainer/Singularity
    can rename ``APPTAINERENV_*`` / ``SINGULARITYENV_*`` entries inside the
    container.  Both representations must resolve to the same policy key.
    """
    current = frozenset(name.upper() for name in _build_provider_env_blocklist())
    return _credential_target_env_name(key).upper() in current

# Active-virtualenv markers that must NOT leak into terminal subprocesses.
# The gateway runs inside its own venv, so its process environment carries
# VIRTUAL_ENV (and possibly CONDA_PREFIX). If those leak into commands the
# agent runs against OTHER Python projects, tools like ``uv``/``poetry`` treat
# the inherited value as the active environment and build/sync that other
# project's dependencies into the Hermes venv path instead of the project's own
# ``.venv`` — silently clobbering the Hermes environment (e.g. a project pinned
# to a different Python version overwrites it and breaks the gateway). The
# Hermes venv stays reachable via PATH (its bin dir is first), so stripping
# these markers is safe and only prevents the cross-project clobber (#23473).
#
# PYTHONHOME is included because a gateway-inherited value redirects the
# standard-library search of ANY child interpreter — including unrelated
# system/venv Pythons — to the Hermes venv's stdlib, which crashes with
# version-mismatch errors before a child script even imports a package
# (#75018). Hermes itself treats PYTHONHOME as contamination in its own
# child processes (managed_uv.py, sqlite_runtime.py), so stripping it from
# subprocess envs is consistent. Users who need PYTHONHOME for a specific
# child can set it explicitly in the command.
#
# PYTHONPATH is NOT included here — it's handled by
# _strip_hermes_owned_pythonpath() which removes only Hermes-owned entries,
# preserving user-set paths.
_ACTIVE_VENV_MARKER_VARS = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME")


def _is_hermes_internal_secret(key: str) -> bool:
    """Return True for Hermes-internal secrets injected under *dynamic* names.

    ``_HERMES_PROVIDER_ENV_BLOCKLIST`` is name-based and derived from the
    provider/tool registries, but the gateway and CLI also inject secrets into
    ``os.environ`` at runtime under names no static registry knows about:

    - ``AUXILIARY_<TASK>_API_KEY`` / ``AUXILIARY_<TASK>_BASE_URL`` — per-task
      side-LLM credentials bridged from ``config.yaml[auxiliary]`` by
      ``gateway/run.py`` and ``cli.py`` (vision, web_extract, approval,
      compression, and any plugin-registered auxiliary task). These are
      separate, often higher-spend API keys plus base URLs that may point at
      private endpoints; a model-authored shell command must never see them.
    - ``GATEWAY_RELAY_*_SECRET`` / ``GATEWAY_RELAY_*_KEY`` /
      ``GATEWAY_RELAY_*_TOKEN`` — relay-auth material provisioned by the
      gateway (``GATEWAY_RELAY_SECRET``, ``GATEWAY_RELAY_DELIVERY_KEY``).
      These are Tier-1 gateway secrets, like the messaging bot tokens in
      ``_ALWAYS_STRIP_KEYS``. Non-secret ``GATEWAY_RELAY_*`` routing hints
      (``GATEWAY_RELAY_URL``, ``GATEWAY_RELAY_PLATFORMS``, …) are NOT matched
      and remain visible.
    - ``BWS_ACCESS_TOKEN`` — the Bitwarden Secrets Manager bootstrap token,
      under the **exact** name configured via ``secrets.bitwarden.access_token_env``
      (default ``BWS_ACCESS_TOKEN``; may be remapped to any name, e.g.
      ``MY_BWS_TOKEN``). Hermes's own vault credential; no spawned child
      legitimately needs it. The one child that does — the ``bws`` CLI —
      receives it explicitly via ``build_subprocess_env(scrub_secrets=False)``
      in ``agent/secret_sources/bitwarden.py``, never through inheritance.
      Only the exact configured name is matched (not a ``*_ACCESS_TOKEN``
      suffix) so legitimate third-party access tokens stay
      ``env_passthrough``-registerable — see ``tools/env_passthrough.py``.

    ``code_execution_tool.py`` already catches these via substring matching on
    ``KEY`` / ``SECRET`` / ``TOKEN``; the terminal backend's narrower name-based
    blocklist did not, which is the leak this predicate closes.

    This is the single source of truth for "Hermes-internal dynamic secret"
    across every spawn path — the terminal ``_make_run_env`` /
    ``_sanitize_subprocess_env`` filters, the Docker passthrough filter, and the
    non-terminal :func:`hermes_subprocess_env` helper all call it, so the
    dynamic patterns are stripped **unconditionally** regardless of
    ``env_passthrough`` skill registration or ``inherit_credentials``. Nothing
    a model-driving CLI legitimately needs matches these patterns.
    """
    upper = _credential_target_env_name(key).upper()
    if upper.startswith("AUXILIARY_") and (
        upper.endswith("_API_KEY") or upper.endswith("_BASE_URL")
    ):
        return True
    if upper.startswith("GATEWAY_RELAY_") and (
        upper.endswith("_SECRET") or upper.endswith("_KEY") or upper.endswith("_TOKEN")
    ):
        return True
    if upper in {"OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_TOKEN"}:
        return True
    if upper.startswith("OP_SESSION_"):
        return True
    if "BWS" in upper and upper.endswith("_TOKEN"):
        return True
    if upper == "BWS_ACCESS_TOKEN" or upper == _get_configured_bws_token_env().upper():
        # Bitwarden Secrets Manager bootstrap token — the exact configured
        # access_token_env name (default BWS_ACCESS_TOKEN; may be remapped to
        # a non-suffix name like MY_BWS_TOKEN), plus the default name itself.
        # A remapped profile sharing one process with a default profile must
        # not let the default profile's BWS_ACCESS_TOKEN (which the shared
        # os.environ carries across profile turns) cross its child boundary
        # either — the Bitwarden rule holds in both directions.
        return True
    return False


def _get_configured_bws_token_env() -> str:
    """Resolve the exact Bitwarden token env name for the active profile.

    ``read_raw_config`` is already cached by profile-aware config path and file
    revision. Adding another per-home cache here would hide runtime remaps and
    leave the newly configured bootstrap token unclassified.
    """
    name = "BWS_ACCESS_TOKEN"
    try:
        from hermes_cli.config import cfg_get, read_raw_config

        configured = cfg_get(
            read_raw_config(), "secrets", "bitwarden", "access_token_env"
        )
        if isinstance(configured, str) and configured.strip():
            name = configured.strip()
    except Exception as exc:
        # A remapped bootstrap name may have no credential-looking suffix. If
        # config authority is unavailable, returning the default would let that
        # arbitrary name cross. Refuse the child decision instead of widening.
        raise RuntimeError("Bitwarden token policy unavailable") from exc
    return name


def _plugin_terminal_env_strip_keys() -> frozenset:
    """Credential env keys owned by plugin-registered terminal backends."""
    try:
        from agent.terminal_env_registry import plugin_strip_env_keys

        return plugin_strip_env_keys()
    except Exception as exc:
        # An unavailable plugin registry is not an empty deny set. Treat it as
        # degraded policy and refuse the child boundary.
        raise RuntimeError("plugin terminal environment policy unavailable") from exc


def _is_credential_shaped_password(key: str) -> bool:
    """True for password-class env names.

    Matches password-shaped names plus bare PASSWORD and *_PWD variants,
    excluding PWD itself because it is the shell working-directory variable.
    """
    upper = _credential_target_env_name(key).upper()
    return "PASSWORD" in upper or (upper.endswith("_PWD") and upper != "PWD")


def _finalize_child_env_policy(
    env: dict[str, str],
    is_passthrough,
    explicit_force_targets=(),
    *,
    enforce_password_policy: bool = False,
) -> dict[str, str]:
    """Reapply generic policy after profile values are overlaid.

    Profile provenance may replace a source-owned value with a target-owned
    value. That replacement must not bypass generic credential filtering.
    Explicit force-prefix targets remain the existing opt-in escape for
    non-internal credentials, while internal secrets stay denied.
    """
    plugin_strip = {
        _credential_target_env_name(name).upper()
        for name in _plugin_terminal_env_strip_keys()
    }
    always_strip = {name.upper() for name in _ALWAYS_STRIP_KEYS}
    force_targets = {str(name).upper() for name in explicit_force_targets}
    for key in list(env):
        target_key = _credential_target_env_name(key)
        target_upper = target_key.upper()
        allow_credential = (
            target_upper in force_targets or is_passthrough(target_key)
        )
        if key.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            env.pop(key, None)
        elif _is_hermes_internal_secret(target_key):
            env.pop(key, None)
        elif target_upper in always_strip:
            env.pop(key, None)
        elif target_upper in plugin_strip:
            env.pop(key, None)
        elif _is_blocked_provider_env(target_key) and not allow_credential:
            env.pop(key, None)
        elif (
            enforce_password_policy
            and _is_credential_shaped_password(target_key)
            and not allow_credential
        ):
            env.pop(key, None)
    return env


def _inject_context_hermes_home(env: dict) -> None:
    """Bridge the context-local Hermes home override into subprocess env."""
    try:
        from hermes_constants import get_hermes_home_override

        value = get_hermes_home_override()
        if value:
            env["HERMES_HOME"] = value
    except Exception:
        pass
    apply_subprocess_home_env(env)


def _inject_session_context_env(env: dict) -> None:
    """Bridge gateway session ContextVars (HERMES_SESSION_*) into a child env.
    Cross-session leak guard: the vars' last-writer-wins ``os.environ`` mirror may
    belong to another turn on a concurrent multi-session host, so once the session
    context is engaged ContextVars are authoritative — a bound value (incl. "") wins
    and an _UNSET var is STRIPPED, not inherited. An unengaged CLI keeps the mirror."""
    try:
        from gateway.session_context import _UNSET, _VAR_MAP, session_context_engaged
    except Exception:
        return
    _engaged = session_context_engaged()
    for var_name, var in _VAR_MAP.items():
        value = var.get()
        if value is not _UNSET:
            env[var_name] = "" if value is None else str(value)
        elif _engaged:
            env.pop(var_name, None)


def _sanitize_subprocess_env(
    base_env: dict | None,
    extra_env: dict | None = None,
    *,
    profile_home: str | os.PathLike | None = None,
    source_profile_home: str | os.PathLike | None = None,
    enforce_profile_boundary: bool = False,
) -> dict:
    """Filter Hermes-managed and cross-profile secrets from a child environment.

    In multiplex mode the exact source-profile ownership boundary is applied
    before the existing provider/blocklist policy. Explicit homes let
    standalone workers such as Kanban enforce the same rule without gateway
    process-global state.
    """
    protected_base = dict(base_env or {})
    boundary = None
    boundary_active = enforce_profile_boundary
    cross_profile = False
    try:
        from agent.secret_scope import (
            build_profile_env_boundary,
            is_multiplex_active,
        )

        boundary_active = boundary_active or is_multiplex_active()
        if boundary_active:
            boundary = build_profile_env_boundary(
                source_home=source_profile_home,
                target_home=profile_home,
            )
            cross_profile = boundary.source_home != boundary.target_home
            protected_base = boundary.sanitize(protected_base)
    except Exception as exc:
        if enforce_profile_boundary or boundary_active:
            raise RuntimeError(
                "profile environment boundary could not be constructed; refusing "
                f"to spawn with ambient environment: {exc}"
            ) from exc
        logger.debug("profile environment boundary unavailable outside multiplex", exc_info=True)

    try:
        from tools.env_passthrough import (
            is_env_passthrough as _is_passthrough_for_profile,
            resolve_passthrough_value as _resolve_passthrough_value,
        )
        _is_passthrough = lambda name: _is_passthrough_for_profile(  # noqa: E731
            name,
            profile_home=profile_home,
        )
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731
        _resolve_passthrough_value = lambda _name, fallback: fallback  # noqa: E731

    sanitized: dict[str, str] = {}
    _plugin_strip = _plugin_terminal_env_strip_keys()

    for key, value in protected_base.items():
        if key.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            continue
        if _is_hermes_internal_secret(key):
            continue
        if key in _plugin_strip:
            continue
        passthrough = _is_passthrough(key)
        if _is_blocked_provider_env(key) and not passthrough:
            continue
        if cross_profile and _is_credential_shaped_password(key) and not passthrough:
            continue
        resolved = _resolve_passthrough_value(key, value) if passthrough else value
        if resolved is not None:
            sanitized[key] = resolved

    for key, value in (extra_env or {}).items():
        if key.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            real_key = key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            if _is_hermes_internal_secret(real_key):
                continue
            key = key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            if not _is_hermes_internal_secret(key):
                out[key] = value
            continue
        if _is_hermes_internal_secret(key) or key in plugin_strip:
            continue
        else:
            passthrough = _is_passthrough(key)
            if _is_blocked_provider_env(key) and not passthrough:
                continue
            if cross_profile and _is_credential_shaped_password(key) and not passthrough:
                continue
            resolved = _resolve_passthrough_value(key, value) if passthrough else value
            if resolved is not None:
                sanitized[key] = resolved

    # Apply the profile boundary after all explicit force-prefix values have
    # been unwrapped. This prevents ``extra_env`` from reintroducing a
    # source-owned credential after the initial protected-base sanitization.
    if boundary_active and boundary is not None:
        sanitized = boundary.sanitize(sanitized)
        sanitized = _materialize_target_passthrough_values(
            sanitized,
            boundary,
            is_passthrough=_is_passthrough,
            plugin_strip=_plugin_strip,
        )
    sanitized = _finalize_child_env_policy(
        sanitized,
        _is_passthrough,
        {
            key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            for key in (extra_env or {})
            if key.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX)
        },
        enforce_password_policy=cross_profile,
    )

    # An explicit target profile is authoritative for both HERMES_HOME and the
    # derived subprocess HOME policy.  Install it before evaluating
    # apply_subprocess_home_env(); otherwise standalone workers can get split
    # identity (target HERMES_HOME with the dispatcher's HOME).
    if profile_home is not None:
        sanitized["HERMES_HOME"] = str(profile_home)
    else:
        _inject_context_hermes_home(sanitized)

def _finalize_child_env(env: dict) -> dict:
    """Guards shared by every spawn surface: profile-home propagation, session-context
    bridging, Hermes-owned PYTHONPATH + venv-marker strip, MSYS defaults, delegate_task
    Kanban scrub. Returns the (possibly new) dict."""
    _apply_profile_home(env)
    _inject_session_context_env(env)
    _strip_hermes_owned_pythonpath_and_runtime_markers(env)
    _apply_windows_msys_bash_env_defaults(env)
    from agent.delegation_context import delegated_child_subprocess_env
    return delegated_child_subprocess_env(env)


def _scrubbed_env(parts, plugin_strip: frozenset, fix_path) -> dict:
    """Filter each ``(items, unwrap_force)`` in *parts* into one env, rewrite PATH via
    *fix_path* (always prepending the hermes install dir so bare ``hermes`` resolves
    for children of a systemd/cron-launched gateway), then apply the shared guards."""
    out: dict[str, str] = {}
    for items, unwrap_force in parts:
        _filter_secret_env(items, out, unwrap_force=unwrap_force, plugin_strip=plugin_strip)
    path_key = _path_env_key(out)
    # Keep bare ``hermes`` invocations available to child jobs even when the gateway was launched by a
    # service manager or cron without the console script's directory on PATH. The terminal environment
    # already applies this invariant; Cron scripts use this sanitizer directly (#92998).
    if path_key is not None:
        out[path_key] = _prepend_hermes_bin_dir(fix_path(out.get(path_key, "")))
    return _finalize_child_env(out)


def _materialize_target_passthrough_values(
    env: dict[str, str],
    boundary: "ProfileEnvBoundary",
    *,
    is_passthrough,
    plugin_strip: frozenset[str] | set[str],
) -> dict[str, str]:
    """Add exact target-authored passthrough grants absent from ambient env.

    The profile boundary is the value authority and the target profile's
    passthrough configuration is the policy authority.  Keeping this step
    shared prevents foreground local execution and process-registry execution
    from disagreeing about target-only grants.
    """
    result = dict(env)
    if boundary.source_home == boundary.target_home:
        return result
    for key, value in boundary.compiled_target_values().items():
        if not is_passthrough(key):
            continue
        if _is_hermes_internal_secret(key) or key in plugin_strip:
            continue
        if _is_blocked_provider_env(key):
            continue
        # ``boundary.target_values`` is already an immutable, identity-bound
        # target-profile snapshot. Re-resolving it through the ambient
        # ContextVar would make a valid explicit boundary depend on caller
        # thread context and can drop target-only grants on recovery threads.
        result[key] = str(value)
    return result


def _scrub_delegated_child_kanban_env(env: dict[str, str]) -> dict[str, str]:
    """Strip dispatcher-owned Kanban env from delegate_task child subprocesses."""
    try:
        from agent.delegation_context import (
            is_delegated_child_process_context,
            scrub_kanban_env,
        )

        if is_delegated_child_process_context():
            return scrub_kanban_env(env)
    except Exception:
        pass
    return env


# Tier-1 secrets: stripped from EVERY spawned subprocess unconditionally —
# even when the caller opts into credential inheritance for a model-driving
# CLI (claude / codex / gemini).  These are not LLM provider credentials; no
# legitimate child Hermes spawns needs them, and they are the highest-value
# secrets to keep out of a compromised dependency's reach (gateway bot tokens,
# GitHub auth, remote-compute tokens, dashboard session secret).  The set is a
# narrow subset of _HERMES_PROVIDER_ENV_BLOCKLIST; provider keys are handled by
# the conditional Tier-2 strip in hermes_subprocess_env().
_ALWAYS_STRIP_KEYS: frozenset[str] = frozenset({
    # GitHub auth
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    # Gateway / messaging bot tokens and access control
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    # Gateway relay auth — the ID/secret/delivery-key triplet the gateway
    # provisions and persists to the 0600 .env. Stripped unconditionally on
    # EVERY spawn surface (terminal + model-driving CLIs) so it can't drift
    # between paths: _SECRET / _DELIVERY_KEY are also matched by
    # _is_hermes_internal_secret, but _ID has no secret suffix, so it must be
    # enumerated here to stay stripped on the inherit_credentials=True path
    # (codex / copilot), which skips the Tier-2 blocklist.
    "GATEWAY_RELAY_ID",
    "GATEWAY_RELAY_SECRET",
    "GATEWAY_RELAY_DELIVERY_KEY",
    "HASS_TOKEN",
    "EMAIL_PASSWORD",
    "HERMES_DASHBOARD_SESSION_TOKEN",
    # Bitwarden Secrets Manager bootstrap token.  Classified as a
    # Hermes-internal secret by _is_hermes_internal_secret on the terminal
    # path; enumerated here so the non-terminal inherit_credentials=True
    # path (codex / copilot / TUI host) also strips it unconditionally.
    # The bws secret-source child injects its token explicitly into its own
    # child env (agent/secret_sources/bitwarden.py) and never relies on
    # ambient inheritance, so Tier-1 stripping cannot break it.
    "BWS_ACCESS_TOKEN",
    # Remote-compute / infrastructure secrets
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
})


def hermes_subprocess_env(
    *,
    inherit_credentials: bool = False,
    profile_boundary: "ProfileEnvBoundary | None" = None,
) -> dict[str, str]:
    """Build a sanitized environment dict for a spawned subprocess.

    Centralized helper for the **non-terminal** spawn surface (browser,
    ACP/CLI executors, computer-use driver, dep-ensure, TUI Node host,
    detached gateway).  Use this instead of copying ``os.environ`` directly
    so strip-by-default is the uniform policy across every spawn site, with a
    single source of truth (``_HERMES_PROVIDER_ENV_BLOCKLIST``).  The terminal
    / execute_code path keeps using :func:`_sanitize_subprocess_env`, which is
    skill-aware (``env_passthrough``); this helper is for spawns that have no
    skill-passthrough concept.

    Two-tier stripping:

    * **Tier 1 (always):** ``_ALWAYS_STRIP_KEYS`` — gateway bot tokens, GitHub
      auth, and remote-compute secrets are removed regardless of
      ``inherit_credentials``.  No child Hermes spawns legitimately needs them.
    * **Tier 2 (conditional):** the rest of ``_HERMES_PROVIDER_ENV_BLOCKLIST``
      (LLM provider API keys, tool secrets) is removed unless the caller passes
      ``inherit_credentials=True``.

    Pass ``inherit_credentials=True`` **only** when the child legitimately
    needs LLM provider credentials — a user-blessed ``claude`` / ``codex`` /
    ``gemini`` CLI executor, or the TUI Node host that makes model calls.  The
    flag is grep-able for audit: ``grep -rn 'inherit_credentials=True'`` lists
    every spawn site that still receives provider credentials.

    Callers that need a *specific* non-provider secret (e.g. the browser worker
    needs ``BROWSERBASE_API_KEY`` / ``FIRECRAWL_API_KEY``) should call with
    ``inherit_credentials=False`` and copy just those keys back from
    ``os.environ`` into the returned dict.
    """
    env = os.environ.copy()

    from agent.secret_scope import build_profile_env_boundary, is_multiplex_active

    multiplex_active = is_multiplex_active()
    boundary = profile_boundary
    boundary_active = multiplex_active or boundary is not None
    if boundary_active:
        boundary = boundary or build_profile_env_boundary()
        env = boundary.sanitize(env)
        if inherit_credentials:
            for key, value in boundary.compiled_target_values().items():
                target_name = _credential_target_env_name(key).upper()
                if target_name in _build_model_provider_env_names():
                    env[str(key)] = str(value)

    # Tier 1 — always strip, including mixed-case Windows keys and
    # Apptainer/Singularity forwarding wrappers around a Tier-1 target.
    for key in list(env):
        if _credential_target_env_name(key).upper() in _ALWAYS_STRIP_KEYS:
            env.pop(key, None)
    plugin_strip = {
        _credential_target_env_name(name).upper()
        for name in _plugin_terminal_env_strip_keys()
    }
    for key in list(env):
        if _credential_target_env_name(key).upper() in plugin_strip:
            env.pop(key, None)
    # Password-shaped variables are provenance-isolated only when crossing
    # profile authority. Single-profile helpers retain the trusted shell contract.
    cross_profile = bool(
        boundary is not None and boundary.source_home != boundary.target_home
    )
    if cross_profile:
        for key in list(env):
            if _is_credential_shaped_password(key):
                env.pop(key, None)
    # Internal routing hints and Hermes-internal dynamic secrets
    # (``AUXILIARY_<TASK>_API_KEY`` / ``_BASE_URL`` side-LLM credentials,
    # ``GATEWAY_RELAY_*`` relay-auth material) must never reach a child,
    # regardless of ``inherit_credentials`` — a model-driving CLI has no
    # legitimate use for them. See :func:`_is_hermes_internal_secret`.
    for key in list(env):
        if key.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            env.pop(key, None)
        elif _is_hermes_internal_secret(key):
            env.pop(key, None)

    if not inherit_credentials:
        # Tier 2 — strip provider/tool credentials unless explicitly inherited.
        for key in list(env):
            if _is_blocked_provider_env(key):
                env.pop(key, None)

    # Windows UTF-8 safety for spawned processes (#31420).
    env.setdefault("PYTHONUTF8", "1")

    if boundary is not None:
        env["HERMES_HOME"] = str(boundary.target_home)
    else:
        _inject_context_hermes_home(env)
    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)

    _strip_hermes_owned_pythonpath_and_runtime_markers(env)

    _apply_windows_msys_bash_env_defaults(env)

    # Cross-session leak guard, same as the terminal spawn paths: this helper
    # copies os.environ, whose HERMES_SESSION_* mirror is a last-writer-wins
    # global under a concurrent multi-session host. A caller that re-binds the
    # session identity explicitly (slash_worker/ACP via --session-key argv) is
    # unaffected — bound ContextVars win here — but a caller that spawns without
    # re-binding (e.g. tui_gateway cli.exec) would otherwise inherit a FOREIGN
    # session's identity. Strip _UNSET session vars when engaged so that can't
    # happen; single uniform policy across every spawn surface.
    _inject_session_context_env(env)

    # Non-terminal subprocess helpers (browser, lazy-deps, TUI/ACP hosts, etc.)
    # also need the delegate_task child lineage marker.  Otherwise a child
    # context that later imports Kanban DB code in the spawned process would
    # still see the parent's HERMES_HOME but lose the DB mutation guard.
    env = _scrub_delegated_child_kanban_env(env)

    return env


def build_subprocess_env(
    base: "Mapping[str, str] | None" = None,
    *,
    inherit_profile_home: bool = True,
    scrub_secrets: bool = True,
    extra: "Mapping[str, str] | None" = None,
    profile_home: str | os.PathLike | None = None,
    source_profile_home: str | os.PathLike | None = None,
    enforce_profile_boundary: bool = False,
) -> dict[str, str]:
    """Single factory for building a child-process environment.

    Every spawn site in the codebase should build its env through this
    function (or :func:`hermes_subprocess_env` for the model-driving-CLI
    surface) instead of copying ``os.environ`` directly, so profile-home
    propagation (``HERMES_HOME`` / subprocess ``HOME`` contract) and the
    Hermes secret-scrub policy have a single owner.  History: ~11 separate
    commits each fixed one more spawn site that missed profile-HOME or
    secret-scrub propagation; this factory is the fix for the class.

    Parameters:

    * ``base`` — starting environment.  ``None`` (default) snapshots
      ``os.environ``.  Pass an explicit mapping to build on a caller-prepared
      env instead.
    * ``scrub_secrets=True`` (default) — delegate to
      :func:`_sanitize_subprocess_env`, the long-standing owner of the scrub
      list (provider blocklist + ``_is_hermes_internal_secret`` dynamic
      patterns + kanban/venv-marker/session-context guards) **and** of
      ``HERMES_HOME`` / subprocess-HOME propagation.  On this path profile
      home propagation is inherent — ``inherit_profile_home`` is ignored
      (always applied), exactly matching today's sanitize semantics.
    * ``scrub_secrets=False`` — preserve the base env content byte-for-byte
      (no key is removed).  Use for children that intentionally receive
      secrets (git credential flows, ``bws``/``op`` secret CLIs) or where
      scrubbing could change behavior.  The site is still a win: it becomes
      grep-able and future-fixable.
    * ``inherit_profile_home`` — on the non-scrub path, when True, bridge the
      context-local Hermes home override into ``HERMES_HOME`` and apply the
      subprocess HOME contract (``hermes_constants.apply_subprocess_home_env``).
      Pass False to keep the inherited env untouched (exact legacy
      ``os.environ.copy()`` behavior).
    * ``extra`` — applied **last** on the non-scrub path so explicit caller
      overrides (e.g. a session-scoped ``HERMES_HOME``) always win.  On the
      scrub path it is forwarded as ``_sanitize_subprocess_env``'s
      ``extra_env`` (same force-prefix / blocklist handling as today).
    * ``profile_home`` / ``source_profile_home`` — optional explicit target
      and source profile homes.  When supplied with
      ``enforce_profile_boundary=True`` they make the profile ownership policy
      usable by standalone workers such as Kanban.
    """
    if scrub_secrets:
        # _sanitize_subprocess_env already performs HERMES_HOME override
        # bridging + apply_subprocess_home_env unconditionally; delegating
        # wholesale keeps one owner and zero drift.
        return _sanitize_subprocess_env(
            dict(base) if base is not None else os.environ.copy(),
            dict(extra) if extra else None,
            profile_home=profile_home,
            source_profile_home=source_profile_home,
            enforce_profile_boundary=enforce_profile_boundary,
        )

    env: dict[str, str] = dict(base) if base is not None else os.environ.copy()
    if scrub_secrets:
        return _sanitize_subprocess_env(env, dict(extra) if extra else None)
    if inherit_profile_home:
        _apply_profile_home(env)
    if extra:
        env.update(extra)
    from agent.delegation_context import delegated_child_subprocess_env
    return delegated_child_subprocess_env(env)


# --- Shell discovery ---
def _windows_bash_candidates(custom: "str | None") -> list[str]:
    """Ordered bash.exe candidates on Windows: HERMES_GIT_BASH_PATH, our portable Git
    under %LOCALAPPDATA%\\hermes\\git (PortableGit ``bin`` and MinGit ``usr\\bin``),
    known Git-for-Windows dirs, then PATH last — ``shutil.which`` may return WSL's
    bash, which fails silently on Windows paths."""
    getenv = os.environ.get
    lad = getenv("LOCALAPPDATA", "")
    roots = [
        lad and os.path.join(lad, "hermes", "git", "bin"),
        lad and os.path.join(lad, "hermes", "git", "usr", "bin"),
        os.path.join(getenv("ProgramFiles", r"C:\Program Files"), "Git", "bin"),
        os.path.join(getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin"),
        lad and os.path.join(lad, "Programs", "Git", "bin"),
    ]
    raw = [custom or "", *(os.path.join(r, "bash.exe") for r in roots if r)]
    candidates = list(dict.fromkeys(c for c in raw if c and os.path.isfile(c)))
    found = shutil.which("bash")
    if found and found not in candidates:
        candidates.append(found)
    return candidates


def _find_bash() -> str:
    """Find bash for command execution."""
    if not _IS_WINDOWS:
        return (shutil.which("bash")
                or next((p for p in ("/usr/bin/bash", "/bin/bash") if os.path.isfile(p)), None)
                or os.environ.get("SHELL") or "/bin/sh")
    custom = os.environ.get("HERMES_GIT_BASH_PATH")
    candidates = _windows_bash_candidates(custom)
    # First candidate that can actually start wins: a stale HERMES_GIT_BASH_PATH
    # pointing at a broken install must not beat a healthy portable Git.
    for candidate in candidates:
        if _bash_starts(candidate):
            if candidate != custom and custom and os.path.isfile(custom):
                logger.warning(
                    "HERMES_GIT_BASH_PATH=%s fails to start; using %s instead", custom, candidate)
            return candidate
    if candidates:
        probe_details = "\n".join(
            detail for c in candidates if (detail := _bash_probe_details_cache.get(c)))
        if _mandatory_aslr_enabled() is True or _looks_like_msys_spawn_failure(probe_details):
            raise RuntimeError(_git_bash_aslr_help(candidates[0], probe_details))
        # Unknown failure class: return the first path so the caller sees the
        # real bash error instead of a less useful "not found".
        return candidates[0]
    raise RuntimeError(
        "Git Bash not found. Hermes Agent requires Git for Windows on Windows.\n"
        "Install it from: https://git-scm.com/download/win\n"
        "Or set HERMES_GIT_BASH_PATH to your bash.exe location.")


_git_bash_bin_dirs_cache: "list[str] | None" = None


def _git_bash_bin_dirs() -> list[str]:
    """Git Bash's coreutils dirs in ``/etc/profile`` order (mingw first so coreutils
    beat System32 lookalikes); ``[]`` off Windows. A non-login ``bash -c`` (fallback
    when ``bash -l`` is broken) never sources ``/etc/profile``, so without these
    ``cat``/``mktemp``/``mv`` are missing and commands exit 127."""
    global _git_bash_bin_dirs_cache
    if _git_bash_bin_dirs_cache is None:
        _git_bash_bin_dirs_cache = _compute_git_bash_bin_dirs() if _IS_WINDOWS else []
    return _git_bash_bin_dirs_cache


def _compute_git_bash_bin_dirs() -> list[str]:
    try:
        bash = _find_bash()
    except Exception:
        return []
    parent = os.path.dirname(os.path.dirname(bash))  # bash in <root>\bin or <root>\usr\bin (MinGit)
    root = os.path.dirname(parent) if os.path.basename(parent).lower() == "usr" else parent
    subs = ("mingw64/bin", "mingw32/bin", "usr/local/bin", "usr/bin", "bin")
    dirs = (os.path.join(root, *sub.split("/")) for sub in subs)
    return list(dict.fromkeys(d for d in dirs if os.path.isdir(d)))


def _prepend_missing_path_entries(existing_path: str, dirs: list[str]) -> str:
    """Prepend *dirs* missing from *existing_path* (``os.pathsep``); an already-listed
    dir keeps its position; unchanged input when nothing is missing."""
    entries = [e for e in existing_path.split(os.pathsep) if e]
    missing = [d for d in dirs if d not in entries]
    return os.pathsep.join([*missing, *entries]) if missing else existing_path


def _prepend_git_bash_dirs(existing_path: str) -> str:
    """Prepend Git Bash's binary dirs if missing (no-op off Windows), so the
    non-login ``bash -c`` fallback can find coreutils."""
    return _prepend_missing_path_entries(existing_path, _git_bash_bin_dirs())


# POSIX-sh-family shells that understand spawn_local's ``[shell, "-lic", "set +m; …"]``
# invocation; fish, csh/tcsh, nushell, elvish, xonsh would error, so _find_shell
# falls back to bash for them.
# (#42203)
_SPAWN_COMPATIBLE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh", "mksh"})


def _find_shell() -> str:
    """User's login shell for background spawning: ``$SHELL`` on POSIX when it is an
    executable sh-family shell, else ``_find_bash``. macOS's system bash 3.2 under
    ``-l`` with stdin ``/dev/null`` sources ``~/.bash_profile``, which often
    ``exec /bin/zsh -l`` and drops ``-c`` — the command silently never runs."""
    user_shell = "" if _IS_WINDOWS else os.environ.get("SHELL")
    if (user_shell and os.path.isfile(user_shell) and os.access(user_shell, os.X_OK)
            and Path(user_shell).name in _SPAWN_COMPATIBLE_SHELLS):
        return user_shell
    return _find_bash()


# --- PATH completion for the terminal subshell ---

# Standard PATH entries for environments with minimal PATH.
_SANE_PATH = ("/opt/homebrew/bin:/opt/homebrew/sbin:"
              "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

# Cached directory containing the ``hermes`` console-script.
# ``_SENTINEL`` distinguishes "not resolved yet" from a resolved ``None``.
_SENTINEL = object()
_HERMES_BIN_DIR: "str | None | object" = _SENTINEL


def _resolve_hermes_bin_dir() -> str | None:
    """Directory holding the ``hermes`` console-script, or None (cached). A gateway
    launched by systemd/cron/a desktop launcher lacks the install dir on PATH and bare
    ``hermes`` exits 127. Order: ``which``; absolute ``sys.argv[0]`` naming a real
    hermes executable; ``sys.executable``'s dir if it holds the shim."""
    global _HERMES_BIN_DIR
    if _HERMES_BIN_DIR is not _SENTINEL:
        return _HERMES_BIN_DIR  # type: ignore[return-value]
    which = shutil.which("hermes")
    argv0 = sys.argv[0] if sys.argv else ""
    base = os.path.basename(argv0).lower()
    exe_dir = os.path.dirname(sys.executable) if sys.executable else ""
    shim = "hermes.exe" if _IS_WINDOWS else "hermes"
    if which:
        candidate = os.path.dirname(which)
    elif (os.path.isabs(argv0) and (base == "hermes" or base.startswith("hermes."))
            and os.path.isfile(argv0)):
        candidate = os.path.dirname(argv0)
    else:
        candidate = exe_dir if exe_dir and os.path.isfile(os.path.join(exe_dir, shim)) else None
    _HERMES_BIN_DIR = candidate if candidate and os.path.isdir(candidate) else None
    return _HERMES_BIN_DIR


def _prepend_hermes_bin_dir(existing_path: str) -> str:
    """Prepend the hermes install dir to ``existing_path`` if missing."""
    bin_dir = _resolve_hermes_bin_dir()
    return _prepend_missing_path_entries(existing_path, [bin_dir] if bin_dir else [])


def _managed_runtime_path_entries() -> list[str]:
    """Existing Hermes-managed runtime dirs: ``$HERMES_HOME/node`` (+``/bin``) and
    ``$HERMES_HOME/bin`` (managed ``uv``). Per call, not cached: home is
    profile-scoped and a managed tree can appear mid-process."""
    try:
        from hermes_constants import get_hermes_home, iter_hermes_node_dirs
        return [str(d) for d in (*iter_hermes_node_dirs(), get_hermes_home() / "bin") if d.is_dir()]
    except Exception:
        return []


def _append_missing_sane_path_entries(existing_path: str) -> str:
    """Normalised POSIX PATH with missing sane entries appended: empty entries
    dropped (shells read them as cwd), duplicates collapsed (first wins), then
    missing ``_SANE_PATH`` / managed-runtime dirs appended so user entries keep
    precedence. Windows is a no-op passthrough (native ``;`` PATH untouched)."""
    if _IS_WINDOWS:
        return existing_path
    # dict preserves first-occurrence order; empty entries dropped.
    ordered = dict.fromkeys(entry for entry in existing_path.split(":") if entry)
    ordered.update(dict.fromkeys([*_SANE_PATH.split(":"), *_managed_runtime_path_entries()]))
    return ":".join(ordered)


def _apply_windows_msys_bash_env_defaults(env: dict) -> None:
    """Disable MSYS argument path conversion (``/FO`` -> ``C:/.../git/FO`` breaks
    tasklist/schtasks/wmic/``cmd /c``). Git for Windows honors ``MSYS_NO_PATHCONV``;
    MSYS2/Cygwin bash honor ``MSYS2_ARG_CONV_EXCL`` — set both; users can override.

    Git Bash rewrites arguments that look like Unix paths (``/FO``, ``/TN``, ``/Create``) into
    ``C:/.../git/FO``-style paths, which breaks native Windows commands such as ``tasklist``, ``schtasks``,
    and ``wmic``. Hermes runs terminal commands through bash on Windows, so set the standard MSYS opt-out by
    default. Refs #56700.
    MSYS2-proper and Cygwin bash (which ``_find_bash`` can still return via the final ``shutil.which``
    fallback) ignore it and honor ``MSYS2_ARG_CONV_EXCL`` instead, so set both. ``*`` disables all argv
    conversion — the semantic equivalent of ``MSYS_NO_PATHCONV=1``. Also fixes ``cmd /c`` mangling (#56147).
    """
    if _IS_WINDOWS:
        env.setdefault("MSYS_NO_PATHCONV", "1")
        env.setdefault("MSYS2_ARG_CONV_EXCL", "*")


def _path_env_key(run_env: dict) -> str | None:
    """PATH env key to update without altering Windows casing (``Path`` vs ``PATH``);
    None when a Windows env has no PATH key at all."""
    return next((k for k in run_env if k.upper() == "PATH"), None) if _IS_WINDOWS else "PATH"


def _make_run_env(env: dict) -> dict:
    """Build a run environment with a sane PATH and provider-var stripping."""
    try:
        from tools.env_passthrough import (
            is_env_passthrough as _is_passthrough,
            resolve_passthrough_value as _resolve_passthrough_value,
        )
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731
        _resolve_passthrough_value = lambda _name, fallback: fallback  # noqa: E731

    merged = dict(os.environ | env)
    _multiplex_active = False
    _boundary = None
    try:
        from agent.secret_scope import build_profile_env_boundary, is_multiplex_active

        _multiplex_active = is_multiplex_active()
        if _multiplex_active:
            _boundary = build_profile_env_boundary()
            merged = _boundary.sanitize(merged)
    except Exception as exc:
        if _multiplex_active:
            raise RuntimeError(
                "profile environment boundary could not be constructed; refusing "
                "to run with ambient environment"
            ) from exc
        logger.debug("profile environment boundary unavailable outside multiplex", exc_info=True)
    run_env = {}
    explicit_force_names = {
        key
        for key in env
        if isinstance(key, str) and key.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX)
    }
    for k, v in merged.items():
        if k.upper().startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            if k not in explicit_force_names:
                continue
            real_key = k[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            if _is_hermes_internal_secret(real_key):
                continue
            run_env[real_key] = v
        elif _is_hermes_internal_secret(k):
            continue
        else:
            passthrough = _is_passthrough(k)
            if _is_blocked_provider_env(k) and not passthrough:
                continue
            if _multiplex_active and _is_credential_shaped_password(k) and not passthrough:
                continue
            value = _resolve_passthrough_value(k, v) if passthrough else v
            if value is not None:
                run_env[k] = value
    if _multiplex_active and _boundary is not None:
        run_env = _boundary.sanitize(run_env)
        run_env = _materialize_target_passthrough_values(
            run_env,
            _boundary,
            is_passthrough=_is_passthrough,
            plugin_strip=_plugin_terminal_env_strip_keys(),
        )
    run_env = _finalize_child_env_policy(
        run_env,
        _is_passthrough,
        {
            key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            for key in explicit_force_names
        },
        enforce_password_policy=_multiplex_active,
    )

    path_key = _path_env_key(run_env)
    if path_key is not None:
        new_path = _append_missing_sane_path_entries(run_env.get(path_key, ""))
        # On Windows, ensure Git Bash's coreutils dirs (…\usr\bin etc.) are on
        # PATH.  A non-login ``bash -c`` fallback (used when ``bash -l`` is
        # broken) never sources /etc/profile, so without this cat/mktemp/mv and
        # friends are missing and every write_file/terminal call fails (empty
        # error / exit 127).  No-op off Windows and when a login snapshot is
        # healthy (the snapshot re-exports the full PATH inside the shell).
        new_path = _prepend_git_bash_dirs(new_path)
        # Ensure the hermes install dir is reachable so plugins can shell out
        # to bare ``hermes`` via the terminal tool even when the gateway was
        # launched without it on PATH (systemd, service managers, cron, etc.).
        run_env[path_key] = _prepend_hermes_bin_dir(new_path)

    _inject_context_hermes_home(run_env)

    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(run_env)

    # Bridge ContextVar-based session vars into the subprocess env (with the
    # cross-session leak guard — strips _UNSET vars when a concurrent host is
    # engaged so a sibling session's os.environ mirror can't leak in).
    _inject_session_context_env(run_env)

    _strip_hermes_owned_pythonpath_and_runtime_markers(run_env)

    _apply_windows_msys_bash_env_defaults(run_env)

    run_env = _scrub_delegated_child_kanban_env(run_env)

    return run_env


def _same_path(left: Path, right: Path) -> bool:
    """Compare path spellings with host filesystem case semantics."""
    left_parts = [os.path.normcase(part) for part in left.parts]
    right_parts = [os.path.normcase(part) for part in right.parts]
    return left_parts == right_parts


def _build_hermes_repo_root_aliases(
    resolved_root: Path,
    lexical_root: Path,
    configured_home: Path,
) -> tuple[Path, ...]:
    """Return exact repo-root spellings emitted by Hermes launchers.

    ``gateway_windows._preserve_hermes_home_path`` maps a physical path under
    the resolved HERMES_HOME back onto the configured HERMES_HOME spelling.
    Mirror that producer contract here so a junction-backed install is matched
    without treating arbitrary descendants of HERMES_HOME as Hermes-owned.
    Additionally, when the repo itself is a junction under the configured root
    (repo-level junction, possibly cross-drive), the single deterministic
    candidate <root>/<repo dirname> is accepted only when strict resolve
    proves it is the exact physical repo root.
    """
    aliases: list[Path] = []

    def add(candidate: Path) -> None:
        if not any(_same_path(candidate, existing) for existing in aliases):
            aliases.append(candidate)

    add(resolved_root)
    add(lexical_root)

    # Profile re-home: with --profile / sticky active_profile the configured
    # home becomes <root>/profiles/<name>.  The repo root then lives beside
    # the profiles directory (not under the profile home), so the home-
    # relative mapping below cannot reach it.  Derive the root spelling
    # lexically the same way get_default_hermes_root() does (parent of a
    # "profiles" component) and run the same exact-ownership mapping against
    # it -- this recovers the launcher's lexical root under profile re-home
    # while still never matching arbitrary descendants of HERMES_HOME.
    home_candidates = [configured_home]
    if configured_home.parent.name == "profiles":
        home_candidates.append(configured_home.parent.parent)

    for home in home_candidates:
        try:
            resolved_home = home.resolve()
            home_key = os.path.normcase(str(resolved_home))
            root_key = os.path.normcase(str(resolved_root))
            if os.path.commonpath([home_key, root_key]) == home_key:
                relative_root = os.path.relpath(str(resolved_root), str(resolved_home))
                add(home / relative_root)
        except (OSError, ValueError):
            pass

    # Repo-level junction recovery: the repository itself may be a
    # junction/symlink under the configured root (e.g. D:\hermes\hermes-agent
    # -> C:\...\hermes-agent) while the import spelling (editable install)
    # resolves to the physical location.  The home-relative mapping above
    # cannot express a cross-drive link (commonpath raises on different
    # drives), so prove the EXACT filesystem identity of the single
    # deterministic candidate -- <lexical root>/<repo dirname> -- with a
    # strict resolve before accepting it as Hermes-owned.  Fail-closed: a
    # missing path (strict resolve raises), a real directory that is not the
    # known physical root, or any unrelated spelling never becomes an alias.
    for home in home_candidates:
        repo_candidate = home / resolved_root.name
        try:
            if repo_candidate.resolve(strict=True) == resolved_root.resolve(strict=True):
                add(repo_candidate)
        except OSError:
            pass

    return tuple(aliases)


# --- Hermes venv / repo-root detection (module-level, computed once) ---
# Owned here; read lazily by tools.environments.local_pythonpath (tests patch here).
# The Electron app prepends the repo root to PYTHONPATH so the backend can ``import
# tools``; other subprocesses must not inherit it. Aliases: launchers may emit other
# spellings — the Windows gateway launcher renders Hermes-owned paths under the
# configured HERMES_HOME spelling (possibly a junction to another drive).
_hermes_repo_root: Path = Path(__file__).resolve().parents[2]
_hermes_repo_root_aliases: tuple[Path, ...] = _build_hermes_repo_root_aliases(
    _hermes_repo_root, Path(__file__).absolute().parents[2], get_process_hermes_home())
_in_venv: bool = (getattr(sys, "base_prefix", sys.prefix) != sys.prefix
                  or hasattr(sys, "real_prefix"))  # real_prefix: virtualenv<20
_hermes_site_packages: list[Path] | None = None  # lazily cached by local_pythonpath


# --- Login-shell init files ---
def _read_terminal_shell_init_config() -> tuple[list[str], bool]:
    """(shell_init_files, auto_source_bashrc) from config.yaml; defaults on any
    failure so terminal execution never breaks."""
    try:
        from hermes_cli.config import load_config
        terminal_cfg = (load_config() or {}).get("terminal") or {}
        files = terminal_cfg.get("shell_init_files") or []
        if not isinstance(files, list):
            files = []
        return [str(f) for f in files if f], bool(terminal_cfg.get("auto_source_bashrc", True))
    except Exception:
        return [], True


def _resolve_shell_init_files() -> list[str]:
    """Files to source before the login-shell snapshot (``~``/``${VAR}`` expanded,
    missing dropped). ``auto_source_bashrc`` applies only without an explicit list:
    ~/.profile and ~/.bash_profile first (no interactivity guard; where
    n/nvm/asdf/pyenv add PATH), ~/.bashrc last (Debian's returns early when
    non-interactive, but guard-less bashrcs keep working)."""
    explicit, auto_bashrc = _read_terminal_shell_init_config()
    candidates = explicit or (["~/.profile", "~/.bash_profile", "~/.bashrc"]
                              if auto_bashrc and not _IS_WINDOWS else [])
    resolved: list[str] = []
    for raw in candidates:
        try:
            path = os.path.expandvars(os.path.expanduser(raw))
            if path and os.path.isfile(path):
                resolved.append(path)
        except Exception:
            continue
    return resolved


def _prepend_shell_init(cmd_string: str, files: list[str]) -> str:
    """Prepend guarded, silent ``source <file>`` lines: ``set +e`` keeps going on
    errors, ``2>/dev/null`` hides noisy prompts, ``|| true`` neutralises the status."""
    if not files:
        return cmd_string
    safe = [p.replace("'", "'\\''") for p in files]
    prelude = ["set +e", *(f"[ -r '{p}' ] && . '{p}' 2>/dev/null || true" for p in safe)]
    return "\n".join(prelude) + "\n" + cmd_string


# --- Process-group teardown (POSIX) ---
def _wait_for_group_exit(proc, pgid: int, timeout: float) -> bool:
    """Wait until the process group is gone, reaping the wrapper as we go (a dead
    but unreaped group leader still makes ``killpg(pgid, 0)`` succeed).
    POSIX-only; callers are behind the _IS_WINDOWS gate."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.poll()
        except Exception:
            pass
        try:
            os.killpg(pgid, 0)  # windows-footgun: ok — POSIX process-group alive probe
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # exists, even if we cannot signal it
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _sweep_escaped_descendants(descendants: list, pgid: int) -> None:
    """SIGKILL snapshotted survivors that escaped the process group via ``setsid``
    — after TERM→KILL so in-group members keep their grace; psutil's identity-aware
    Process skips recycled PIDs. POSIX-only (see _IS_WINDOWS gate in caller)."""
    for child in descendants:
        try:
            if not child.is_running():
                continue
            try:
                if os.getpgid(child.pid) == pgid:
                    continue  # group-kill already covers it
            except OSError:  # ProcessLookupError / PermissionError included
                pass
            child.kill()
        except Exception:
            continue


def _kill_process_group_posix(proc) -> None:
    """TERM the group, wait, KILL, then sweep setsid escapees. Descendants are
    snapshotted BEFORE the first signal — once the wrapper dies they reparent to
    init — and we wait on the group, not the wrapper, which can exit before
    grandchildren under load. POSIX-only (_IS_WINDOWS handled by the caller)."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        if (pgid := getattr(proc, "_hermes_pgid", None)) is None:
            raise
    try:  # psutil children snapshot; empty on any failure (must never break the kill)
        import psutil
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        descendants = []
    try:
        os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok — POSIX only (see _IS_WINDOWS gate in caller)
        if not _wait_for_group_exit(proc, pgid, 1.0):
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX only (see _IS_WINDOWS gate in caller)
            _wait_for_group_exit(proc, pgid, 2.0)
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=0.2)
    except ProcessLookupError:
        pass
    _sweep_escaped_descendants(descendants, pgid)


def _kill_process_windows(proc) -> None:
    """Identity-checked terminate (start time guards against PID reuse), else kill."""
    try:
        from gateway.status import get_process_start_time, terminate_pid
        terminate_pid(proc.pid, force=True, expected_start_time=get_process_start_time(proc.pid))
    except Exception:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=2.0)


class LocalEnvironment(BaseEnvironment):
    """Run commands directly on the host: every execute() spawns a fresh bash;
    the session snapshot preserves env vars across calls; CWD persists via the
    stdout marker."""

    _profile_scoped_passthrough = True
    # Commands run on the Hermes host itself — controller-side platform behavior
    # (macOS TCC pruning, etc.) legitimately applies here.
    is_local = True

    def _additional_profile_scoped_passthrough_names(self) -> tuple[str, ...]:
        """First-party ``BUZZ_*`` names present in the env, excluded from the shared
        session snapshot. env_passthrough can never list them (it refuses blocklisted
        names), so under a multiplexed gateway profile A's BUZZ_PRIVATE_KEY would land
        in the snapshot and be sourced by profile B. Prefix-only and monotonic on
        purpose: conservative even when the context-gated carve-out is inactive."""
        merged = dict(os.environ | self.env)
        return tuple(sorted(
            name for name in merged
            if isinstance(name, str) and _matches_terminal_first_party_prefix(name)))

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        super().__init__(cwd=_resolve_local_initial_cwd(cwd), timeout=timeout, env=env)
        self.init_session()

    def get_temp_dir(self) -> str:
        """Shell-safe writable temp dir. Precedence: ``TERMINAL_TEMP_DIR``, TMPDIR/TMP/TEMP
        (Termux has no /tmp), ``HERMES_HOME/cache/terminal`` (real storage: tmpfs /tmp
        fills under Hermes load; pruned by ``cleanup_terminal_temp_cache``), /tmp,
        ``tempfile.gettempdir()``; backend env before process env so terminal.env
        overrides work. Windows: ``%TEMP%`` often has spaces that break unquoted bash,
        so always the HERMES_HOME cache dir with forward slashes (bash- and Python-valid)."""
        if _IS_WINDOWS:
            cache_dir = (_default_terminal_temp_dir()
                         or Path(tempfile.gettempdir()) / "hermes_terminal")
            cache_dir.mkdir(parents=True, exist_ok=True)
            _prune_terminal_temp_once()
            return str(cache_dir).replace("\\", "/")
        def _posix(p: str) -> str:
            return p.rstrip("/") or "/"
        for env_var in ("TERMINAL_TEMP_DIR", "TMPDIR", "TMP", "TEMP"):
            candidate = self.env.get(env_var) or os.environ.get(env_var)
            if candidate and candidate.startswith("/") and (
                    env_var != "TERMINAL_TEMP_DIR" or os.path.isdir(candidate)):
                return _posix(candidate)
        try:
            cache_dir = _default_terminal_temp_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            resolved = str(cache_dir)
            if resolved.startswith("/") and os.access(resolved, os.W_OK | os.X_OK):
                _prune_terminal_temp_once()
                return _posix(resolved)
        except Exception:
            pass
        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK | os.X_OK):
            return "/tmp"
        fallback = tempfile.gettempdir()
        return _posix(fallback) if fallback.startswith("/") else "/tmp"

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        """Use native paths for Python, but Git Bash-friendly paths for cd."""
        return BaseEnvironment._quote_cwd_for_cd(_windows_to_msys_path(cwd))

    def _quote_shell_path(self, path: str) -> str:
        """Rewrite native/mixed Windows paths before quoting for Git Bash."""
        return _quote_bash_path(path)

    def _recover_cwd(self) -> None:
        """Swap ``self.cwd`` for a usable directory if it vanished or is inaccessible
        (e.g. a command ``rm -rf``'d its own cwd) — otherwise Popen raises before bash
        starts and every subsequent call fails. A benign MSYS→Windows normalization
        is not warned about."""
        # Recover when the cwd has been deleted out from under us — usually by a previous tool call that ran
        # ``rm -rf`` on its own working dir (issue #17558). On Windows, ``_resolve_safe_cwd`` also
        # normalises Git Bash-style POSIX paths (``/c/Users/...``) to native form so a perfectly valid ``pwd
        # -P`` result from bash isn't mistakenly treated as "missing" and spammed as a warning on every
        # command.
        safe_cwd = _resolve_safe_cwd(self.cwd)
        if safe_cwd == self.cwd:
            return
        if safe_cwd != _msys_to_windows_path(self.cwd):
            logger.warning(
                "LocalEnvironment cwd %r is missing on disk; "
                "falling back to %r so terminal commands keep working.",
                self.cwd, safe_cwd)
        self.cwd = safe_cwd

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        # Login invocations (init_session's env snapshot) source the user's rc /
        # custom init files so nvm/asdf/pyenv land on PATH in the snapshot.
        if login:
            cmd_string = _prepend_shell_init(cmd_string, _resolve_shell_init_files())
        args = [bash, *(["-l"] if login else []), "-c", cmd_string]
        self._recover_cwd()
        proc = subprocess.Popen(
            args, text=True, env=_make_run_env(self.env), encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            start_new_session=True, cwd=self.cwd,
            **({"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}))
        if not _IS_WINDOWS:
            with contextlib.suppress(ProcessLookupError):
                proc._hermes_pgid = os.getpgid(proc.pid)
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _kill_process(self, proc):
        """Kill the entire process group (all children)."""
        try:
            (_kill_process_windows if _IS_WINDOWS else _kill_process_group_posix)(proc)
        except OSError:  # ProcessLookupError / PermissionError included
            with contextlib.suppress(Exception):
                proc.kill()

    def _extract_cwd_from_output(self, result: dict):
        """Base semantics plus: Git Bash ``pwd -P`` emits MSYS form on Windows —
        normalize to native and require the dir to exist, else ``_run_bash`` would
        warn every command. A stale path rolls back to the previous cwd, which this
        command did not observe, so ``cwd_observed`` is dropped."""
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd:
            normalized = _msys_to_windows_path(self.cwd)
            if normalized and os.path.isdir(normalized):
                self.cwd = normalized
                result["cwd"] = normalized
            else:
                self.cwd = prev_cwd
                result.pop("cwd_observed", None)
                result.pop("cwd", None)

    def cleanup(self):
        """Clean up temp files, including orphaned atomic-write snapshots
        (``snap.tmp.<bashpid>``) a failed/interrupted mv could leave behind."""
        # See #38249.
        import glob
        try:
            stale = glob.glob(f"{self._snapshot_path}.tmp.*")
        except Exception:
            stale = []
        for f in (self._snapshot_path, self._cwd_file, *stale):
            with contextlib.suppress(OSError):
                os.unlink(f)
