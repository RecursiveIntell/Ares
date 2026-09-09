"""Hermes update pipeline: dispatchers (``_cmd_update_impl``/``_cmd_update_check``) + git plumbing.

Each concern lives in ``update_cmd_<concern>.py`` and is re-imported here so
``hermes_cli.update_cmd.<name>`` keeps resolving (and stays monkeypatchable). Imports are one-way:
main -> update_cmd -> update_cmd_*; ``_m()`` resolves ``hermes_cli.main`` at call time.
"""

import logging
from contextlib import suppress
import os
import shlex
import shutil  # noqa: F401  (tests patch update_cmd.shutil.*; split modules resolve it here)
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.config import get_hermes_home  # noqa: F401  (re-exported; patched via update_cmd)
from hermes_cli.update_cmd_common import _best_effort
from hermes_constants import get_default_hermes_root, venv_python_path

# Re-exports: every split-module name stays reachable (and monkeypatchable) as update_cmd.<name>.
from hermes_cli.update_abort_recovery import (  # noqa: F401
    _abort_recovery_is_complete, _qualified_serve_skips, _recover_gateway_restart_after_abort,
    _serve_unit_recovery_available, _surviving_pre_update_serve_runtimes,
    _warn_stale_serve_runtimes)
from hermes_cli.update_cmd_windows import (  # noqa: F401
    _HOLDER_VALUE_FLAGS_FALLBACK, _clear_windows_venv_holders_or_exit,
    _cold_start_windows_gateway_after_update, _desktop_owns_gateway_lifecycle,
    _detect_venv_python_processes, _format_venv_python_holders_message,
    _handoff_reapable_backend_pids, _hermes_holder_subcommand, _holder_value_flags,
    _holder_value_flags_cache, _ledger_manual_serve_holders, _ledger_reapable_backend_pids,
    _leftover_pausable_gateway_pids, _looks_like_desktop_control_plane,
    _orphaned_desktop_backend_pids, _pause_windows_gateways_for_update,
    _refresh_bootstrap_cache_scripts, _refresh_windows_gateway_launchers,
    _refuse_gateway_ancestor_tree_kill, _relaunch_stopped_serves,
    _restore_windows_gateway_service, _resume_windows_gateways_after_update,
    _resume_windows_gateways_and_merge_outcome, _self_and_non_gateway_ancestor_pids,
    _serve_relaunch_commands, _start_windows_gateway_service, _stop_process_trees,
    _stop_windows_gateway_service, _venv_launcher_ancestors,
    _wait_for_windows_update_gateway_exit, _write_update_planned_stop_marker)
from hermes_cli.update_cmd_fleet import (  # noqa: F401
    _FLEET_RESTART_PENDING_NAME, _FRESH_RESTART_SUPERVISORS, _GatewayRestartOutcome,
    _apply_pending_fleet_restart_catchup, _clear_fleet_restart_pending_marker,
    _current_checkout_sha, _drain_or_signal_gateway_for_update, _fleet_probe_expected_runtimes,
    _fleet_restart_pending_marker_path, _for_each_systemd_gateway_unit,
    _gateway_recovery_partition, _gateway_service_matches_profile, _pending_fleet_restart_needed,
    _receipt_looks_unfinished, _receipt_reports_stale_runtime, _resolve_manage_cmd,
    _restart_gateway_fleet_after_update, _restart_launchd_gateway_after_update,
    _restart_macos_launchd_gateways, _restart_phase_failure_is_incomplete,
    _restart_systemd_gateway_units, _restart_systemd_gateway_units_best_effort,
    _run_pending_fleet_restart, _service_restart_sec,
    _service_unit_supports_graceful_sigusr1_restart, _surviving_gateway_pids_after_failed_restart,
    _systemctl, _systemctl_reset_and_restart, _verify_fleet_after_update,
    _wait_for_service_active, _warn_gateway_restart_phase_aborted,
    _warn_incomplete_gateway_fleet_restart, _warn_pending_fleet_restart,
    _warn_pending_fleet_restart_on_startup, _write_fleet_restart_pending_marker,
    _write_gateway_update_exit_code)
from hermes_cli.update_cmd_zip import (  # noqa: F401
    _ZIP_PRESERVED_TOP_LEVEL, _ZIP_STAGING_ARTIFACT_SUFFIXES, _abort_zip_update_if_dirty_tree,
    _atomic_replace_dir, _commit_staged_replacements, _discard_staged,
    _is_zip_preserved_entry_status_line, _is_zip_staging_artifact_status_line, _stage_replacement,
    _update_via_zip, _zip_overlay_block_reason)
from hermes_cli.update_cmd_stash import (  # noqa: F401
    _AUTOSTASH_NAME_PREFIX, _AUTOSTASH_WARN_AGE_DAYS, _discard_stashed_changes,
    _git_untracked_paths, _park_stashed_changes, _print_stash_cleanup_guidance,
    _reject_unsafe_stash_restore, _resolve_stash_selector, _restore_stashed_changes,
    _restored_python_paths, _stash_apply_failed_only_on_existing_untracked,
    _stash_local_changes_if_needed, _warn_orphaned_update_autostashes)
from hermes_cli.update_cmd_config import (  # noqa: F401
    _LAST_SIBLING_SNAPSHOTS, _check_and_apply_config_migration, _migrate_sibling_profile_configs,
    _print_items, _reload_config_modules, _run_config_check_fresh, _run_migrate_config_fresh)
from hermes_cli.update_cmd_deps import (  # noqa: F401
    _INSTALL_DEFINING_FILES, _SELF_LOCKING_NATIVE_MODULES, _UPDATE_CRITICAL_MODULES,
    _abort_dependency_sync_if_self_locked, _capture_active_lazy_features,
    _capture_active_tool_dependencies, _critical_module_import_failures,
    _defer_update_for_self_lock, _dependency_sync_would_rewrite, _desktop_app_present,
    _detect_self_loaded_native_modules, _editable_install_is_current, _ensure_uv_for_termux,
    _ensure_venv_pip, _install_psutil_android_compat, _is_android_python, _npm_bin_exists,
    _npm_lockfile_changed, _npm_manifest_paths, _npm_manifests_digest, _path_uid,
    _rebuild_desktop_after_update, _record_npm_lockfile_hash, _refresh_active_lazy_features,
    _refresh_active_memory_provider_dependencies, _refuse_update_if_venv_foreign_owned,
    _repair_node_deps_on_current_checkout, _restore_active_tool_dependencies,
    _sync_python_dependencies_after_pull, _update_node_dependencies,
    _upgrade_pip_before_lazy_refresh, _validate_critical_modules_import,
    _venv_core_imports_healthy, _venv_foreign_owned_paths, _web_build_toolchain_ready,
    _web_toolchain_roots)
from hermes_cli.update_cmd_git import (  # noqa: F401
    OFFICIAL_REPO_URL, OFFICIAL_REPO_URLS, SKIP_UPSTREAM_PROMPT_FILE, _ORPHAN_RESCUE_REFS_TO_KEEP,
    _ORPHAN_RESCUE_REF_MAX_AGE_DAYS, _add_upstream_remote, _assess_parked_branch_switch,
    _branch_head_label, _branch_head_suffix, _classify_fetch_failure, _count_commits_between,
    _discard_lockfile_churn, _ensure_non_trampoline_git, _get_origin_url, _git_is_trampoline,
    _has_upstream_remote, _is_fork, _locate_real_git, _mark_skip_upstream_prompt,
    _normalize_managed_eol, _portable_git_candidates, _print_fetch_failure,
    _print_parked_branch_kept_notice, _print_parked_branch_skip_warning,
    _prune_orphan_rescue_refs, _should_skip_upstream_prompt, _sync_fork_with_upstream,
    _sync_with_upstream_if_needed)
from hermes_cli.update_cmd_maint import (  # noqa: F401
    _PRE_UPDATE_SNAPSHOT_KEEP, _PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE, _STALE_PURGE_PREFIXES,
    _STALE_PURGE_PROTECTED, _UPDATE_RUNTIME_RELOAD_MODULES, _clear_stale_sqlite_sidecars,
    _ensure_acp_launcher, _ensure_fhs_path_guard, _finish_dashboard_update_cleanup,
    _format_time_ago, _post_update_sqlite_runtime_status, _print_bundled_skills_sync_report,
    _print_curator_first_run_notice, _print_curator_recent_run_notice,
    _print_fts_optimize_available_notice, _print_update_completion, _print_update_summary,
    _print_verified_update_completion, _purge_stale_hermes_modules, _read_project_version,
    _reload_process_scan_modules, _reload_updated_runtime_modules,
    _resolve_pre_update_backup_mode, _restore_state_db_from_snapshot,
    _run_post_update_maintenance, _run_pre_update_backup, _sweep_bytecode_after_update,
    _update_complete_message, _verify_and_restore_one_state_db,
    _verify_and_restore_state_dbs_post_update)
logger = logging.getLogger(__name__)


def _m():
    """Lazy ``hermes_cli.main`` handle: keeps main-side test patches effective, import one-way."""
    from hermes_cli import main
    return main


_UPDATE_RUNTIME_RELOAD_MODULES = (
    "hermes_constants",
    "tools.environments.local",
    "tools.lazy_deps",
)

def _reload_updated_runtime_modules() -> None:
    """Reload update-sensitive modules after the checkout changes in-place.

    ``hermes update`` keeps running in the pre-pull Python process. After a
    large update, modules already present in ``sys.modules`` can still expose
    old symbols even though their source files on disk are new. Refresh the
    small module set used by lazy-backend refresh before that step imports
    newly-updated code paths.
    """
    try:
        import importlib

        importlib.invalidate_caches()
        for module_name in _UPDATE_RUNTIME_RELOAD_MODULES:
            module = _m().sys.modules.get(module_name)
            if module is None:
                continue
            try:
                importlib.reload(module)
            except Exception as exc:
                logger.debug("Could not reload updated module %s: %s", module_name, exc)
    except Exception as exc:
        logger.debug("Could not refresh update runtime modules: %s", exc)


def _reload_config_modules() -> None:
    """Force-reload modules from disk after git pull.

    ``hermes update`` runs in the PRE-pull Python process. After ``git pull``
    updates the source files on disk, modules already in ``sys.modules``
    still hold the OLD code. Function-level imports return the cached module,
    so ``DEFAULT_CONFIG["_config_version"]`` is the OLD value and
    ``check_config_version()`` reports ``(33, 33)`` — "up to date" — even
    though the freshly-pulled code has v34 with a migration to run.

    This function force-reloads ``hermes_cli.config_defaults``,
    ``hermes_cli.config``, and ``hermes_cli.config_migrations`` from disk
    so subsequent imports read the UPDATED code.

    It also reloads ``hermes_cli._subprocess_compat`` and
    ``hermes_cli.dashboard_procs`` so that post-update dashboard cleanup
    (``_finish_dashboard_update_cleanup`` → ``_scan_dashboard_processes``)
    uses the freshly-pulled code. Without this, a new symbol added to
    ``_subprocess_compat`` (e.g. ``bounded_probe_run``) is invisible to the
    cached module object, causing ``ImportError`` during the cleanup step
    that runs later in the same process.
    """
    import importlib

    importlib.invalidate_caches()
    for mod_name in (
        "hermes_cli.config_defaults",
        "hermes_cli.config",
        "hermes_cli.config_migrations",
        "hermes_cli._subprocess_compat",
        "hermes_cli.dashboard_procs",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            try:
                importlib.reload(mod)
            except Exception as exc:
                logger.debug("Could not reload %s for fresh post-update code: %s", mod_name, exc)


def _run_config_check_fresh() -> tuple:
    """Check config version using freshly-reloaded modules.

    See ``_reload_config_modules`` for why this is necessary.
    Returns ``(current_ver, latest_ver)``.
    """
    _reload_config_modules()
    from hermes_cli.config import check_config_version

    return check_config_version()


def _run_migrate_config_fresh(*, interactive: bool = False, quiet: bool = False) -> dict:
    """Run config migration using freshly-reloaded modules.

    See ``_reload_config_modules`` for why this is necessary.
    Returns the migration results dict.
    """
    _reload_config_modules()
    from hermes_cli.config import migrate_config

    return migrate_config(interactive=interactive, quiet=quiet)


def _migrate_sibling_profile_configs() -> list[tuple[str, int, int]]:
    """Migrate every SIBLING profile's config.yaml to the current version.

    #91277 Phase 2 (fleet-wide config migration; #20438/#54926/#79048): the
    shared checkout serves every profile, but ``hermes update`` historically
    migrated only the active profile's config — siblings drifted versions
    until their gateway hit a config the new code couldn't read.

    Per profile home (skipping the active one, already migrated by the
    caller): scope config reads/writes via the context-local HERMES_HOME
    override (thread-safe — never ``os.environ``), check the version, and
    run the NON-INTERACTIVE, quiet migration. Prompt-requiring settings are
    left for the profile's own next interactive session, identical to the
    gateway-mode contract for the active profile.

    Returns ``[(profile_name, from_version, to_version), ...]`` for profiles
    actually migrated. Never raises; a failing profile is skipped (its own
    startup migration remains the fallback).
    """
    migrated: list[tuple[str, int, int]] = []
    try:
        from hermes_constants import (
            get_process_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.profiles import _get_profiles_root, _PROFILE_ID_RE

        active_home = get_process_hermes_home()
        root = _get_profiles_root()
        if not root.is_dir():
            return migrated
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not _PROFILE_ID_RE.match(entry.name):
                continue
            try:
                if entry.resolve() == Path(active_home).resolve():
                    continue
            except OSError:
                continue
            if not (entry / "config.yaml").is_file():
                continue  # profile never configured — nothing to migrate
            token = set_hermes_home_override(entry)
            try:
                current_ver, latest_ver = _run_config_check_fresh()
                if current_ver >= latest_ver:
                    continue
                _run_migrate_config_fresh(interactive=False, quiet=True)
                after_ver, _ = _run_config_check_fresh()
                if after_ver > current_ver:
                    migrated.append((entry.name, current_ver, after_ver))
            except Exception as exc:
                logger.debug(
                    "Config migration for profile %s failed: %s", entry.name, exc
                )
            finally:
                reset_hermes_home_override(token)
    except Exception as exc:
        logger.debug("Sibling profile enumeration failed: %s", exc)
    return migrated

def _check_and_apply_config_migration(
    *,
    assume_yes: bool = False,
    gateway_mode: bool = False,
    pre_update_snapshot_id: str | None = None,
) -> None:
    """Check and apply configuration migrations on an update completion path (#91360).

    CRITICAL: ``check_config_version`` and ``migrate_config`` must use
    freshly-reloaded modules, not the ``sys.modules`` cache (see
    ``_reload_config_modules``). This must run on EVERY update completion
    path — the normal post-pull path, the venv-repair retry and the
    Node-deps repair on the ``commit_count == 0`` "Already up to date"
    branch — so an interrupted update that previously pulled new code does
    not strand the user on an older config version.
    """
    print()
    print("→ Checking configuration for new options...")

    # Reload config modules BEFORE any config reads so get_missing_*,
    # check_config_version, and migrate_config all use the updated code.
    _reload_config_modules()

    from hermes_cli.config import (
        get_missing_env_vars,
        get_missing_config_fields,
    )

    # Defensive (#91360): this helper runs on repair/retry completion paths
    # too — a config-check failure must not break an otherwise-successful
    # update. Log, point at the manual command, and return.
    try:
        missing_env = get_missing_env_vars(required_only=True)
        missing_config = get_missing_config_fields()
        current_ver, latest_ver = _run_config_check_fresh()
    except Exception as exc:
        logger.debug("Config check during update failed: %s", exc)
        print("  ⚠️  Could not check config version.")
        print("     Run 'hermes config migrate' to check manually.")
        return

    has_new_options = bool(missing_env or missing_config)
    version_bump_only = (
        not has_new_options and current_ver < latest_ver
    )
    needs_migration = has_new_options or current_ver < latest_ver

    if version_bump_only:
        # Nothing for the user to fill in — only the config format version
        # changed (new defaults already merge in transparently). Asking
        # "configure new options now?" here is misleading: saying yes just
        # bumps the version and looks like a no-op (issue: ScottFive /
        # Tt2021). Apply it silently and say what actually happened.
        print()
        print(
            f"  ℹ Updating config format (v{current_ver} → v{latest_ver})…"
        )
        try:
            _mig_results = _run_migrate_config_fresh(
                interactive=False, quiet=True
            )
            print("  ✓ Config format updated (no new settings to configure)")
            # quiet=True also mutes migration steps that RESET or REMOVE an
            # existing setting (e.g. the v33→v34 personality reset from
            # #81946, which records its note only in the results dict).
            # Re-surface those notes so an unattended update never silently
            # changes user configuration (#86656). In this branch
            # missing_config is empty, so config_added can only contain
            # migration-step mutations, not missing-key listings.
            for _note in _mig_results.get("config_added") or []:
                print(f"  ℹ {_note}")
            for _warn in _mig_results.get("warnings") or []:
                print(f"  ⚠️  {_warn}")
        except Exception as _mig_err:
            print(f"  ⚠️  Config format update failed: {_mig_err}")
            print("     Run 'hermes config migrate' to retry.")
    elif needs_migration:
        print()
        # Show WHAT changed, not just a count, so the user can make an
        # informed yes/no decision (previously the prompt named nothing).
        def _print_items(items, label, key, fallback_key=None):
            if not items:
                return
            print(f"  {label}:")
            shown = items[:8]
            for it in shown:
                if isinstance(it, dict):
                    name = it.get(key) or (fallback_key and it.get(fallback_key)) or "?"
                    desc = (it.get("description") or "").strip()
                else:
                    # Defensive: some callers/mocks pass bare name strings.
                    name = str(it)
                    desc = ""
                if desc:
                    print(f"      • {name} — {desc}")
                else:
                    print(f"      • {name}")
            extra = len(items) - len(shown)
            if extra > 0:
                print(f"      … and {extra} more")

        if missing_env:
            print(
                f"  ⚠️  {len(missing_env)} new required setting(s) need configuration"
            )
            _print_items(missing_env, "New settings", "name")
        if missing_config:
            print(f"  ℹ️  {len(missing_config)} new config option(s) available")
            _print_items(missing_config, "New options", "key")

        print()
        if assume_yes:
            print(
                "  ℹ --yes: auto-applying config migration (skipping API-key prompts)."
            )
            response = "y"
        elif gateway_mode:
            response = (
                _gateway_prompt(
                    "Would you like to configure new options now? [Y/n]", "n"
                )
                .strip()
                .lower()
            )
        elif not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("  ℹ Non-interactive session — applying safe config migrations.")
            response = "auto"
        else:
            try:
                response = (
                    input("Would you like to configure them now? [Y/n]: ")
                    .strip()
                    .lower()
                )
            except EOFError:
                response = "n"
            except UnicodeDecodeError:
                # input() can raise this when the terminal encoding can't
                # decode the byte sequence (e.g. a non-UTF-8 locale, or an
                # embedded terminal). Without this, the exception escapes
                # here and crashes the update at this prompt.
                print(
                    "  ⚠ Could not read input (encoding issue). Skipping. "
                    "Run 'hermes config migrate' manually to configure."
                )
                response = "n"

        if response in {"", "y", "yes", "auto"}:
            print()
            # Gateway mode, --yes, and non-interactive update contexts
            # (dashboard / web server actions) cannot prompt for API keys.
            # Still run the non-interactive migration pass before restarting
            # so new default config fields and version bumps are written
            # before the freshly updated gateway validates config at startup.
            interactive_migration = not (
                gateway_mode or assume_yes or response == "auto"
            )
            results = _run_migrate_config_fresh(interactive=interactive_migration, quiet=False)

            if results["env_added"] or results["config_added"]:
                print()
                print("✓ Configuration updated!")
            if (gateway_mode or assume_yes or response == "auto") and missing_env:
                print("  ℹ API keys require manual entry: hermes config migrate")
        else:
            print()
            print("Skipped. Run 'hermes config migrate' later to configure.")
    else:
        print("  ✓ Configuration is up to date")

    # Fleet-wide config migration (#91277 Phase 2; #20438 earliest report,
    # #54926, #79048): the shared checkout serves EVERY profile, but the
    # migration above only touched the active profile's config.yaml.
    # Sibling profiles kept their old _config_version and silently
    # drifted (field repro: sibling gateway restarted onto new code but
    # stayed at config v33 vs v37). Run the same NON-INTERACTIVE safe
    # migration for every sibling profile home, scoped via the
    # context-local HERMES_HOME override (never os.environ — other
    # threads must not see it).
    try:
        _migrated_siblings = _migrate_sibling_profile_configs()
        for _name, _from_ver, _to_ver in _migrated_siblings:
            print(
                f"  ✓ Profile '{_name}': config format updated "
                f"(v{_from_ver} → v{_to_ver})"
            )
    except Exception as exc:
        logger.debug("Sibling config migration failed: %s", exc)

    # Safety net: config-version migrations have been observed to leave
    # cron/jobs.json valid-but-empty, silently dropping every scheduled
    # job (issue #34600). The desktop scheduler can also overwrite with
    # its own small set, causing partial loss (issue #52144). If the
    # live file now has fewer jobs than the pre-update snapshot, restore
    # it and warn loudly.
    try:
        from hermes_cli.backup import restore_cron_jobs_if_emptied

        cron_restore = restore_cron_jobs_if_emptied(pre_update_snapshot_id)
        if cron_restore:
            print()
            print(
                "  ⚠️  cron/jobs.json lost jobs during this update — "
                f"restored {cron_restore['job_count']} job(s) from "
                f"pre-update snapshot {cron_restore['snapshot_id']}."
            )
    except Exception as exc:
        # Never let the cron safety net break an otherwise-good update.
        logger.debug("Cron jobs auto-restore check failed: %s", exc)

    # #66140: run the same cron-jobs safety net for every sibling
    # profile against ITS OWN pre-update snapshot (same-generation by
    # construction — both taken by this run).
    try:
        from hermes_cli.backup import restore_cron_jobs_all_profiles

        for _restored in restore_cron_jobs_all_profiles(
            _LAST_SIBLING_SNAPSHOTS
        ):
            print()
            print(
                f"  ⚠️  Profile '{_restored['profile']}': cron/jobs.json "
                f"lost jobs during this update — restored "
                f"{_restored['job_count']} job(s) from pre-update "
                f"snapshot {_restored['snapshot_id']}."
            )
    except Exception as exc:
        logger.debug("Sibling cron auto-restore check failed: %s", exc)


# Critical files that Hermes must be able to import immediately after an
# update/install. Most are imported on every CLI startup; ``web_server.py``
# is the desktop/dashboard backend path that a fresh Windows install launches
# right away. If any of these fail to parse after a pull, the user can be
# left with a bricked CLI or desktop backend. The post-pull syntax guard
# validates these and auto-rolls-back on failure.
_UPDATE_CRITICAL_FILES = (
    "hermes_cli/main.py", "hermes_cli/config.py", "hermes_cli/__init__.py",
    "hermes_cli/web_server.py", "cli.py", "run_agent.py", "model_tools.py", "toolsets.py",
    "hermes_constants.py")


def _record_update_step(step: str, ok: bool, detail: str = "") -> None:
    """Best-effort ``update_receipt.record_step``; the receipt must never break an update."""
    with suppress(Exception):
        from hermes_cli.update_receipt import record_step
        record_step(step, ok, detail)


def _git_run(git_cmd, args, cwd=None, *, check=False, network=False):
    """Run git capturing utf-8 text (default cwd: checkout); ``network=True`` disables the
    terminal prompt so an HTTP 401 fails fast instead of hanging."""
    return subprocess.run(
        git_cmd + args, cwd=_m().PROJECT_ROOT if cwd is None else cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=check,
        **(_no_prompt_git_kwargs() if network else {}))


def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA, or None if it can't be resolved."""
    try:
        result = _git_run(git_cmd, ["rev-parse", "HEAD"], cwd, check=True)
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def _validate_python_files_syntax(root, relpaths) -> tuple[bool, str | None, str | None]:
    """Compile *relpaths* under *root*; the .pyc goes to a temp dir, not ``__pycache__/`` (no
    race with test workers, no stale pyc for another interpreter)."""
    import py_compile
    import tempfile
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="hermes-syntax-check-") as tmpdir:
        for relpath in relpaths:
            path = root / relpath
            if not path.exists():
                continue
            cfile = Path(tmpdir) / (str(relpath).replace("/", "__") + "c")
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, str(path), str(exc)
            except OSError as exc:
                return False, str(path), f"could not read: {exc}"
    return True, None, None


def _validate_critical_files_syntax(root) -> tuple[bool, str | None, str | None]:
    """Compile ``_UPDATE_CRITICAL_FILES`` -> ``(ok, failing_path, error_message)``."""
    return _validate_python_files_syntax(root, _UPDATE_CRITICAL_FILES)


def _gateway_prompt(prompt_text: str, default: str = "", timeout: float = 300.0) -> str:
    """File-based IPC prompt for ``--gateway``: write a marker the gateway forwards to the
    messenger, poll for a response file, fall back to *default* on timeout."""
    import json as _json
    import uuid as _uuid
    from hermes_constants import get_hermes_home  # noqa: F811  (deliberate: constants variant)
    home = get_hermes_home()
    prompt_path, response_path = home / ".update_prompt.json", home / ".update_response"
    response_path.unlink(missing_ok=True)

    payload = {"prompt": prompt_text, "default": default, "id": str(_uuid.uuid4())}
    tmp = prompt_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(prompt_path)

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            with suppress(OSError, ValueError):
                answer = response_path.read_text(encoding="utf-8").strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
        _time.sleep(0.5)

    prompt_path.unlink(missing_ok=True)
    response_path.unlink(missing_ok=True)
    print(f"  (no response after {int(timeout)}s, using default: {default!r})")
    return default


def _called_process_error_cmd_parts(exc: subprocess.CalledProcessError) -> list[str]:
    """Normalize ``CalledProcessError.cmd`` into argv-style tokens."""
    cmd = exc.cmd
    if cmd is None:
        return []
    if isinstance(cmd, (str, bytes)):
        text = cmd.decode("utf-8", "replace") if isinstance(cmd, bytes) else cmd
        try:
            return shlex.split(text, posix=os.name != "nt")
        except ValueError:
            return text.split()
    return [str(part) for part in cmd]


def _called_process_error_is_git(exc: subprocess.CalledProcessError) -> bool:
    """True when the failed subprocess was git itself."""
    parts = _called_process_error_cmd_parts(exc)
    if not parts:
        return False
    # Windows argv may use backslashes; POSIX basename() would keep the whole path.
    name = os.path.basename(parts[0].replace("\\", "/")).lower()
    return name in {"git", "git.exe"}


def _called_process_error_is_python_dep_install(exc: subprocess.CalledProcessError) -> bool:
    """True when the failed subprocess was a uv/pip (or ensurepip) install."""
    parts = [part.lower() for part in _called_process_error_cmd_parts(exc)]
    if not parts:
        return False
    exe = os.path.basename(parts[0].replace("\\", "/"))
    return "ensurepip" in parts or ("install" in parts and (
        "pip" in parts or exe in {"pip", "pip.exe", "pip3", "pip3.exe", "uv", "uv.exe"}))


def _format_update_failure_stage(exc: subprocess.CalledProcessError) -> str:
    """Name the failed stage: git pull and dep install share one ``try``, and calling every
    CalledProcessError a git failure misled users and keyed the ZIP overlay on exception
    *type* rather than on git actually failing.

    See #85840, #87304.
    """
    if _called_process_error_is_python_dep_install(exc):
        return "Python dependency install failed"
    if _called_process_error_is_git(exc):
        return "Git update failed"
    return "Update step failed"


def _shim_quarantine_error_type() -> "type[BaseException]":
    """Strict-quarantine refusal type via ``_m()``; falls back to a never-raised private
    type when main.py lacks it (torn mid-update tree) so the ``except`` stays valid."""
    cls = getattr(_m(), "ShimQuarantineError", None)
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls

    class _Never(Exception):
        pass

    return _Never


def _refuse_update_for_contended_shims(exc: BaseException) -> None:
    """Fail closed when live shims could not be quarantined: a rename failing every retry
    proves a holder without FILE_SHARE_DELETE, and installing anyway strands the venv between
    versions. The code swap is already committed; only the dep install is deferred (via the
    update-incomplete marker). Exits 2 so the receipt records a refusal, not a failure.

    See #87331.
    """
    print("✗ Cannot continue the update: live Hermes launcher(s) could not be")
    print("  moved aside:")
    for name in getattr(exc, "failed_shims", []) or ["hermes.exe"]:
        print(f"    {name}")
    print("  Another process is holding this install's venv — typically Hermes")
    print("  Desktop, a gateway, or another hermes REPL — and mutating the venv")
    print("  now would strand it half-updated.")
    print("  The dependency install has been deferred: close the process(es)")
    print("  above, then run any `hermes` command to finish it automatically.")
    # Idempotent (git path already dropped it); covers ZIP/repair paths so the deferral is never silent.
    _write_update_incomplete_marker()
    sys.exit(2)


def _should_zip_fallback_on_update_error(exc: BaseException) -> bool:
    """ZIP fallback is only for Windows git file-I/O breakage: after a dep-install failure the
    pull already succeeded, so a ZIP overlay can't fix it and would replace every top-level
    entry except venv/node_modules/.git/.env, deleting uncommitted and untracked files."""
    return (
        isinstance(exc, subprocess.CalledProcessError)
        and _m()._is_windows()
        and _called_process_error_is_git(exc))


def _print_called_process_error_tail(exc: subprocess.CalledProcessError, *, limit: int = 12) -> None:
    """Print a captured stderr/stdout tail when the failing call recorded one."""
    blob = exc.stderr or exc.stdout or ""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    lines = [line for line in str(blob).splitlines() if line.strip()]
    if not lines:
        return
    print("  Last output:")
    for line in lines[-limit:]:
        print(f"    {line}")


def _zip_overlay_block_reason(
    root: Path, *, ignore_staging_artifacts: bool = False
) -> Optional[str]:
    """Why overlaying a ZIP onto ``root`` would destroy work, or None if safe.

    The ZIP path swaps every top-level entry (except a tiny preserve set) and
    then deletes the backups, so uncommitted edits and untracked files under
    a replaced directory are gone. Fail closed when git status cannot run:
    unknown dirtiness is not a license to clobber the tree (#87304).

    ``ignore_staging_artifacts`` is for the pre-swap re-check: phase 1 of the
    two-phase replace creates ``*.hermes-update-staging`` siblings inside the
    checkout, which git reports as untracked. Those are our own artifacts,
    not user work — without the filter the re-check would always refuse.
    """
    if not (root / ".git").exists():
        return None
    git_cmd = ["git"]
    if sys.platform == "win32":
        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
    result = subprocess.run(
        # -uall: a user-level ``status.showUntrackedFiles = no`` git config
        # would otherwise hide untracked files and silently blind this guard.
        # --ignored=matching: gitignored files are still USER DATA the ZIP
        # overlay would permanently delete (logs, scratch files, local data)
        # — a .gitignore entry must not blind the guard either (#87392).
        # ``matching`` reports an ignored directory as one ``dir/`` line
        # instead of enumerating its contents (cheaper, same verdict for the
        # top-level filter below). NOTE: ``--ignored=all`` is NOT a valid
        # git mode — it exits 128 and would fail-close every ZIP update.
        git_cmd + ["status", "--porcelain", "--untracked-files=all", "--ignored=matching"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f" ({detail[0]})" if detail else ""
        return f"could not check the working tree{suffix}"
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    # --ignored=all reports the ZIP path's own preserved entries (venv,
    # node_modules are gitignored on every normal install). The swap never
    # touches those top-level entries, so they must not turn into a false
    # dirty-tree refusal. Everything else — including ignored files — blocks.
    lines = [line for line in lines if not _is_zip_preserved_entry_status_line(line)]
    if ignore_staging_artifacts:
        lines = [
            line for line in lines if not _is_zip_staging_artifact_status_line(line)
        ]
    if lines:
        return "the working tree has uncommitted changes or untracked files"
    return None


_ZIP_STAGING_ARTIFACT_SUFFIXES = (".hermes-update-staging", ".hermes-update-old")
# Single source of truth for the top-level entries the ZIP swap preserves —
# consumed by both the dirty-tree filter below and _update_via_zip's swap loop.
_ZIP_PRESERVED_TOP_LEVEL = {"venv", "node_modules", ".git", ".env"}


def _is_zip_preserved_entry_status_line(line: str) -> bool:
    """True when every path on a porcelain status line sits under a top-level
    entry the ZIP swap preserves.

    The ``" -> "`` two-path split applies ONLY to rename/copy status codes
    (R/C): porcelain v1 does not quote a plain filename containing spaces,
    so an ignored file literally named ``venv -> node_modules`` on an
    ``!!``/``??`` line must be treated as ONE path — splitting it would
    filter it as two preserved tops and fail-open into the destructive swap.
    Requiring EVERY path preserved keeps renames leaving a preserved dir
    (``R venv/x -> src/x``) blocking, fail-closed.
    """
    status, payload = (line[:2], line[3:]) if len(line) >= 3 else ("", line)
    is_rename = any(code in "RC" for code in status)
    paths = payload.split(" -> ") if is_rename else [payload]
    for path in paths:
        top_level = (
            path.strip().strip('"').replace("\\", "/").rstrip("/").split("/", 1)[0]
        )
        if top_level not in _ZIP_PRESERVED_TOP_LEVEL:
            return False
    return True


def _is_zip_staging_artifact_status_line(line: str) -> bool:
    """True when a porcelain status line is our own two-phase-swap artifact."""
    payload = line[3:] if len(line) >= 3 else line
    top_level = (
        payload.strip().strip('"').replace("\\", "/").rstrip("/").split("/", 1)[0]
    )
    return top_level.endswith(_ZIP_STAGING_ARTIFACT_SUFFIXES)


def _abort_zip_update_if_dirty_tree() -> None:
    """Refuse to overlay a ZIP onto a dirty git checkout (#87304)."""
    reason = _zip_overlay_block_reason(_m().PROJECT_ROOT)
    if reason is None:
        return
    print(f"✗ ZIP fallback refused: {reason}.")
    print(
        "  Overlaying the ZIP would overwrite uncommitted edits and permanently "
        "delete untracked files."
    )
    print("  Stash or commit your changes, then rerun `hermes update`.")
    print("  To inspect: git status --porcelain")
    _m().sys.exit(1)


def _read_project_version() -> str | None:
    """Read the ``version`` field from the checkout's pyproject.toml.

    Reads the on-disk file (not importlib.metadata) because after a git
    pull the installed distribution metadata still describes the OLD
    version; the file is the only source that reflects what was just
    pulled. Returns None on any failure — version reporting is cosmetic
    and must never break an update.
    """
    try:
        import tomllib

        with open(_m().PROJECT_ROOT / "pyproject.toml", "rb") as fh:  # windows-footgun: ok — binary mode, tomllib requires bytes
            version = tomllib.load(fh).get("project", {}).get("version")
        return str(version) if version else None
    except Exception:
        return None


def _update_complete_message(pre_version: str | None) -> str:
    """Completion line with the version transition when it is known.

    Ported from PrimeIntellect-ai/prime-agent#630: after a successful
    self-update, show both versions (``v0.19.4 → v0.20.0``) so the user
    can see what they actually got. Falls back to the plain message when
    either side is unknown or the version did not change (e.g. several
    commits landed within one release).
    """
    post_version = _read_project_version()
    if pre_version and post_version and pre_version != post_version:
        return f"✓ Update complete! (v{pre_version} → v{post_version})"
    if post_version:
        return f"✓ Update complete! (v{post_version})"
    return "✓ Update complete!"


def _clear_stale_sqlite_sidecars(db_path: Path) -> None:
    """Delete the WAL / shared-memory / rollback-journal files next to *db_path*.

    Call this immediately before overwriting a database file with a snapshot
    image. Quick snapshots are produced by ``backup._safe_copy_db`` through
    ``sqlite3.backup()``, so the image is already checkpointed and owns no WAL —
    which is exactly why ``backup._EXCLUDED_SUFFIXES`` refuses to ship sidecars
    inside a snapshot. Copying the image over the destination replaces only the
    main database file, so any ``-wal`` / ``-shm`` left behind by the *old*
    database (a crashed writer, or a second Hermes process the updater's drain
    did not stop) survives and is replayed over the fresh image on the next
    open. The result passes ``PRAGMA integrity_check`` while serving the old
    database's contents, and the first checkpoint folds it in permanently.

    Removing them is safe here specifically: they belong to a database the
    caller has already declared corrupt and is about to discard.
    """
    for suffix in ("-wal", "-shm", "-journal"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _print_update_summary(
    *,
    node_failures: list,
    desktop_build_ok: bool,
    pre_update_version: str | None,
) -> None:
    """Final update banner. A failed Desktop rebuild is non-fatal for the
    Python side, but must not print ``✓ Update complete!`` (#88251)."""
    print()
    if node_failures or not desktop_build_ok:
        parts = []
        if node_failures:
            parts.append(
                f"Node.js dependencies for {', '.join(node_failures)} did not refresh"
            )
        if not desktop_build_ok:
            parts.append(
                "the desktop app was not rebuilt and is still on the previous build"
            )
        print("⚠ Update partially complete — " + "; ".join(parts) + ".")
        if node_failures:
            print("  Code and Python deps are updated, but the dashboard/TUI may")
            print("  be in a mixed state until the Node deps are rebuilt.")
        if not desktop_build_ok:
            print("  Run `hermes desktop` to retry the desktop rebuild.")
    else:
        _print_update_completion(_update_complete_message(pre_update_version))


def _write_gateway_update_exit_code(ok: bool) -> None:
    path = get_hermes_home() / ".update_exit_code"
    try:
        path.write_text("0" if ok else "1", encoding="utf-8")
    except OSError:
        pass


def _restore_state_db_from_snapshot(state_path: Path, snap_state: Path) -> bool:
    """Replace *state_path* with the snapshot image at *snap_state*.

    Shared by both post-update auto-restore paths (the ZIP update and the git
    pull). The destination's stale sidecars are cleared before the copy, so the
    restored image cannot be silently overwritten by the corrupt database's WAL
    replay — see :func:`_clear_stale_sqlite_sidecars`.

    Refuses (returns ``False``) while another process still holds the database
    or its sidecars open: copying a snapshot over a live writer's inode makes
    the writer's page cache and WAL index disagree with the file bytes, and
    its next checkpoint writes pages at offsets that no longer mean what it
    thinks — the #90950 page-1 clobber. ``None`` (scan unavailable) proceeds:
    the updater has already drained gateways, and refusing on "unknown" would
    disable auto-restore on every non-Linux host.

    Returns ``True`` when the restored file passes an integrity check. Raises
    ``OSError`` if the copy itself fails, which callers already report.
    """
    from hermes_cli.backup import _foreign_db_holder_pids, verify_sqlite_integrity

    holders = _foreign_db_holder_pids(state_path)
    if holders:
        print(
            f"  ✗ Auto-restore refused: process(es) {holders} still hold "
            "state.db or its WAL open. Stop them (hermes gateway stop), "
            "then restore manually with /snapshot restore."
        )
        return False
    _clear_stale_sqlite_sidecars(state_path)
    shutil.copy2(snap_state, state_path)
    restored = verify_sqlite_integrity(
        state_path, check_header=True, run_pragma=True
    )
    return bool(restored.get("valid"))


def _update_via_zip(args, *, had_desktop_app_before_update: bool = False) -> bool:
    """Update Hermes Agent by downloading a ZIP archive.

    Used on Windows when git file I/O is broken (antivirus, NTFS filter
    drivers causing 'Invalid argument' errors on file creation).

    Returns ``False`` when a Desktop rebuild ran and failed; ``True`` otherwise.
    """
    active_tool_dependencies = _m()._capture_active_tool_dependencies()

    import tempfile
    import zipfile
    from urllib.request import urlretrieve

    # Snapshot the pre-update version before files are replaced so the
    # completion line can report the transition (prime-agent#630 port).
    pre_update_version = _read_project_version()

    # The ZIP fallback exists for Windows git-file-I/O breakage. It pulls a
    # static archive from GitHub, which is fine for the default "main"
    # channel but would silently ignore --branch and update from main even
    # if the user asked for something else — exactly the silent-divergence
    # bug --branch was added to prevent. Refuse to proceed in that case
    # rather than lie.
    branch = _m()._resolve_update_branch(args)
    if branch != "main":
        print(
            f"✗ --branch={branch} is not supported on the Windows ZIP-fallback "
            "update path."
        )
        print(
            "  This path runs when git file I/O is broken on the system. "
            "Either resolve the git-side breakage (typically an antivirus "
            "or NTFS filter holding files open) and rerun `hermes update "
            f"--branch {branch}`, or update against main with `hermes update`."
        )
        _m().sys.exit(1)
    _abort_zip_update_if_dirty_tree()
    zip_url = (
        f"https://github.com/NousResearch/hermes-agent/archive/refs/heads/{branch}.zip"
    )

    print("→ Downloading latest version...")
    tmp_dir = tempfile.mkdtemp(prefix="hermes-update-")
    try:
        zip_path = os.path.join(tmp_dir, f"hermes-agent-{branch}.zip")
        urlretrieve(zip_url, zip_path)

        print("→ Extracting...")
        import stat as _stat
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Validate paths to prevent zip-slip (path traversal) AND reject
            # symlink members. A GitHub source ZIP for hermes-agent itself
            # should never contain symlinks — they'd point outside the
            # extracted tree and let an attacker who can compromise the
            # update mirror plant arbitrary files via the update path.
            tmp_dir_real = os.path.realpath(tmp_dir)
            for member in zf.infolist():
                member_path = os.path.realpath(os.path.join(tmp_dir, member.filename))
                if (
                    not member_path.startswith(tmp_dir_real + os.sep)
                    and member_path != tmp_dir_real
                ):
                    raise ValueError(
                        f"Zip-slip detected: {member.filename} escapes extraction directory"
                    )
                # Unix mode lives in the upper 16 bits of external_attr;
                # mask to the file-type bits.
                mode = (member.external_attr >> 16) & 0o170000
                if _stat.S_ISLNK(mode):
                    raise ValueError(
                        f"ZIP contains unsupported symlink member: {member.filename}"
                    )
            zf.extractall(tmp_dir)

        # GitHub ZIPs extract to hermes-agent-<branch>/
        extracted = os.path.join(tmp_dir, f"hermes-agent-{branch}")
        if not os.path.isdir(extracted):
            # Try to find it
            for d in os.listdir(tmp_dir):
                candidate = os.path.join(tmp_dir, d)
                if os.path.isdir(candidate) and d != "__MACOSX":
                    extracted = candidate
                    break

        # Copy updated files over existing installation, preserving venv/node_modules/.git
        preserve = _ZIP_PRESERVED_TOP_LEVEL
        entries = [i for i in os.listdir(extracted) if i not in preserve]

        # Two-phase replace (#76104). Phase 1 copies every entry — directories
        # AND top-level files — to a sibling staging path without touching
        # anything live; phase 2 swaps them all in with same-filesystem
        # renames and rolls back every swap if any one fails. Replacing
        # entries one-at-a-time (the previous shape) meant an interruption
        # partway left `agent/` new and `tools/` stale — all files valid, the
        # tree unbootable. Files matter as much as directories here: the repo
        # root holds 20 first-party modules (run_agent.py, cli.py,
        # hermes_constants.py, ...).
        #
        # Staging costs one extra copy of the tree on disk. Check up front so
        # we fail with a clear message instead of running out mid-copy.
        need = sum(
            os.path.getsize(os.path.join(dirpath, f))
            for entry in entries
            for dirpath, _dirs, files in os.walk(os.path.join(extracted, entry))
            for f in files
        ) + sum(
            os.path.getsize(os.path.join(extracted, e))
            for e in entries
            if os.path.isfile(os.path.join(extracted, e))
        )
        # Only the staging copy is new — the live tree already occupies its
        # space and the swaps are renames, not copies. Ask for the staging
        # copy plus 20% headroom rather than a full 2x, which would block
        # updates that would have succeeded on exactly the space-constrained
        # machines most likely to hit this path.
        required = int(need * 1.2)
        free = shutil.disk_usage(str(_m().PROJECT_ROOT)).free
        if free < required:
            raise RuntimeError(
                f"not enough free disk space to stage the update safely "
                f"(need ~{required // (1024 * 1024)} MB, have "
                f"{free // (1024 * 1024)} MB)"
            )

        staged: list[tuple[str, str]] = []
        try:
            for item in entries:
                src = os.path.join(extracted, item)
                dst = os.path.join(str(_m().PROJECT_ROOT), item)
                staged.append((_stage_replacement(src, dst), dst))
                # #70337/#87331: the GitHub source ZIP contains only source —
                # apps/desktop/release/ (the BUILT desktop app, win-unpacked/
                # Hermes.exe) exists only in the LIVE tree. Swapping `apps`
                # without it deletes the desktop build and breaks the
                # shortcut. Graft the live release dir into the staged copy
                # BEFORE the swap so the commit preserves it atomically.
                if item == "apps":
                    live_release = os.path.join(dst, "desktop", "release")
                    staged_release = os.path.join(
                        staged[-1][0], "desktop", "release"
                    )
                    if os.path.isdir(live_release) and not os.path.exists(
                        staged_release
                    ):
                        os.makedirs(os.path.dirname(staged_release), exist_ok=True)
                        shutil.copytree(live_release, staged_release)
        except Exception:
            # Nothing is live yet; drop the partial staging copies so a retry
            # starts from the same free space this attempt did.
            _discard_staged(staged)
            raise

        try:
            # Re-check the tree right before the swap (#87304 TOCTOU): the
            # download + extract + staging window above can take minutes, and
            # work created in it would be destroyed by the commit below. Our
            # own phase-1 staging siblings are filtered out — they are the
            # expected artifacts of getting here, not user work.
            recheck_reason = _zip_overlay_block_reason(
                _m().PROJECT_ROOT, ignore_staging_artifacts=True
            )
            if recheck_reason is not None:
                _discard_staged(staged)
                print(f"✗ ZIP fallback aborted before the swap: {recheck_reason}.")
                print(
                    "  Files appeared in the checkout while the update was "
                    "downloading; committing the swap would delete them."
                )
                print("  Stash or commit your changes, then rerun `hermes update`.")
                _m().sys.exit(1)
            _commit_staged_replacements(staged)
        except Exception:
            # The rollback already restored every swapped entry, but staging
            # copies for the not-yet-swapped entries (potentially most of a
            # full tree) are still on disk. Drop them, or the retry's
            # up-front free-space check — which runs BEFORE the lazy
            # per-entry leftover cleanup — fails on litter this attempt
            # left behind: the exact "retry fails harder" failure mode
            # _discard_staged exists to prevent. Safe post-rollback: swapped
            # entries' staging paths were renamed away, and _discard_staged
            # skips paths that no longer exist.
            _discard_staged(staged)
            raise
        update_count = len(staged)

        print(f"✓ Updated {update_count} items from ZIP")

    except Exception as e:
        print(f"✗ ZIP update failed: {e}")
        # The two-phase replace either commits every entry or rolls them all
        # back, so a failure here does not leave a mixed-version tree — don't
        # scare the user toward a reinstall they don't need.
        print("  Your existing install was left in place.")
        print(
            "  Re-run `hermes update` to retry; if the agent won't start, "
            "reinstall from https://hermes-agent.nousresearch.com"
        )
        _m().sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Clear stale bytecode after ZIP extraction
    removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
    if removed:
        print(
            f"  ✓ Cleared {removed} stale __pycache__ director{'y' if removed == 1 else 'ies'}"
        )
    _m()._record_bytecode_fingerprint()
    _m()._refresh_bootstrap_cache_scripts(branch)

    # Reinstall Python dependencies. Prefer .[all], but if one optional extra
    # breaks on this machine, keep base deps and reinstall the remaining extras
    # individually so update does not silently strip working capabilities.
    #
    # Self-lock deferral (relocated preflight — #86735): the ZIP code swap
    # above is already committed; defer only the dependency sync when this
    # process holds a native extension the sync must rewrite.
    _m()._abort_dependency_sync_if_self_locked()
    print("→ Updating Python dependencies...")

    from hermes_cli.managed_uv import ensure_uv, update_managed_uv

    # Keep managed uv current — runs `uv self update` if we already have one.
    update_managed_uv()

    uv_bin = ensure_uv()

    pip_cmd = [_m().sys.executable, "-m", "pip"]
    if not uv_bin:
        uv_bin = _ensure_uv_for_termux(pip_cmd)
    if uv_bin:
        # Same third-party UV-env isolation as the main update path (#83914):
        # a user-level UV_PYTHON_INSTALL_DIR / UV_PYTHON from unrelated
        # software must not steer which interpreter uv resolves here.
        from hermes_cli.managed_uv import managed_python_env

        uv_env = managed_python_env()
        uv_env["VIRTUAL_ENV"] = str(_m().PROJECT_ROOT / "venv")
        if _m()._is_termux_env(uv_env):
            uv_env.pop("PYTHONPATH", None)
            uv_env.pop("PYTHONHOME", None)
        try:
            _m()._install_python_dependencies_with_optional_fallback([uv_bin, "pip"], env=uv_env)
        except _shim_quarantine_error_type() as _sqe:
            # #87331: this runs inside the ZIP-fallback error handler, so the
            # boundary except clause in cmd_update cannot catch it — refuse
            # here with the same defer-via-marker contract.
            _refuse_update_for_contended_shims(_sqe)
    else:
        # Use sys.executable to explicitly call the venv's pip module,
        # avoiding PEP 668 'externally-managed-environment' errors on Debian/Ubuntu.
        # Some environments lose pip inside the venv; bootstrap it back with
        # ensurepip before trying the editable install.
        try:
            subprocess.run(
                pip_cmd + ["--version"],
                cwd=_m().PROJECT_ROOT,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                [_m().sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                cwd=_m().PROJECT_ROOT,
                check=True,
            )
        _m()._install_python_dependencies_with_optional_fallback(pip_cmd)

    install_prefix = [uv_bin, "pip"] if uv_bin else pip_cmd
    install_env = uv_env if uv_bin else None
    _m()._restore_active_tool_dependencies(
        active_tool_dependencies,
        install_prefix,
        env=install_env,
    )

    # ZIP path parity: heal the active memory provider's bridge packages
    # after the dependency reinstall, same as the git-pull path (#53272,
    # #70636).
    _m()._refresh_active_memory_provider_dependencies()

    # Now that dependencies are installed, verify the tree actually imports.
    # The copy loop above replaces top-level entries one at a time in
    # os.listdir order, so an interruption between (say) `agent/` and `tools/`
    # leaves a tree whose files all parse but cannot be imported together —
    # the ImportError-on-startup class this guard exists to catch. Deliberately
    # placed *after* the dependency reinstall so a genuinely-new third-party
    # requirement isn't misreported as a partial copy. There is no SHA to roll
    # back to here, so surface it with a concrete recovery step rather than
    # reporting a successful update over a bricked install.
    import_ok, failing_module, import_error = _validate_critical_modules_import(
        _m().PROJECT_ROOT
    )
    if not import_ok:
        print()
        print("✗ Update left the install in an unimportable state:")
        print(f"  {failing_module}: {import_error}")
        print()
        print("  This usually means the copy was interrupted partway through.")
        print("  Re-run `hermes update` to complete it.")
        _m().sys.exit(1)

    node_failures = _update_node_dependencies()
    _m()._build_web_ui(_m().PROJECT_ROOT / "web")
    desktop_build_ok = _rebuild_desktop_after_update(
        _m().PROJECT_ROOT / "apps" / "desktop",
        had_desktop_app_before_update=had_desktop_app_before_update,
    )

    # Sync skills
    try:
        from tools.skills_sync import sync_skills

        print("→ Syncing bundled skills...")
        result = sync_skills(quiet=True)
        if result["copied"]:
            print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
        if result.get("updated"):
            print(
                f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}"
            )
        if result.get("user_modified"):
            print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
            print(
                "    → see them: hermes skills list-modified  "
                "(diff/reset to resume updates)"
            )
        if result.get("cleaned"):
            print(f"  − {len(result['cleaned'])} removed from manifest")
        if result.get("relocated"):
            print(
                f"  → {len(result['relocated'])} moved to new upstream paths: "
                f"{', '.join(result['relocated'])}"
            )
        if not result["copied"] and not result.get("updated"):
            print("  ✓ Skills are up to date")
    except Exception:
        pass

    # Seed the model-catalog disk cache from the freshly-unpacked checkout
    # (same rationale as the git-pull path in _cmd_update_impl). Non-fatal.
    try:
        from hermes_cli.model_catalog import seed_cache_from_checkout

        if seed_cache_from_checkout(_m().PROJECT_ROOT):
            print("  ✓ Model catalog cache refreshed from checkout")
    except Exception as e:
        logger.debug("Model catalog seed during zip update failed: %s", e)

    # ── Post-update state.db integrity guard (#68474) ─────────────────
    # Same as the git-pull path: verify state.db survived the ZIP update
    # and auto-restore from the most recent pre-update snapshot if needed.
    try:
        from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity

        _state_path = get_hermes_home() / "state.db"
        if _state_path.exists():
            _state_ok = verify_sqlite_integrity(
                _state_path, check_header=True, run_pragma=True
            )
            if not _state_ok.get("valid"):
                print()
                print(
                    "⚠ state.db is corrupted after update: "
                    + _state_ok.get("message", "unknown error")
                )
                _snap_root = _quick_snapshot_root(get_hermes_home())
                if _snap_root.exists():
                    _snap_dirs = sorted(
                        (d for d in _snap_root.iterdir() if d.is_dir()),
                        reverse=True,
                    )
                    for _snap_dir in _snap_dirs:
                        _snap_state = _snap_dir / "state.db"
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(
                                _snap_state, check_header=True, run_pragma=True
                            )
                            if _snap_ok.get("valid"):
                                try:
                                    if _restore_state_db_from_snapshot(
                                        _state_path, _snap_state
                                    ):
                                        print(
                                            "  ✓ Auto-restored from snapshot "
                                            f"{_snap_dir.name}"
                                        )
                                    else:
                                        print(
                                            "  ✗ Auto-restore FAILED — restored "
                                            "copy also failed integrity"
                                        )
                                    break
                                except OSError as _exc:
                                    print(
                                        f"  ✗ Auto-restore file copy failed: {_exc}"
                                    )
                                    break
    except Exception as exc:
        logger.debug(
            "Post-update state.db integrity check (zip path) failed: %s", exc
        )

    _print_update_summary(
        node_failures=node_failures,
        desktop_build_ok=desktop_build_ok,
        pre_update_version=pre_update_version,
    )
    try:
        _print_curator_first_run_notice()
    except Exception as e:
        logger.debug("Curator first-run notice failed: %s", e)
    try:
        _print_curator_recent_run_notice()
    except Exception as e:
        logger.debug("Curator recent-run notice failed: %s", e)
    # Don't stop a working dashboard when the Node refresh failed — see the
    # git-update path for rationale (#30271).
    _finish_dashboard_update_cleanup(node_failures)
    try:
        from hermes_cli.update_receipt import finalize_update_receipt

        finalize_update_receipt(
            "success" if (desktop_build_ok and not node_failures) else "partial"
        )
    except Exception as _receipt_exc:
        logger.debug("Update receipt finalize (zip path) failed: %s", _receipt_exc)
    return desktop_build_ok

def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path) -> Optional[str]:
    status = subprocess.run(
        git_cmd + ["status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    if not status.stdout.strip():
        return None

    # If the index has unmerged entries (e.g. from an interrupted merge/rebase),
    # git stash will fail with "needs merge / could not write index".  Clear the
    # conflict state with `git reset` so the stash can proceed.  Working-tree
    # changes are preserved; only the index conflict markers are dropped.
    unmerged = subprocess.run(
        git_cmd + ["ls-files", "--unmerged"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if unmerged.stdout.strip():
        print("→ Clearing unmerged index entries from a previous conflict...")
        subprocess.run(git_cmd + ["reset"], cwd=cwd, capture_output=True)

    from datetime import datetime, timezone

    stash_name = datetime.now(timezone.utc).strftime(
        "hermes-update-autostash-%Y%m%d-%H%M%S"
    )
    print("→ Local changes detected — stashing before update...")
    prev_stash = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "refs/stash"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    push = subprocess.run(
        git_cmd + ["stash", "push", "--include-untracked", "-m", stash_name],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if push.stdout.strip():
        print(push.stdout.strip())
    stash_probe = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "refs/stash"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    stash_ref = stash_probe.stdout.strip()
    stash_created = (
        stash_probe.returncode == 0 and bool(stash_ref) and stash_ref != prev_stash
    )

    if push.returncode != 0:
        if stash_created:
            # git stash push exits non-zero when it saved everything but could
            # not delete some swept untracked files from the working tree
            # (e.g. a root-owned directory: "warning: failed to remove ...:
            # Permission denied").  The stash entry is complete — the changes
            # are safe — so this is not a failure.  Leave the undeletable
            # files in place and continue the update.
            if push.stderr.strip():
                print(push.stderr.strip())
            print(
                "  ⚠ Some untracked files could not be removed from the "
                "working tree (permission denied)."
            )
            print(
                "    They were still saved to the stash and were left in "
                "place — the update will continue."
            )
            # A partially-failed stash push also aborts its working-tree
            # cleanup for TRACKED modifications — they are saved in the stash
            # but still dirty the tree, which would break the checkout/pull
            # that follows. Safe to reset: everything is in the stash entry.
            subprocess.run(
                git_cmd + ["reset", "--hard", "HEAD"],
                cwd=cwd,
                capture_output=True,
            )
        else:
            # No stash entry was created: the changes were NOT saved.  This
            # is a real failure — bail out before the update touches HEAD.
            print("✗ Could not stash local changes — update aborted.")
            if push.stderr.strip():
                print(f"  {push.stderr.strip().splitlines()[0]}")
            print(
                "  Commit, stash, or clean up your local changes manually, "
                "then re-run `hermes update`."
            )
            raise subprocess.CalledProcessError(
                push.returncode, push.args, output=push.stdout, stderr=push.stderr
            )

    return stash_ref

def _resolve_stash_selector(
    git_cmd: list[str], cwd: Path, stash_ref: str
) -> Optional[str]:
    stash_list = subprocess.run(
        git_cmd + ["stash", "list", "--format=%gd %H"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(" ")
        if commit.strip() == stash_ref:
            return selector.strip()
    return None

def _print_stash_cleanup_guidance(
    stash_ref: str, stash_selector: Optional[str] = None
) -> None:
    print(
        "  Check `git status` first so you don't accidentally reapply the same change twice."
    )
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f"  Remove it with: git stash drop {stash_selector}")
    else:
        print(
            f"  Look for commit {stash_ref}, then drop its selector with: git stash drop stash@{{N}}"
        )

def _stash_apply_failed_only_on_existing_untracked(stderr: str) -> bool:
    """True when a ``git stash apply`` failure is ONLY about untracked files
    that already exist in the working tree.

    This is the tail end of the permission-denied autostash class: ``git stash
    push --include-untracked`` swept undeletable files (e.g. a root-owned
    ``packaging/`` directory) into the stash but could not remove them from
    disk.  On restore, git applies all tracked changes, then refuses to
    overwrite those still-present files (``already exists, no checkout`` /
    ``could not restore untracked files from stash``) and exits non-zero even
    though nothing was lost.  Any other error line (e.g. ``would be
    overwritten by merge`` / ``Aborting``) means the tracked apply itself
    failed and this returns False.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return False
    saw_untracked_error = False
    for ln in lines:
        if "already exists, no checkout" in ln:
            saw_untracked_error = True
        elif "could not restore untracked files from stash" in ln:
            saw_untracked_error = True
        elif ln.startswith(("warning:", "hint:")):
            continue
        else:
            return False
    return saw_untracked_error

def _park_stashed_changes(stash_ref: str) -> None:
    """Leave a pre-update autostash parked instead of re-applying it.

    Used by ``hermes update --keep-stash`` (the desktop updater's mode): the
    stash made the update possible on a dirty tree, but local source edits
    must never be silently re-applied onto the updated code. Nothing is
    lost — the entry stays in ``git stash`` with printed recovery guidance.
    """
    print()
    print("ℹ️  Local changes were stashed before updating and were NOT re-applied (--keep-stash).")
    print(f"  Stash ref: {stash_ref}")
    print(f"  Restore manually with: git stash apply {stash_ref}")


def _restore_stashed_changes(
    git_cmd: list[str],
    cwd: Path,
    stash_ref: str,
    prompt_user: bool = False,
    input_fn=None,
) -> bool:
    if prompt_user:
        print()
        print("⚠ Local changes were stashed before updating.")
        print(
            "  Restoring them may reapply local customizations onto the updated codebase."
        )
        print("  Review the result afterward if Hermes behaves unexpectedly.")
        print("Restore local changes now? [Y/n]")
        if input_fn is not None:
            response = input_fn("Restore local changes now? [Y/n]", "y")
        else:
            try:
                response = input().strip().lower()
            except (EOFError, UnicodeDecodeError):
                # Mirror the config-migration prompt's fix: don't let a
                # terminal-encoding issue or a closed stdin crash the
                # update mid-restore. Falls through to the existing
                # skip-restore path below, which already explains how to
                # restore manually from git stash.
                response = "n"
        if response not in {"", "y", "yes"}:
            print("Skipped restoring local changes.")
            print("Your changes are still preserved in git stash.")
            print(f"Restore manually with: git stash apply {stash_ref}")
            return False

    print("→ Restoring local changes...")
    restore = subprocess.run(
        git_cmd + ["stash", "apply", stash_ref],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )

    # Check for unmerged (conflicted) files — can happen even when returncode is 0
    unmerged = subprocess.run(
        git_cmd + ["diff", "--name-only", "--diff-filter=U"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    has_conflicts = bool(unmerged.stdout.strip())

    if restore.returncode != 0 and not has_conflicts and (
        _stash_apply_failed_only_on_existing_untracked(restore.stderr)
    ):
        # Permission-denied autostash tail end: the tracked changes applied
        # cleanly; the only "failure" is untracked files that never left the
        # working tree (git could not delete them at stash time, so it now
        # refuses to overwrite them). Their content was never touched —
        # nothing is lost. Treat as restored.
        print(
            "  ⚠ Some stashed untracked files already exist in the working "
            "tree and were kept as-is."
        )
    elif restore.returncode != 0 or has_conflicts:
        print("✗ Update pulled new code, but restoring local changes hit conflicts.")
        if restore.stdout.strip():
            print(restore.stdout.strip())
        if restore.stderr.strip():
            print(restore.stderr.strip())

        # Show which files conflicted
        conflicted_files = unmerged.stdout.strip()
        if conflicted_files:
            print("\nConflicted files:")
            for f in conflicted_files.splitlines():
                print(f"  • {f}")

        print("\nYour stashed changes are preserved — nothing is lost.")
        print(f"  Stash ref: {stash_ref}")

        # Always reset to clean state — leaving conflict markers in source
        # files makes hermes completely unrunnable (SyntaxError on import).
        # The user's changes are safe in the stash for manual recovery.
        subprocess.run(
            git_cmd + ["reset", "--hard", "HEAD"],
            cwd=cwd,
            capture_output=True,
        )
        print("Working tree reset to clean state.")
        print(f"Restore your changes later with: git stash apply {stash_ref}")
        # Don't sys.exit — the code update itself succeeded, only the stash
        # restore had conflicts.  Let cmd_update continue with pip install,
        # skill sync, and gateway restart.
        return False

    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Local changes were restored, but Hermes couldn't find the stash entry to drop."
        )
        print(
            "  The stash was left in place. You can remove it manually after checking the result."
        )
        _print_stash_cleanup_guidance(stash_ref)
    else:
        drop = subprocess.run(
            git_cmd + ["stash", "drop", stash_selector],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if drop.returncode != 0:
            print(
                "⚠ Local changes were restored, but Hermes couldn't drop the saved stash entry."
            )
            if drop.stdout.strip():
                print(drop.stdout.strip())
            if drop.stderr.strip():
                print(drop.stderr.strip())
            print(
                "  The stash was left in place. You can remove it manually after checking the result."
            )
            _print_stash_cleanup_guidance(stash_ref, stash_selector)

    print("⚠ Local changes were restored on top of the updated codebase.")
    print("  Review `git diff` / `git status` if Hermes behaves unexpectedly.")
    return True

def _discard_stashed_changes(
    git_cmd: list[str],
    cwd: Path,
    stash_ref: str,
) -> bool:
    """Throw away a stash created before an update, without applying it.

    Used only on a NON-interactive update when the user has set
    ``updates.non_interactive_local_changes: discard`` — i.e. they've opted out
    of keeping local source edits on this machine. Drops the stash entry
    instead of re-applying it, so the working tree stays clean at the freshly
    pulled HEAD. Unlike ``git reset --hard`` + ``git clean -fd``, this only
    affects what was stashed (tracked changes + the untracked files we
    explicitly captured) — ignored paths like node_modules/venv/build outputs
    are never touched, since they were never stashed.

    Returns True if the stash was dropped, False on a git failure (in which
    case the stash is left in place for safety).
    """
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Configured to discard local changes on non-interactive update, "
            "but Hermes couldn't find the stash entry to drop."
        )
        _print_stash_cleanup_guidance(stash_ref)
        return False

    drop = subprocess.run(
        git_cmd + ["stash", "drop", stash_selector],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if drop.returncode != 0:
        print(
            "⚠ Configured to discard local changes, but Hermes couldn't drop "
            "the saved stash entry."
        )
        if drop.stderr.strip():
            print(f"  {drop.stderr.strip().splitlines()[0]}")
        _print_stash_cleanup_guidance(stash_ref, stash_selector)
        return False

    print("→ Discarded local source changes (updates.non_interactive_local_changes=discard).")
    return True

OFFICIAL_REPO_URLS = {
    "https://github.com/NousResearch/hermes-agent.git",
    "git@github.com:NousResearch/hermes-agent.git",
    "https://github.com/NousResearch/hermes-agent",
    "git@github.com:NousResearch/hermes-agent",
}

OFFICIAL_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"

SKIP_UPSTREAM_PROMPT_FILE = ".skip_upstream_prompt"

def _get_origin_url(git_cmd: list[str], cwd: Path) -> Optional[str]:
    """Get the URL of the origin remote, or None if not set."""
    try:
        result = subprocess.run(
            git_cmd + ["remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def _is_fork(origin_url: Optional[str]) -> bool:
    """Check if the origin remote points to a fork (not the official repo)."""
    if not origin_url:
        return False
    # Normalize URL for comparison (strip trailing .git if present)
    normalized = origin_url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    for official in OFFICIAL_REPO_URLS:
        official_normalized = official.rstrip("/")
        if official_normalized.endswith(".git"):
            official_normalized = official_normalized[:-4]
        if normalized == official_normalized:
            return False
    return True

def _has_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Check if an 'upstream' remote already exists."""
    try:
        result = subprocess.run(
            git_cmd + ["remote", "get-url", "upstream"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False

def _add_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Add the official repo as the 'upstream' remote. Returns True on success."""
    try:
        result = subprocess.run(
            git_cmd + ["remote", "add", "upstream", OFFICIAL_REPO_URL],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False

def _count_commits_between(git_cmd: list[str], cwd: Path, base: str, head: str) -> int:
    """Count commits on `head` that are not on `base`. Returns -1 on error."""
    try:
        result = subprocess.run(
            git_cmd + ["rev-list", "--count", f"{base}..{head}"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return -1

def _should_skip_upstream_prompt() -> bool:
    """Check if user previously declined to add upstream."""
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).exists()

def _mark_skip_upstream_prompt():
    """Create marker file to skip future upstream prompts."""
    try:
        from hermes_constants import get_hermes_home

        (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).touch()
    except Exception:
        pass

def _sync_fork_with_upstream(git_cmd: list[str], cwd: Path) -> bool:
    """Attempt to push updated main to origin (sync fork).

    Returns True if push succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            git_cmd + ["push", "origin", "main", "--force-with-lease"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False

def _sync_with_upstream_if_needed(git_cmd: list[str], cwd: Path) -> None:
    """Check if fork is behind upstream and sync if safe.

    This implements the fork upstream sync logic:
    - If upstream remote doesn't exist, ask user if they want to add it
    - Compare origin/main with upstream/main
    - If origin/main is strictly behind upstream/main, pull from upstream
    - Try to sync fork back to origin if possible
    """
    has_upstream = _has_upstream_remote(git_cmd, cwd)

    if not has_upstream:
        # Check if user previously declined
        if _should_skip_upstream_prompt():
            return

        # Ask user if they want to add upstream
        print()
        print("ℹ Your fork is not tracking the official Hermes repository.")
        print("  This means you may miss updates from NousResearch/hermes-agent.")
        print()
        try:
            response = (
                input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print()
            response = "n"

        if response in {"", "y", "yes"}:
            print("→ Adding upstream remote...")
            if _add_upstream_remote(git_cmd, cwd):
                print(
                    "  ✓ Added upstream: https://github.com/NousResearch/hermes-agent.git"
                )
                has_upstream = True
            else:
                print("  ✗ Failed to add upstream remote. Skipping upstream sync.")
                return
        else:
            print(
                "  Skipped. Run 'git remote add upstream https://github.com/NousResearch/hermes-agent.git' to add later."
            )
            _mark_skip_upstream_prompt()
            return

    # Fetch upstream main only. This sync compares upstream/main with
    # origin/main, so there's no reason to pull every upstream ref — and a bare
    # fetch drags in thousands of auto-generated branches.
    print()
    print("→ Fetching upstream...")
    try:
        subprocess.run(
            git_cmd + ["fetch", "upstream", "main", "--quiet"],
            cwd=cwd,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("  ✗ Failed to fetch upstream. Skipping upstream sync.")
        return

    # Compare origin/main with upstream/main
    origin_ahead = _count_commits_between(git_cmd, cwd, "upstream/main", "origin/main")
    upstream_ahead = _count_commits_between(
        git_cmd, cwd, "origin/main", "upstream/main"
    )

    if origin_ahead < 0 or upstream_ahead < 0:
        print("  ✗ Could not compare branches. Skipping upstream sync.")
        return

    # If origin/main has commits not on upstream, don't trample
    if origin_ahead > 0:
        print()
        print(f"ℹ Your fork has {origin_ahead} commit(s) not on upstream.")
        print("  Skipping upstream sync to preserve your changes.")
        print("  If you want to merge upstream changes, run:")
        print("    git pull upstream main")
        return

    # If upstream is not ahead, fork is up to date
    if upstream_ahead == 0:
        print("  ✓ Fork is up to date with upstream")
        return

    # origin/main is strictly behind upstream/main (can fast-forward)
    print()
    print(f"→ Fork is {upstream_ahead} commit(s) behind upstream")
    print("→ Pulling from upstream...")

    try:
        subprocess.run(
            git_cmd + ["pull", "--ff-only", "upstream", "main"],
            cwd=cwd,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "  ✗ Failed to pull from upstream. You may need to resolve conflicts manually."
        )
        return

    print("  ✓ Updated from upstream")

    # Try to sync fork back to origin
    print("→ Syncing fork...")
    if _sync_fork_with_upstream(git_cmd, cwd):
        print("  ✓ Fork synced with upstream")
    else:
        print(
            "  ℹ Got updates from upstream but couldn't push to fork (no write access?)"
        )
        print("    Your local repo is updated, but your fork on GitHub may be behind.")

def _invalidate_update_cache():
    """Delete the update-check cache for ALL profiles: the repo is shared, so one profile's
    update makes every profile current and a stale "commits behind" banner would linger."""
    default_home = get_default_hermes_root()
    profiles_root = default_home / "profiles"
    homes = [default_home]
    if profiles_root.is_dir():
        homes += [entry for entry in profiles_root.iterdir() if entry.is_dir()]
    for home in homes:
        with suppress(Exception):
            (home / ".update_check").unlink(missing_ok=True)


def _write_marker_file(path: Path, *, label: str) -> None:
    """Drop an update-recovery breadcrumb. Never raises."""
    if _m()._pytest_owns_live_checkout(path.parent):
        logger.debug("Skipping %s marker under pytest (live checkout)", label)
        return
    try:
        path.write_text(f"started={_time.time()}\npid={os.getpid()}\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write %s marker: %s", label, exc)


def _write_update_incomplete_marker() -> None:
    """Drop the interrupted core-install breadcrumb. Never raises."""
    _write_marker_file(_m()._update_marker_path(), label="update-incomplete")


def _write_lazy_refresh_incomplete_marker() -> None:
    """Drop the interrupted lazy-refresh breadcrumb. Never raises."""
    _write_marker_file(_m()._lazy_refresh_marker_path(), label="lazy-refresh-incomplete")


def _format_concurrent_instances_message(matches: list[tuple[int, str]], scripts_dir: Path) -> str:
    """Explanation + remediation hint for the Windows concurrent-hermes.exe gate."""
    shim = scripts_dir / "hermes.exe"
    lines = [
        "✗ Another hermes.exe is running:",
        *(f"    PID {pid}  {name}" for pid, name in matches),
        "",
        f"  Updating now would fail to overwrite {shim} because",
        "  Windows blocks REPLACE on a running executable.",
        "",
        "  Close Hermes Desktop, exit any open `hermes` REPLs, and",
        "  stop the gateway (`hermes gateway stop`) before retrying.",
        ""]
    if matches:
        pid_args = " ".join(f"/PID {pid}" for pid, _ in matches)
        lines += [
            "  If you've already closed everything and these PIDs are",
            "  stale, terminate them directly, then retry the update:",
            f"      taskkill {pid_args} /F",
            ""]
    lines += [
        "  Override with `hermes update --force` if you've already",
        "  confirmed those processes will not write to the venv."]
    return "\n".join(lines)


def _classify_concurrent_instance(pid: int) -> str:
    """Classify ``pid`` as "gateway" / "non-gateway" / "unknown" (psutil can't read it). Uses
    ``_is_pausable_gateway`` (same matcher as the Desktop preflight and venv-holder guard) so
    "gateway" is exactly what the pause/restart machinery stops; "unknown" gates as non-gateway."""
    try:
        import psutil  # noqa: PLC0415
        cmdline_list = psutil.Process(int(pid)).cmdline()
    except Exception:
        return "unknown"

    from hermes_cli._scan_venv_blockers import _is_pausable_gateway  # noqa: PLC0415
    return "gateway" if _is_pausable_gateway(" ".join(cmdline_list or [])) else "non-gateway"


def _filter_non_gateway_concurrent_instances(matches: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop gateway matches (the pause + post-update restart machinery handles them); anything else
    (TUI, Desktop backend child, another REPL) has no pause path, so the gate aborts."""
    return [(pid, name) for pid, name in matches if _classify_concurrent_instance(pid) != "gateway"]


def _log_only_write(text: str) -> None:
    """Write to update.log only: reaches past the ``_UpdateOutputStream`` stdout mirror so
    loud, low-signal subprocess output stays debuggable without flooding the terminal."""
    if not text:
        return
    stream = _m().sys.stdout
    log_file = getattr(stream, "_log", None)
    with suppress(Exception):
        if log_file is None:
            log_path = get_hermes_home() / "logs" / "update.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fallback:
                fallback.write(text)
        else:
            log_file.write(text)
            log_file.flush()


def _run_logged_subprocess(cmd, *, cwd=None, env=None):
    """Stream combined build output to update.log, retaining it for failure reporting."""
    import codecs
    import io
    from hermes_cli._subprocess_compat import kill_process_tree, windows_hide_flags

    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    spawn = {"creationflags": windows_hide_flags()} if os.name == "nt" else {"process_group": 0}
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **spawn)
    # read1 delivers partial lines too; incremental decoding preserves split UTF-8
    # and the universal-newline behavior callers previously got from text=True.
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")("replace"), True)
    output = []
    try:
        while True:
            chunk = proc.stdout.read1(8192)
            text = decoder.decode(chunk, final=not chunk)
            output.append(text)
            _log_only_write(text)
            if not chunk:
                break
        return subprocess.CompletedProcess(cmd, proc.wait(), stdout="".join(output))
    except BaseException:
        # Unlike Popen.__exit__, do not wait for a cancelled build to finish.
        kill_process_tree(proc)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise
    finally:
        proc.stdout.close()


def _cmd_update_check(branch: str = "main", *, branch_explicit: bool = False):
    """``hermes update --check``: fetch and report without installing. ``branch_explicit`` is
    True iff --branch was passed (Docker installs print a notice instead of dropping the flag)."""
    # Same marker-first admission gate as the apply path, so --check never reports git
    # state for an install whose real update mechanism is an image pull.
    from hermes_cli.update_contract import evaluate_update_admission, record_refusal_receipt

    refusal = evaluate_update_admission(_m().PROJECT_ROOT)
    if refusal is not None:
        print(refusal.message)
        record_refusal_receipt(refusal)
        sys.exit(2)

    git_dir = _m().PROJECT_ROOT / ".git"
    if not git_dir.exists():
        print("✗ Not a git repository — cannot check for updates.")
        sys.exit(1)

    git_cmd = _base_git_cmd()

    # Interrupted fetches leave .git/*.lock behind ("File exists" forever); self-heal first.
    from hermes_cli.gitlock import clear_stale_git_locks, clear_stale_tmp_packs
    for lock_path in clear_stale_git_locks(_m().PROJECT_ROOT):
        print(f"  (removed stale git lock: {lock_path})")
    # Aborted fetches also strand tmp_pack_* debris (has reached 6 GB and corrupted the
    # pack dir); same age+process safety contract as the locks.
    swept = clear_stale_tmp_packs(_m().PROJECT_ROOT)
    if swept:
        print(f"  (removed {len(swept)} aborted-fetch pack temp file(s))")

    # Fetch only <branch> (a bare fetch pulls thousands of auto-generated branches). Prefer
    # upstream only for main (a fork's other branches have no upstream counterpart). Installer
    # checkouts are shallow: a plain fetch would unshallow them and rev-list would report a
    # bogus huge "behind" count, so fetch --depth 1 and report presence-only.
    is_shallow = _is_shallow_checkout(git_cmd)
    depth_args = ["--depth", "1"] if is_shallow else []

    # Probe locally for an 'upstream' remote before a network fetch non-forks always fail.
    fetch_result = None
    if branch == "main" and _git_run(git_cmd, ["remote", "get-url", "upstream"]).returncode == 0:
        print("→ Fetching from upstream...")
        fetch_result = _git_run(git_cmd, ["fetch"] + depth_args + ["upstream", branch], network=True)
    if fetch_result is not None and fetch_result.returncode == 0:
        compare_branch = f"upstream/{branch}"
    else:
        print("→ Fetching from origin...")
        fetch_result = _git_run(git_cmd, ["fetch"] + depth_args + ["origin", branch], network=True)
        compare_branch = f"origin/{branch}"

    if fetch_result.returncode != 0:
        _print_fetch_failure(fetch_result.stderr)
        sys.exit(1)

    # rev-list on a bogus ref exits 128 and (check=True) would traceback; verify first.
    verify_result = _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", compare_branch])
    if verify_result.returncode != 0:
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)

    if is_shallow:
        # No history across the shallow boundary: compare tip SHAs, then recover the
        # exact count via the GitHub compare API (complete graph).
        head_sha, target_sha = _tip_shas(git_cmd, compare_branch)
        if head_sha and target_sha and head_sha == target_sha:
            print("✓ Already up to date.")
            return
        from hermes_cli.banner import _github_compare_behind
        # counted == 0 means local-ahead, not behind; None means the API could not count.
        _print_update_check_result(_github_compare_behind(head_sha, target_sha), compare_branch)
        return

    rev_result = _git_run(git_cmd, ["rev-list", f"HEAD..{compare_branch}", "--count"], check=True)
    _print_update_check_result(int(rev_result.stdout.strip()), compare_branch)


def _base_git_cmd() -> list[str]:
    """``git`` argv; Windows adds ``-c windows.appendAtomically=false`` (git can fail "unable to
    write loose object file: Invalid argument" on non-atomic appends)."""
    if sys.platform == "win32":
        return ["git", "-c", "windows.appendAtomically=false"]
    return ["git"]


def _is_shallow_checkout(git_cmd) -> bool:
    return _git_run(git_cmd, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"


def _tip_shas(git_cmd, target_ref: str) -> tuple[str, str]:
    """``(HEAD sha, <target_ref> sha)`` as printed by rev-parse ("" when unresolvable)."""
    return tuple(_git_run(git_cmd, ["rev-parse", ref]).stdout.strip() for ref in ("HEAD", target_ref))


def _print_update_check_result(behind: int | None, compare_branch: str) -> None:
    """Report ``--check``'s verdict: up to date, N commits behind, or behind by an unknown count."""
    if behind == 0:
        print("✓ Already up to date.")
        return
    if behind is not None:
        print(f"⚕ Update available: {behind} {'commit' if behind == 1 else 'commits'} behind {compare_branch}.")
    else:
        print(f"⚕ Update available (behind {compare_branch}).")
    from hermes_cli.config import recommended_update_command
    print(f"  Run '{recommended_update_command()}' to install.")


def _repair_venv_on_current_checkout(
    *, assume_yes, gateway_mode, pre_update_snapshot_id, desktop_dir,
    had_desktop_app_before_update, active_lazy_features, active_tool_dependencies,
    _windows_gateway_resume) -> bool:
    """Reinstall ``.[all]`` + lazy/tool deps into an unhealthy (or handed-off) venv; returns
    whether the checkout can be reported complete."""
    # Self-lock deferral: the repair rewrites the venv too (same mapped-extension hazard).
    # See #86735.
    # Self-lock deferral (relocated preflight — #86735): if THIS process holds a native extension the sync
    # must rewrite, defer NOW — after the code swap, so only the dependency install is pending and the next
    # fresh launch completes it via the marker.
    _m()._abort_dependency_sync_if_self_locked(_windows_gateway_resume)
    _write_update_incomplete_marker()
    from hermes_cli.managed_uv import ensure_uv
    repair_uv = ensure_uv()
    # Venv gone entirely (repair interrupted after the old one was moved aside): recreate.
    venv_python_missing = not (
        venv_python_path(_m().PROJECT_ROOT / "venv", windows=_m()._is_windows())).exists()
    if venv_python_missing and repair_uv:
        print("→ Recreating virtual environment...")
        subprocess.run([repair_uv, "venv", "venv"], cwd=_m().PROJECT_ROOT, check=False)
    repair_prefix, repair_env = _pip_install_prefix(repair_uv)
    _m()._install_python_dependencies_with_optional_fallback(repair_prefix, env=repair_env, group="all")
    _m()._refresh_active_lazy_features(repair_prefix, env=repair_env, features=active_lazy_features)
    _m()._restore_active_tool_dependencies(active_tool_dependencies, repair_prefix, env=repair_env)
    # Core ``.[all]`` install finished. Clear the generic core breadcrumb before the lazy-refresh phase —
    # that phase uses its own marker so a later lazy failure cannot be "healed" by clearing the core marker
    # based on a narrow 7-package import probe (#58004 review).
    _m()._clear_update_incomplete_marker()
    healthy_after, detail_after = _venv_core_imports_healthy()
    if not healthy_after:
        print(f"⚠ Venv still unhealthy after repair: {detail_after}")
        print("  Close all Hermes windows/gateways and re-run: hermes update")
        return False
    print("✓ Dependencies repaired!")
    # Check for config migrations (#91360).
    _check_and_apply_config_migration(
        assume_yes=assume_yes, gateway_mode=gateway_mode, pre_update_snapshot_id=pre_update_snapshot_id)
    # The hand-off child never reaches the commits-pulled rebuild; do it here.
    if _rebuild_desktop_after_update(desktop_dir, had_desktop_app_before_update=had_desktop_app_before_update):
        return _print_verified_update_completion("✓ Update complete!")
    _print_update_completion(
        "⚠ Update partially complete — the desktop app was not rebuilt and is still on the previous build.")
    return False


def _pip_install_prefix(uv_bin) -> tuple[list[str], dict | None]:
    """``(install prefix, env)``: ``uv pip`` isolated from third-party UV env vars (so a foreign
    UV_PYTHON_INSTALL_DIR can't hijack it), else ``sys.executable -m pip`` (avoids PEP 668 errors)."""
    if uv_bin:
        # Same third-party UV-env isolation as the main update path (#83914): a user-level
        # UV_PYTHON_INSTALL_DIR / UV_PYTHON from unrelated software must not steer which interpreter uv
        # resolves here.
        # See #83914.
        from hermes_cli.managed_uv import managed_python_env
        env = managed_python_env()
        env["VIRTUAL_ENV"] = str(_m().PROJECT_ROOT / "venv")
        return [uv_bin, "pip"], env
    return [sys.executable, "-m", "pip"], None


def _repair_current_checkout(
    *, assume_yes, gateway_mode, pre_update_snapshot_id, desktop_dir,
    had_desktop_app_before_update, active_lazy_features, active_tool_dependencies,
    upstream_checked, _windows_gateway_resume) -> bool:
    """Already-up-to-date path: keep the managed runtime current, repair a broken venv.
    Returns whether the checkout can be reported complete."""
    # "No new commits" != safe interpreter: uv can keep the same CPython patch while
    # python-build-standalone refreshes the embedded SQLite; keep the boundary hook here too.
    from hermes_cli.managed_uv import ensure_uv, update_managed_uv
    runtime_repairs = []
    update_managed_uv(repair_observer=runtime_repairs.append)
    ensure_uv(repair_observer=runtime_repairs.append)
    runtime_repaired = next((result for result in runtime_repairs if result.repaired), None)

    # A current checkout does NOT imply a healthy install (a prior sync may have died
    # partway, e.g. Windows locked .pyd); probe or "Already up to date!" hides a bricked venv.
    healthy, detail = _venv_core_imports_healthy()
    # The Windows shim hand-off child is current BY DESIGN; its one job is the pending sync,
    # not venv health — without this it would print "Already up to date!" and skip it.
    handed_off_sync = os.environ.get(_m()._UPDATE_REEXEC_ENV) == "1"
    if handed_off_sync:
        print("→ Finishing the dependency install handed off by hermes.exe...")
    elif not healthy:
        print("⚠ Checkout is current, but the venv is unhealthy:")
        print(f"  {detail}")
        print("→ Repairing Python dependencies...")
    if handed_off_sync or not healthy:
        current_checkout_complete = _repair_venv_on_current_checkout(
            assume_yes=assume_yes, gateway_mode=gateway_mode,
            pre_update_snapshot_id=pre_update_snapshot_id, desktop_dir=desktop_dir,
            had_desktop_app_before_update=had_desktop_app_before_update,
            active_lazy_features=active_lazy_features,
            active_tool_dependencies=active_tool_dependencies,
            _windows_gateway_resume=_windows_gateway_resume)
    else:
        current_checkout_complete = _repair_node_deps_on_current_checkout(
            _print_verified_update_completion, assume_yes=assume_yes, gateway_mode=gateway_mode,
            pre_update_snapshot_id=pre_update_snapshot_id,
            completion_message=(
                "✓ Already up to date!" if upstream_checked
                else "✓ Up to date with your fork (official repo not checked)."),
            had_desktop_app_before_update=had_desktop_app_before_update)
    if runtime_repaired is not None and not _m()._is_windows():
        print()
        print("⚠ Restart required to finish the managed Python runtime repair.")
        print(
            "  Any running Hermes gateways, Desktop backends, or other "
            "long-lived processes still use the previous runtime.")
        print("  Restart each of them to pick up the repaired runtime.")
    return current_checkout_complete


def _reconcile_diverged_checkout(git_cmd, branch: str, pre_pull_sha) -> None:
    """Fast-forward failed: merge on a custom branch (local commits survive) or reset --hard on the
    same branch (rescue ref first when histories share no ancestor). ``sys.exit(1)`` on failure."""
    # A custom branch (local commits atop origin/<branch>) also can't ff, and reset --hard
    # would discard that work: merge instead, stop on conflict.
    _cur_branch = (_git_run(git_cmd, ["branch", "--show-current"]).stdout or "").strip()
    if _cur_branch and _cur_branch != branch:
        print(
            f"  ⚠ Checkout is on custom branch '{_cur_branch}' — "
            f"merging origin/{branch} instead of resetting so local commits survive...")
        # Best-effort safety tag as a recovery anchor.
        _git_run(git_cmd, ["tag", f"pre-update-{_time.strftime('%Y%m%d-%H%M%S')}"])
        if _git_run(git_cmd, ["merge", "--no-edit", f"origin/{branch}"]).returncode != 0:
            _git_run(git_cmd, ["merge", "--abort"])
            print("✗ Merge conflict between local commits and upstream — update stopped, nothing was changed.")
            print(f"  Resolve manually: cd {_m().PROJECT_ROOT} && git merge origin/{branch}")
            print("  Then re-run the update. Local work is untouched.")
            sys.exit(1)
        return
    # Same branch: a true upstream force-push/rebase; local changes are stashed, so reset.
    # Orphan divergence (no common ancestor: corrupted HEAD, re-init) would lose the whole
    # local graph, so park pre_pull_sha behind a rescue ref first.
    merge_base_result = _git_run(git_cmd, ["merge-base", "HEAD", f"origin/{branch}"])
    has_common_ancestor = merge_base_result.returncode == 0 and merge_base_result.stdout.strip()
    if not has_common_ancestor and pre_pull_sha:
        from datetime import datetime as _dt, timezone
        # SHA suffix so two updates in the same second get distinct refs.
        rescue_ref = (
            f"refs/hermes-update-backups/orphan-{branch}-"
            f"{_dt.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{pre_pull_sha[:12]}")
        head = f"  ⚠ Local history shares no common ancestor with origin/{branch} (orphan divergence) — "
        if _git_run(git_cmd, ["update-ref", rescue_ref, pre_pull_sha]).returncode == 0:
            print(
                f"{head}backed up current HEAD to {rescue_ref} before resetting. "
                f"This backup expires after {_ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days.")
        else:
            # update-ref failure is intentionally non-fatal, but never claim a backup exists.
            print(
                f"{head}attempted to back up current HEAD to {rescue_ref} before resetting, "
                f"but the backup write failed (pre-reset SHA was {pre_pull_sha}).")
        _prune_orphan_rescue_refs(git_cmd, _m().PROJECT_ROOT, branch)
    print("  ⚠ Fast-forward not possible (history diverged), resetting to match remote...")
    reset_result = _git_run(git_cmd, ["reset", "--hard", f"origin/{branch}"])
    if reset_result.returncode != 0:
        print(f"✗ Failed to reset to origin/{branch}.")
        if reset_result.stderr.strip():
            print(f"  {reset_result.stderr.strip()}")
        print(f"  Try manually: git fetch origin && git reset --hard origin/{branch}")
        sys.exit(1)


def _rollback_if_pulled_syntax_error(git_cmd, pre_pull_sha) -> None:
    """Post-pull syntax guard: roll back to *pre_pull_sha* and ``sys.exit(1)`` when a critical
    file no longer compiles (a bad admin-merge past CI must not brick the CLI)."""
    syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(_m().PROJECT_ROOT)
    if syntax_ok:
        return
    print()
    print("✗ Pulled code has a syntax error in a critical file:")
    print(f"  {failing_path}")
    # py_compile errors can be multi-line; show enough for the SyntaxError text.
    for line in str(syntax_error).splitlines()[:6] if syntax_error else ():
        print(f"    {line}")
    print()
    if pre_pull_sha:
        print(f"→ Rolling back to {pre_pull_sha[:10]}...")
        rollback_result = _git_run(git_cmd, ["reset", "--hard", pre_pull_sha])
        if rollback_result.returncode == 0:
            print("  ✓ Rollback complete — your install is unchanged.")
            print("  Try ``hermes update`` again later once a fix lands.")
        else:
            print("  ✗ Rollback failed. Recover manually with:")
            print(f"    cd {_m().PROJECT_ROOT} && git reset --hard {pre_pull_sha}")
            if rollback_result.stderr.strip():
                print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
    else:
        print("  Could not capture pre-pull SHA — recover manually with:")
        print(f"    cd {_m().PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>")
    sys.exit(1)


def _pull_updates(
    git_cmd, branch, auto_stash_ref, *, prompt_for_restore, gw_input_fn, discard_local_changes,
    keep_stash):
    """Fast-forward onto ``origin/<branch>`` and settle the autostash. Divergence by shape:
    custom branch -> merge, same branch -> reset, orphan history -> rescue ref first; a
    post-pull syntax error in a critical file rolls back. Exits on failure; returns pre-pull SHA."""
    update_succeeded = False
    # Pre-pull SHA for auto-rollback (stray conflict markers once bricked every updater).
    # Capture the pre-pull SHA so we can auto-roll-back if the new code has a syntax error in a
    # critical-path file (PR #28452 incident: orphan merge-conflict markers in hermes_cli/config.py bricked
    # every user who ran ``hermes update`` for the 7 minutes between the bad commit and the fix landing).
    pre_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
    try:
        # merge --ff-only the already-fetched ref instead of `git pull`, which would do a
        # SECOND network fetch; identical in effect given the fresh tracking ref.
        if _git_run(git_cmd, ["merge", "--ff-only", f"origin/{branch}"]).returncode != 0:
            _reconcile_diverged_checkout(git_cmd, branch, pre_pull_sha)
        _rollback_if_pulled_syntax_error(git_cmd, pre_pull_sha)
        update_succeeded = True
    finally:
        if auto_stash_ref is not None:
            # No stash restore if the update failed — tree state is unknown.
            if not update_succeeded:
                print(f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})")
                print("  Restore manually with: git stash apply")
            elif discard_local_changes:
                # Non-interactive + updates.non_interactive_local_changes: discard.
                _m()._discard_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref)
            elif keep_stash:
                # --keep-stash (desktop updater): leave edits parked rather than re-apply silently.
                _m()._park_stashed_changes(auto_stash_ref)
            else:
                _m()._restore_stashed_changes(
                    git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=prompt_for_restore,
                    input_fn=gw_input_fn)
    return pre_pull_sha


@dataclass
class _CheckoutPlan:
    """What the pre-pull checkout phase decided (see ``_prepare_checkout_for_update``)."""

    auto_stash_ref: "str | None"
    commit_count: int
    in_place_update: bool
    parked_branch_switched: bool
    prompt_for_restore: bool
    switch_block_reason: "str | None"
    upstream_checked: bool


def _apply_parked_branch_guard(
    git_cmd, branch, current_branch, *, switch_branch, _windows_gateway_resume
) -> tuple[bool, bool, "str | None"]:
    """Decide how a checkout parked on another branch is brought to *branch* (stash-switch-pull-
    switch-back used to "update" main while the running code stayed behind).

    By branch contents + updates.parked_branch_strategy: fully merged -> switch back;
    unmerged -> "switch" (default; loud "kept" notice) or "update_in_place" (merge origin/<target>
    INTO the branch, checkout never moves; --switch-branch overrides once); dirty/unverifiable ->
    touch nothing, warn, ``sys.exit(1)`` with the code update SKIPPED (also when the target is
    missing). Returns ``(parked_branch_switched, in_place_update, switch_block_reason)``.
    """
    if current_branch == branch or current_branch == "HEAD":
        return False, False, None
    switch_safe, switch_block_reason = _m()._assess_parked_branch_switch(
        git_cmd, _m().PROJECT_ROOT, current_branch, branch)
    if not switch_safe:
        _m()._print_parked_branch_skip_warning(
            git_cmd, _m().PROJECT_ROOT, current_branch, branch, switch_block_reason)
        print()
        print(f"⚠ Update finished — code update SKIPPED{_branch_head_suffix(git_cmd, _m().PROJECT_ROOT)}")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)
    if not switch_block_reason.startswith("unmerged:"):
        print(f"  ⚠ Checkout was parked on '{current_branch}' (fully merged) — switching back to {branch}...")
        return True, False, switch_block_reason
    _in_place_configured = False
    with _best_effort('Could not read updates.parked_branch_strategy: %s'):
        _in_place_configured = (
            _updates_config().get("parked_branch_strategy", "switch") == "update_in_place")
    if not _in_place_configured or switch_branch:
        _m()._print_parked_branch_kept_notice(
            current_branch, branch, switch_block_reason.split(":", 1)[1])
        return True, False, switch_block_reason
    # --branch typos used to surface via the checkout failing, which this path skips.
    if _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", f"origin/{branch}"]).returncode != 0:
        print(f"✗ Branch '{branch}' does not exist locally or on origin.")
        sys.exit(1)
    print(
        f"  ℹ On branch '{current_branch}' — updating it in place from "
        f"origin/{branch} (no branch switch; local commits preserved).")
    return False, True, switch_block_reason


def _prepare_checkout_for_update(
    git_cmd, branch, current_branch, *, is_fork, assume_yes, gateway_mode, gw_input_fn,
    switch_branch, _windows_gateway_resume):
    """Parked-branch guard, land on the target, stash, count new commits. Exits when the
    checkout is unsafe to move or the target is missing. ``commit_count`` is 0 when up to
    date, -1 when tips differ but the shallow count is unrecoverable."""
    parked_branch_switched, in_place_update, switch_block_reason = _apply_parked_branch_guard(
        git_cmd, branch, current_branch, switch_branch=switch_branch,
        _windows_gateway_resume=_windows_gateway_resume)

    if not in_place_update and current_branch == "HEAD" != branch:
        print(f"  ⚠ Currently on detached HEAD — switching to {branch} for update...")
    auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)
    if (
        not in_place_update and current_branch != branch
        and _git_run(git_cmd, ["checkout", branch]).returncode != 0):
        track_result = _git_run(git_cmd, ["checkout", "-B", branch, f"origin/{branch}"])
        if track_result.returncode != 0:
            # Restore the stash before bailing so the user isn't stranded.
            if auto_stash_ref is not None:
                _m()._restore_stashed_changes(
                    git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=False, input_fn=gw_input_fn)
            print(f"✗ Branch '{branch}' does not exist locally or on origin.")
            if track_result.stderr.strip():
                print(f"  {track_result.stderr.strip().splitlines()[0]}")
            sys.exit(1)

    prompt_for_restore = (
        auto_stash_ref is not None
        and not assume_yes
        and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty())))

    # On shallow checkouts `rev-list --count` can report the entire remote ancestry. The
    # zero/nonzero gate is still sound; treat the shallow NUMBER as unknown and recover it
    # via the GitHub compare API when possible.
    result = _git_run(git_cmd, ["rev-list", f"HEAD..origin/{branch}", "--count"], check=True)
    commit_count = int(result.stdout.strip())

    apply_is_shallow = _is_shallow_checkout(git_cmd)
    if commit_count > 0 and apply_is_shallow:
        from hermes_cli.banner import _github_compare_behind
        counted = _github_compare_behind(*_tip_shas(git_cmd, f"origin/{branch}"))
        # counted == 0 means local-ahead: falls through to the up-to-date path.
        commit_count = counted if counted is not None else -1

    # A fork can match origin yet trail upstream, so the sync can move HEAD with
    # commit_count == 0; detect that BEFORE the no-update return so deps, restarts AND the
    # fleet matrix still run (it used to live in the early-return branch and verified nothing).
    # The sync can therefore advance HEAD even though the origin comparison found no commits. Detect that
    # BEFORE taking the no-update return so dependency refreshes, gateway restarts, AND the fleet version
    # matrix still run for the pulled code (#73108 — previously the sync lived inside the commit_count == 0
    # branch, which returns immediately after: an update that pulled hundreds of upstream commits printed
    # "Already up to date!" and verified nothing). Non-fork checkouts have no upstream question: origin IS
    # the official repo, so "Already up to date!" is fully verified there.
    upstream_checked = True
    if commit_count == 0 and is_fork and branch == "main":
        pre_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        upstream_checked = _m()._sync_with_upstream_if_needed(
            git_cmd, _m().PROJECT_ROOT, assume_yes=assume_yes, input_fn=gw_input_fn)
        post_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        if pre_sync_sha and post_sync_sha and pre_sync_sha != post_sync_sha:
            synced_count = _count_commits_between(
                git_cmd, _m().PROJECT_ROOT, pre_sync_sha, post_sync_sha)
            # HEAD moving is proof of an update even if the count can't be read.
            commit_count = max(1, synced_count)

    return _CheckoutPlan(
        auto_stash_ref=auto_stash_ref, commit_count=commit_count, in_place_update=in_place_update,
        parked_branch_switched=parked_branch_switched, prompt_for_restore=prompt_for_restore,
        switch_block_reason=switch_block_reason, upstream_checked=upstream_checked)


@dataclass
class _UpdateOptions:
    """Resolved ``hermes update`` inputs (flags, config, pre-update snapshots)."""

    active_lazy_features: object
    active_tool_dependencies: object
    pre_update_version: object
    gw_input_fn: object
    assume_yes: bool
    keep_stash: bool
    switch_branch: bool
    discard_local_changes: bool


def _desktop_app_present(desktop_dir: Path) -> bool:
    """Return whether a packaged or source Desktop build exists."""
    return (
        _m()._desktop_packaged_executable(desktop_dir) is not None
        or _m()._desktop_dist_exists(desktop_dir)
    )


def _rebuild_desktop_after_update(
    desktop_dir: Path, *, had_desktop_app_before_update: bool
) -> bool:
    """Rebuild an installed Desktop app when its source or artifact changed.

    Returns ``False`` only when a rebuild was attempted and failed, so the
    caller can withhold ``✓ Update complete!`` and (in gateway mode) write
    a failing ``.update_exit_code`` (#88251). Every other outcome — nothing
    to rebuild, up to date, build succeeded, Desktop never installed —
    returns ``True``.
    """
    # The release tree is ignored by git and can disappear during an update.
    # Its pre-update presence is enough to restore it; do not make people who
    # have never used Desktop pay for an Electron build.
    has_desktop_app = had_desktop_app_before_update or _desktop_app_present(desktop_dir)
    if not (
        (desktop_dir / "package.json").exists()
        and _m()._resolve_node_runtime_npm()
        and has_desktop_app
    ):
        return True

    print("→ Checking if desktop app needs rebuilding...")
    # Consult the content-hash stamp IN-PROCESS first. The spawned
    # `hermes desktop --build-only` subprocess re-imports the whole CLI stack
    # (~1-3 s) just to reach the same _m()._desktop_build_needed check; when
    # the stamp already says "up to date" we can skip the spawn entirely. The
    # update path never passes --source, so the subprocess would run with
    # source_mode=False — mirror that here. Any error in the pre-check falls
    # through to the subprocess.
    skip_desktop_build = False
    try:
        skip_desktop_build = not _m()._desktop_build_needed(
            desktop_dir, _m().PROJECT_ROOT, source_mode=False
        )
    except Exception:
        skip_desktop_build = False
    if skip_desktop_build:
        print("  ✓ Desktop app up to date")
        return True

    desktop_build_cmd = [sys.executable, "-m", "hermes_cli.main", "desktop", "--build-only"]
    # Capture the (very loud) Electron/vite build output into update.log
    # instead of streaming it to the terminal. On the rare nonzero exit,
    # retry once after waiting again for the venv — this covers a
    # still-settling rebuild window the first wait didn't fully catch — then
    # surface the captured tail so the failure is debuggable.
    #
    # Start the build subprocess with the Hermes-managed Node on PATH: when
    # `hermes update` runs inside the desktop updater chain (Desktop →
    # hermes-setup → hermes update), the shell PATH customizations are lost,
    # so a bare-PATH child would fail with `node: not found` before cmd_gui can
    # self-heal.
    from hermes_constants import with_hermes_node_path

    build_env = with_hermes_node_path()
    build_result = _m()._run_logged_subprocess(
        desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=build_env
    )
    if build_result.returncode != 0:
        build_result = _m()._run_logged_subprocess(
            desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=build_env
        )
    if build_result.returncode != 0:
        print("  ⚠ Desktop build failed (run `hermes desktop` to retry)")
        tail = "\n".join((build_result.stdout or "").strip().splitlines()[-15:])
        if tail:
            print(tail)
        from hermes_constants import display_hermes_home as _dhh

        print(f"  Full build log: {_dhh()}/logs/update.log")
        return False
    print("  ✓ Desktop app up to date")
    return True



def _serialize_update_plan(plan):
    if plan is None:
        return None
    try:
        return plan.to_dict()
    except Exception:
        return None


def _deserialize_update_plan(payload):
    if not isinstance(payload, dict):
        return None
    from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan

    values = dict(payload)
    values["runtimes"] = [
        RuntimeRecord(**item) if isinstance(item, dict) else item
        for item in values.get("runtimes", [])
    ]
    return UpdatePlan(**values)


def _post_update_restart_worker(payload_path: str) -> int:
    """Run post-update restart/verification in one fresh module graph."""
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    status_path = payload.get("status_path")
    import hermes_cli.update_receipt as update_receipt

    receipt_data = payload.get("receipt_data")
    if isinstance(receipt_data, dict):
        receipt = update_receipt.UpdateReceipt()
        receipt.data = receipt_data
        update_receipt._current = receipt

    exit_code = 0
    try:
        _run_post_update_restart(
            gateway_mode=bool(payload.get("gateway_mode")),
            node_failures=payload.get("node_failures") or [],
            _pre_update_plan=_deserialize_update_plan(payload.get("pre_update_plan")),
            _windows_gateway_resume=payload.get("windows_gateway_resume"),
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except BaseException:
        logger.exception("Fresh post-update restart worker failed")
        exit_code = 1
    finally:
        if update_receipt._current is not None:
            update_receipt.finalize_pending_update_receipt(
                exit_code, stop_reason="fresh post-update restart worker"
            )
        if status_path:
            try:
                Path(status_path).write_text(
                    json.dumps(
                        {
                            "worker_completed": True,
                            "exit_code": exit_code,
                            "receipt_handoff_complete": (
                                receipt_data is None or update_receipt._current is None
                            ),
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
    return exit_code

    # On Windows, abort early if another hermes.exe is holding the venv shim
    # open. Continuing would result in a string of WinError 32 warnings and
    # then either a deferred-rename leftover or a failed git-pull fast path
    # that silently falls back to the slower ZIP route. See issue #26670.
    #
    # Exception (#37039): when every concurrent instance is a gateway
    # runtime, the pause machinery a few lines below
    # (``_pause_windows_gateways_for_update``) stops it before any file
    # mutation, and the post-update restart phase brings it back. Aborting
    # just to make the user run the same kill manually is friction without
    # benefit. Anything not positively identified as a gateway (TUI shell,
    # Desktop backend child, unreadable cmdline) still aborts exactly as
    # before.
    if _m()._is_windows() and not getattr(args, "force", False):
        scripts_dir = _m()._venv_scripts_dir()
        if scripts_dir is not None:
            concurrent = _m()._detect_concurrent_hermes_instances(scripts_dir)
            if concurrent:
                non_gateway = _m()._filter_non_gateway_concurrent_instances(
                    concurrent
                )
                if non_gateway:
                    print(
                        _format_concurrent_instances_message(
                            non_gateway, scripts_dir
                        )
                    )
                    sys.exit(2)

def _run_post_update_restart_in_fresh_process(
    *, gateway_mode: bool, node_failures, _pre_update_plan, _windows_gateway_resume
) -> None:
    """Transfer post-update restart ownership to a new Python interpreter."""
    import tempfile

    import hermes_cli.update_receipt as update_receipt
    from tools.environments.local import build_subprocess_env

    receipt_data = None
    if update_receipt._current is not None:
        receipt_data = update_receipt._current.data
    payload = {
        "gateway_mode": bool(gateway_mode),
        "node_failures": list(node_failures or []),
        "pre_update_plan": _serialize_update_plan(_pre_update_plan),
        "windows_gateway_resume": _windows_gateway_resume,
        "receipt_data": receipt_data,
    }
    payload_path = None
    status_path = None
    completed = None
    worker_status = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="hermes-update-restart-",
            suffix=".json", delete=False
        ) as handle:
            status_path = f"{handle.name}.status"
            payload["status_path"] = status_path
            json.dump(payload, handle, default=str)
            payload_path = handle.name
        worker = (
            "from hermes_cli.update_cmd import _post_update_restart_worker; "
            "import sys; raise SystemExit(_post_update_restart_worker(sys.argv[1]))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", worker, payload_path],
            cwd=_m().PROJECT_ROOT,
            env=build_subprocess_env(
                scrub_secrets=False,
                inherit_profile_home=False,
            ),
            check=False,
        )
        if status_path is not None:
            try:
                worker_status = json.loads(
                    Path(status_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                worker_status = None
    finally:
        if payload_path is not None:
            try:
                Path(payload_path).unlink()
            except OSError:
                pass
        if status_path is not None:
            try:
                Path(status_path).unlink()
            except OSError:
                pass

    acknowledged = bool(
        isinstance(worker_status, dict)
        and worker_status.get("worker_completed") is True
        and worker_status.get("receipt_handoff_complete") is True
    )
    returncode = completed.returncode if completed is not None else 1
    if acknowledged:
        # The worker owns and finalized the transferred receipt. Prevent the
        # old command-boundary safety net from writing a duplicate.
        update_receipt._current = None
    else:
        # Worker import/payload failures happen before its receipt try/finally.
        # Preserve the parent's receipt rather than manufacturing completion.
        update_receipt.finalize_pending_update_receipt(
            returncode or 1,
            stop_reason="fresh post-update restart worker did not acknowledge handoff",
        )

    if returncode or not acknowledged:
        if gateway_mode:
            _write_gateway_update_exit_code(False)
        raise SystemExit(returncode or 1)


def _run_post_update_restart(
    *, gateway_mode: bool, node_failures, _pre_update_plan, _windows_gateway_resume
) -> None:
    gateway_fleet_restart_incomplete = False
    # Snapshot of gateways running before we touch anything. Stays empty
    # until we successfully import the probe and are about to stop/drain —
    # so an exception raised before we touch any gateway keeps this empty
    # (nothing to fail closed on), while a failure after we have stopped a
    # discovered gateway lets the handler fail closed on an empty survivor
    # probe rather than reporting a clean update (#78574).
    _pre_restart_gateway_pids: list | None = []
    # Declared outside the restart try/except below (and never reset
    # to None) so it's always safe to read afterwards even if that
    # block raises before reaching its own restart bookkeeping —
    # needed to forward already-restarted units to
    # ``_finish_dashboard_update_cleanup`` (review on #83595).
    restarted_services: list = []
    # Same outside-the-try treatment: the post-restart fleet version
    # check consults killed_pids to decide whether to wait for
    # freshly-restarted gateways to settle, and the phase's except
    # path forwards it to the update receipt.
    killed_pids: set = set()

    # Auto-restart every gateway in this fresh post-update interpreter.
    # All imports below therefore resolve from one coherent checkout.
    try:
        from hermes_cli.gateway import (
            is_macos,
            supports_systemd_services,
            _ensure_user_systemd_env,
            find_gateway_pids,
            find_profile_gateway_processes,
            _prepare_profile_gateway_update_restart,
            _get_service_pids,
            _graceful_restart_via_sigusr1,
            _wait_for_gateway_exit,
        )
        import signal as _signal

        def _wait_for_service_active(
            scope_cmd_: list,
            svc_name_: str,
            timeout: float = 10.0,
        ) -> bool:
            """Poll ``systemctl is-active`` until the unit reports active.

            systemd's Stopped -> Started transition after a graceful exit
            (or a hard restart) is not instantaneous; a one-shot check
            races that window and falsely reports the unit as down.
            Poll every 0.5s up to ``timeout`` seconds before giving up.
            """
            deadline = _time.monotonic() + max(timeout, 0.5)
            while True:
                try:
                    _verify = subprocess.run(
                        scope_cmd_ + ["is-active", svc_name_],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=5,
                    )
                    if _verify.stdout.strip() == "active":
                        return True
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                if _time.monotonic() >= deadline:
                    return False
                _time.sleep(0.5)

        def _service_restart_sec(
            scope_cmd_: list,
            svc_name_: str,
            default: float = 0.0,
        ) -> float:
            """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

            After a graceful exit-75, systemd waits ``RestartSec`` before
            respawning the unit.  Callers that poll for ``is-active``
            must use a timeout >= ``RestartSec`` + transition slack, or
            they'll give up *during* the cooldown window and wrongly
            conclude the unit didn't relaunch.
            """
            try:
                _show = subprocess.run(
                    scope_cmd_
                    + [
                        "show",
                        svc_name_,
                        "--property=RestartUSec",
                        "--value",
                    ],
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return default
            raw = (_show.stdout or "").strip()
            # systemd emits values like "30s", "100ms", "1min 30s", or
            # "infinity".  Parse conservatively; on any miss return default.
            if not raw or raw == "infinity":
                return default
            total = 0.0
            matched = False
            for part in raw.split():
                for _suf, _mult in (
                    ("ms", 0.001),
                    ("us", 0.000001),
                    ("min", 60.0),
                    ("s", 1.0),
                ):
                    if part.endswith(_suf):
                        try:
                            total += float(part[: -len(_suf)]) * _mult
                            matched = True
                        except ValueError:
                            pass
                        break
            return total if matched else default

        _manage_cmd_cache: dict = {}

        def _resolve_manage_cmd(scope_: str, scope_cmd_: list, svc_name_: str):
            """Resolve the command prefix for manage-units operations.

            Read-only systemctl calls (``is-active``, ``show``,
            ``list-units``) work unprivileged, but manage-units verbs
            (``reset-failed``, ``start``, ``restart``) on a *system*
            service trigger a polkit ``org.freedesktop.systemd1.manage-units``
            authentication prompt when run as a non-root user.  That
            interactive prompt runs inside our captured subprocess with a
            10-15s timeout — the user sees the prompt flash and "exit
            directly" before they can answer, and the resulting
            TimeoutExpired used to be swallowed silently.

            Strategy: if root, plain systemctl.  If not root, try
            non-interactive sudo (``sudo -n``) — first a blanket probe,
            then a targeted ``systemctl reset-failed`` probe so a
            least-privilege sudoers entry scoped to
            ``systemctl ... hermes-gateway*`` also qualifies
            (``reset-failed`` is an idempotent no-op we run before every
            privileged restart anyway).  If neither works, return None —
            the caller must SKIP the restart (without draining the
            gateway first!) and tell the user how to restart manually.
            ``--no-ask-password`` guarantees polkit can never hang a
            captured subprocess on this path.
            """
            if scope_ in _manage_cmd_cache:
                return _manage_cmd_cache[scope_]
            cmd = scope_cmd_ + ["--no-ask-password"]
            if (
                scope_ == "system"
                and hasattr(os, "geteuid")
                and os.geteuid() != 0  # windows-footgun: ok — systemd path, Linux-only
            ):
                sudo_cmd = ["sudo", "-n"] + scope_cmd_ + ["--no-ask-password"]
                sudo_ok = False
                try:
                    _probe = subprocess.run(
                        ["sudo", "-n", "true"],
                        capture_output=True,
                        timeout=5,
                    )
                    sudo_ok = _probe.returncode == 0
                    if not sudo_ok:
                        # Blanket sudo refused — a targeted sudoers entry
                        # (NOPASSWD for systemctl ... hermes-gateway*)
                        # may still allow the exact commands we need.
                        _probe = subprocess.run(
                            sudo_cmd + ["reset-failed", svc_name_],
                            capture_output=True,
                            timeout=5,
                        )
                        sudo_ok = _probe.returncode == 0
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    sudo_ok = False
                cmd = sudo_cmd if sudo_ok else None
            _manage_cmd_cache[scope_] = cmd
            return cmd

        # Wait budget for graceful SIGUSR1 restarts.  In-band restart
        # may defer stop() until active turns finish
        # (``restart_after_turn_timeout``, #77184) and then spend up to
        # ``restart_drain_timeout`` inside stop(). Cover both phases so
        # we don't fall back to a hard kill while the gateway is still
        # patiently waiting for the requesting turn. On older systemd
        # units without SIGUSR1 wiring this wait just times out and we
        # fall back to ``systemctl restart`` (the old behaviour).
        try:
            from hermes_cli.gateway import _get_restart_exit_wait_budget

            _drain_budget = max(float(_get_restart_exit_wait_budget()), 45.0)
        except Exception:
            _drain_budget = 45.0

        failed_or_stale_units = []
        killed_pids = set()
        relaunched_profiles = []
        externally_supervised_profiles = []

        # Record which gateways are running before any stop/drain, so a
        # later failure that leaves the survivor probe empty can still be
        # recognised as "a running gateway was stopped and did not come
        # back" rather than "nothing was running" (#78574). Best-effort:
        # if the probe itself raises, leave the snapshot as-is (the
        # survivor probe's own None result already fails closed).
        try:
            _pre_restart_gateway_pids = list(find_gateway_pids(all_profiles=True))
        except Exception:
            _pre_restart_gateway_pids = None

        # --- Systemd services (Linux) ---
        # Discover all hermes-gateway* units (default + profiles) plus
        # hermes-serve* units (the Desktop app's backend, #83438).
        if supports_systemd_services():
            try:
                _ensure_user_systemd_env()
            except Exception:
                pass

            for scope, scope_cmd in [
                ("user", ["systemctl", "--user"]),
                ("system", ["systemctl"]),
            ]:
                try:
                    result = subprocess.run(
                        scope_cmd
                        + [
                            "list-units",
                            "hermes-gateway*",
                            "hermes-serve*",
                            "--plain",
                            "--no-legend",
                            "--no-pager",
                        ],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=10,
                    )
                except FileNotFoundError:
                    continue
                except subprocess.TimeoutExpired as exc:
                    # Discovery timeout — skip this scope, keep the other.
                    print(
                        f"  ⚠ systemctl timed out listing {scope}-scope "
                        f"gateway units ({exc.cmd if exc.cmd else 'unknown command'}). "
                        f"Check the gateway with: hermes gateway status"
                    )
                    continue

                def _restart_one_systemd_gateway_unit(svc_name: str) -> None:
                    # Check if active
                    check = subprocess.run(
                        scope_cmd + ["is-active", svc_name],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=5,
                    )
                    if check.stdout.strip() != "active":
                        return

                    # Resolve how we may run manage-units verbs
                    # (reset-failed/start/restart) for this scope.
                    # None ⇒ no non-interactive privilege path; we
                    # must avoid those verbs entirely or polkit will
                    # throw an interactive auth prompt inside our
                    # captured 10-15s subprocess (the user sees it
                    # flash and "exit directly" — reported June 2026).
                    _manage_cmd = _resolve_manage_cmd(
                        scope, scope_cmd, svc_name
                    )

                    # Prefer a graceful SIGUSR1 restart so in-flight
                    # agent runs drain instead of being SIGKILLed.
                    # The gateway's SIGUSR1 handler calls
                    # request_restart(via_service=True) → drain →
                    # exit; systemd's Restart=always respawns the unit.
                    # hermes-serve has no such handler (it isn't
                    # gateway/run.py), so skip straight to the blunt
                    # restart below rather than sending it an unhandled
                    # signal and waiting out the drain budget for
                    # nothing.
                    _main_pid = 0
                    if _service_unit_supports_graceful_sigusr1_restart(svc_name):
                        try:
                            _show = subprocess.run(
                                scope_cmd
                                + [
                                    "show",
                                    svc_name,
                                    "--property=MainPID",
                                    "--value",
                                ],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=5,
                            )
                            _main_pid = int((_show.stdout or "").strip() or 0)
                        except (
                            ValueError,
                            subprocess.TimeoutExpired,
                            FileNotFoundError,
                        ):
                            _main_pid = 0

                    _graceful_ok = False
                    if _main_pid > 0:
                        from hermes_cli.gateway import (
                            GATEWAY_LOOP_WEDGED,
                            _escalate_wedged_gateway,
                            probe_gateway_loop_liveness,
                        )

                        if (
                            probe_gateway_loop_liveness(_main_pid)
                            == GATEWAY_LOOP_WEDGED
                        ):
                            # Loop-liveness probe says the gateway's event
                            # loop is provably dead (#81642): SIGUSR1 can
                            # never drain it, so waiting the full budget
                            # (180s default) only wedges the update too.
                            # Bounded escalation (SIGTERM grace → SIGKILL,
                            # ~10s) then restart the unit. A busy gateway
                            # keeps a fresh heartbeat and never takes this
                            # path — its drain (incl. the #86684 cron
                            # floor) is untouched.
                            print(
                                f"  ⚠ {svc_name}: gateway event loop is "
                                "unresponsive — skipping drain, forcing "
                                "a bounded stop..."
                            )
                            _escalate_wedged_gateway(_main_pid)
                            _graceful_ok = True
                        else:
                            print(
                                f"  → {svc_name}: draining (up to {int(_drain_budget)}s)..."
                            )
                            _graceful_ok = _graceful_restart_via_sigusr1(
                                _main_pid,
                                drain_timeout=_drain_budget,
                            )

                    if _graceful_ok:
                        # Gateway exited after a planned restart.
                        # ``Restart=always`` means systemd WILL respawn
                        # the unit — but only after
                        # ``RestartSec`` (default 60s on our unit
                        # file). That 60s wait is a crash-loop guard,
                        # and is the right default when the gateway
                        # dies unexpectedly. For a voluntary restart
                        # on update, it's dead time the user watches.
                        #
                        # Shortcut it: ``reset-failed`` + ``start``
                        # skips RestartSec entirely (we're manually
                        # initiating the unit, not waiting for
                        # systemd's auto-restart logic). Takes about
                        # as long as the process takes to come up
                        # (~1-3s on a warm box).
                        #
                        # If the unit is already active because
                        # RestartSec elapsed while we were draining,
                        # ``start`` is a no-op and we fall through to
                        # the poll below. Either way we collapse the
                        # 60s+ delay to a ~5s one.
                        #
                        # The shortcut needs manage-units privileges.
                        # Without them (system service, non-root, no
                        # passwordless sudo) skip it — systemd's own
                        # auto-restart still relaunches the unit after
                        # RestartSec, no privileges required.
                        if _manage_cmd is not None:
                            subprocess.run(
                                _manage_cmd + ["reset-failed", svc_name],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=10,
                            )
                            subprocess.run(
                                _manage_cmd + ["start", svc_name],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=15,
                            )
                            # Short poll: the gateway should be up
                            # within a few seconds now that we
                            # bypassed RestartSec.
                            if _wait_for_service_active(
                                scope_cmd,
                                svc_name,
                                timeout=10.0,
                            ):
                                restarted_services.append(svc_name)
                                return
                        # Passive poll: systemd's auto-restart fires
                        # after RestartSec regardless of privileges.
                        # This is the primary path when _manage_cmd is
                        # None, and the fallback when the explicit
                        # start didn't take.
                        _restart_sec = _service_restart_sec(
                            scope_cmd,
                            svc_name,
                            default=0.0,
                        )
                        _post_drain_timeout = max(
                            10.0,
                            _restart_sec + 10.0,
                        )
                        if _manage_cmd is None and _restart_sec > 5.0:
                            print(
                                f"  → {svc_name}: waiting for systemd "
                                f"auto-restart (~{int(_restart_sec)}s; "
                                "no root for an immediate restart)..."
                            )
                        if _wait_for_service_active(
                            scope_cmd,
                            svc_name,
                            timeout=_post_drain_timeout,
                        ):
                            restarted_services.append(svc_name)
                            return
                        # Process exited but wasn't respawned (older
                        # unit without Restart=on-failure or
                        # RestartForceExitStatus=75).  Fall through
                        # to systemctl start/restart.
                        print(
                            f"  ⚠ {svc_name} drained but didn't relaunch — forcing restart"
                        )

                    # Forcing a restart requires manage-units
                    # privileges.  Without a non-interactive path,
                    # running systemctl here would spawn a polkit
                    # auth prompt inside a captured 10-15s subprocess
                    # — it flashes and dies before the user can
                    # answer.  Skip with clear instructions instead.
                    if _manage_cmd is None:
                        failed_or_stale_units.append(svc_name)
                        print(
                            f"  ⚠ {svc_name} is a system service and restarting it needs root.\n"
                            f"    Restart it manually to load the new version:\n"
                            f"      sudo systemctl restart {svc_name}\n"
                            f"    To let `hermes update` restart it automatically, allow\n"
                            f"    passwordless sudo for systemctl, or run updates with sudo."
                        )
                        return

                    # Fallback: blunt systemctl restart.  This is
                    # what the old code always did; we get here only
                    # when the graceful path failed (unit missing
                    # SIGUSR1 wiring, drain exceeded the budget,
                    # restart-policy mismatch).
                    #
                    # Always `reset-failed` first.  If systemd's own
                    # auto-restart attempts already parked the unit
                    # in a failed state (transient CHDIR / OOM /
                    # filesystem race after our drain + exit-75),
                    # a plain `systemctl restart` can wedge against
                    # the RestartSec backoff and leave the unit
                    # dead.  Clearing the failed state first makes
                    # the restart idempotent.  Mirrors the recovery
                    # path in `hermes gateway restart`
                    # (`systemd_restart()`) as of PR #20949.
                    subprocess.run(
                        _manage_cmd + ["reset-failed", svc_name],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=10,
                    )
                    restart = subprocess.run(
                        _manage_cmd + ["restart", svc_name],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=15,
                    )
                    if restart.returncode == 0:
                        # Verify the service actually survived the
                        # restart.  systemctl restart returns 0 even
                        # if the new process crashes immediately.
                        if _wait_for_service_active(
                            scope_cmd,
                            svc_name,
                            timeout=10.0,
                        ):
                            restarted_services.append(svc_name)
                        else:
                            # Retry once — transient startup failures
                            # (stale module cache, import race) often
                            # resolve on the second attempt.  Again
                            # clear any failed state first so the
                            # retry isn't blocked by the previous
                            # crash.
                            print(
                                f"  ⚠ {svc_name} died after restart, retrying..."
                            )
                            subprocess.run(
                                _manage_cmd + ["reset-failed", svc_name],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=10,
                            )
                            subprocess.run(
                                _manage_cmd + ["restart", svc_name],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=15,
                            )
                            if _wait_for_service_active(
                                scope_cmd,
                                svc_name,
                                timeout=10.0,
                            ):
                                restarted_services.append(svc_name)
                                print(f"  ✓ {svc_name} recovered on retry")
                            else:
                                failed_or_stale_units.append(svc_name)
                                _scope_flag = "--user " if scope == "user" else ""
                                _sudo_hint = "sudo " if scope == "system" else ""
                                print(
                                    f"  ✗ {svc_name} failed to stay running after restart.\n"
                                    f"    Check logs: {_sudo_hint}journalctl {_scope_flag}-u {svc_name} --since '2 min ago'\n"
                                    f"    Recover manually:\n"
                                    f"      {_sudo_hint}systemctl {_scope_flag}reset-failed {svc_name}\n"
                                    f"      {_sudo_hint}systemctl {_scope_flag}restart {svc_name}"
                                )
                    else:
                        failed_or_stale_units.append(svc_name)
                        print(
                            f"  ⚠ Failed to restart {svc_name}: {restart.stderr.strip()}"
                        )

                def _on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
                    # Isolate the timeout to this unit and keep going
                    # (#68523). A scope-wide handler used to abort every
                    # later gateway and leave the fleet on mixed code.
                    failed_or_stale_units.append(svc_name)
                    print(
                        f"  ⚠ systemctl timed out restarting {svc_name} "
                        f"({exc.cmd if exc.cmd else 'unknown command'}); "
                        f"continuing with remaining gateways"
                    )

                _for_each_systemd_gateway_unit(
                    result.stdout,
                    process_unit=_restart_one_systemd_gateway_unit,
                    on_unit_timeout=_on_unit_timeout,
                )

        # --- Launchd services (macOS) ---
        # Restart EVERY ai.hermes.gateway* LaunchAgent, not only the
        # invoking profile's — parity with the systemd branch above
        # (#41403). Per-label TimeoutExpired isolation happens inside.
        if is_macos():
            try:
                _restart_macos_launchd_gateways(
                    restarted_services,
                    failed_or_stale_units,
                    _drain_budget,
                )
            except (FileNotFoundError, ImportError):
                pass

        # --- Manual (non-service) gateways ---
        # Kill any remaining gateway processes not managed by a service.
        # Exclude PIDs that belong to just-restarted services so we don't
        # immediately kill the process that systemd/launchd just spawned.
        service_pids = _get_service_pids(all_profiles=True)
        manual_pids = find_gateway_pids(
            exclude_pids=service_pids, all_profiles=True
        )
        profile_processes = {
            proc.pid: proc
            for proc in find_profile_gateway_processes(exclude_pids=service_pids)
            if proc.pid in manual_pids
        }
        # Profile gateways we could not arm a relaunch for.  These must
        # NOT be left running: their modules are the pre-update ones and
        # every lazy import from here on mixes versions against the new
        # code on disk (#88654).  Handing them to the unmapped sweep
        # below stops them and surfaces them in the "Stopped N manual
        # gateway process(es) / Restart manually" summary, which is the
        # contract already used for gateways with no profile mapping.
        unrestartable_pids = set()
        for pid, proc in profile_processes.items():
            restart_mode = _prepare_profile_gateway_update_restart(
                proc.profile, pid
            )
            if restart_mode is None:
                # Previously a bare ``continue``: the gateway was neither
                # relaunched nor stopped nor mentioned, so it kept serving
                # from stale modules with no operator signal at all.
                print(
                    f"  ⚠ {proc.profile}: could not arm an automatic "
                    f"gateway restart for PID {pid} — stopping it instead "
                    "so it cannot keep running pre-update code"
                )
                unrestartable_pids.add(pid)
                continue
            # Prefer a graceful SIGUSR1 drain so in-flight agent runs
            # finish before the watcher respawns the gateway.  If the
            # gateway doesn't support SIGUSR1 or doesn't exit within
            # the drain budget, fall back to SIGTERM — the watcher
            # still sees the exit and relaunches either way.
            # Announce the drain first: this wait can hold for the full
            # budget per gateway with no other output, and on surfaces
            # that stream update progress (the desktop updater most of
            # all) the silence reads as a hung update (#44515).
            print(
                f"  → {proc.profile}: draining gateway PID {pid} "
                f"(up to {int(_drain_budget)}s)..."
            )
            from hermes_cli.gateway import (
                GATEWAY_LOOP_WEDGED,
                _escalate_wedged_gateway,
                probe_gateway_loop_liveness,
            )

            if probe_gateway_loop_liveness(pid) == GATEWAY_LOOP_WEDGED:
                # Loop-liveness probe: this gateway's event loop is
                # provably dead (#81642) — SIGUSR1/SIGTERM shutdown can
                # never run, so the drain wait would burn the full budget
                # and stall the update. Bounded stop instead (SIGTERM
                # grace → SIGKILL, ~10s). A busy-but-alive gateway keeps
                # a fresh heartbeat and never takes this branch, so live
                # drains (incl. the #86684 cron floor) are unaffected.
                print(
                    f"  ⚠ {proc.profile}: gateway event loop is "
                    "unresponsive — skipping drain, forcing a bounded stop..."
                )
                _escalate_wedged_gateway(pid)
                drained = True
            else:
                drained = _graceful_restart_via_sigusr1(
                    pid,
                    drain_timeout=_drain_budget,
                )
            if not drained:
                try:
                    os.kill(pid, _signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            # Wait for the old process to fully exit before the watcher
            # spawns the new gateway.  Telegram holds the previous
            # getUpdates long-poll session open on its servers for up to
            # ~30s after the client disconnects.  If the new gateway
            # connects before that window expires it receives a 409
            # Conflict, which _handle_polling_conflict() recovers from
            # via back-off retries — but a brief wait here reduces the
            # chance of hitting that path at all, especially on fast
            # machines where the watcher loop restarts in < 1s.
            # We wait up to 5s for the process to exit (the OS-level
            # close, not the Telegram server-side expiry), then let the
            # watcher take over.  The Telegram adapter's retry logic
            # handles any remaining 409s if the server session is still
            # live when the new gateway polls.
            _wait_for_gateway_exit(timeout=5.0, force_after=None)
            killed_pids.add(pid)
            if restart_mode == "external-supervisor":
                externally_supervised_profiles.append(proc.profile)
            else:
                relaunched_profiles.append(proc.profile)

        for pid in manual_pids:
            if pid in profile_processes and pid not in unrestartable_pids:
                continue
            try:
                os.kill(pid, _signal.SIGTERM)
                killed_pids.add(pid)
            except (ProcessLookupError, PermissionError):
                pass

        if restarted_services or killed_pids:
            print()
            for svc in restarted_services:
                print(f"  ✓ Restarted {svc}")
            if relaunched_profiles:
                names = ", ".join(relaunched_profiles)
                print(f"  ✓ Restarting manual gateway profile(s): {names}")
            if externally_supervised_profiles:
                names = ", ".join(externally_supervised_profiles)
                print(
                    "  ✓ Handed gateway profile(s) back to their external "
                    f"supervisor: {names}"
                )
            unmapped_count = (
                len(killed_pids)
                - len(relaunched_profiles)
                - len(externally_supervised_profiles)
            )
            if unmapped_count:
                print(f"  → Stopped {unmapped_count} manual gateway process(es)")
                print("    Restart manually: hermes gateway run")
                if unmapped_count > 1:
                    print(
                        "    (or: hermes -p <profile> gateway run  for each profile)"
                    )

        if failed_or_stale_units:
            gateway_fleet_restart_incomplete = True
            if gateway_mode:
                _exit_code_path = get_hermes_home() / ".update_exit_code"
                try:
                    _exit_code_path.write_text("1", encoding="utf-8")
                except OSError:
                    pass
        _warn_incomplete_gateway_fleet_restart(failed_or_stale_units)

        try:
            from hermes_cli.update_receipt import record_gateway_restart

            record_gateway_restart(
                restarted_services=restarted_services,
                relaunched_profiles=relaunched_profiles,
                externally_supervised_profiles=externally_supervised_profiles,
                killed_pids=sorted(killed_pids),
                failed_units=failed_or_stale_units,
                incomplete=bool(failed_or_stale_units),
            )
        except Exception:
            pass

        if not restarted_services and not killed_pids:
            # No gateways were running — nothing to do
            pass

        # --- Post-restart survivor sweep -----------------------------
        # Issue #17648: some gateways ignore SIGTERM (stuck drain,
        # blocked I/O, PID dead but zombie).  The detached profile
        # watchers wait 120s for the old PID to exit — if it never
        # does, no respawn happens and the user keeps hitting
        # ImportError against a stale sys.modules.  Give the
        # graceful paths a brief window to complete, then SIGKILL
        # any remaining pre-update PIDs so the watcher / service
        # manager can relaunch with fresh code.
        try:
            _time.sleep(3.0)
            _service_pids_after = _get_service_pids(all_profiles=True)
            _surviving = find_gateway_pids(
                exclude_pids=_service_pids_after,
                all_profiles=True,
            )
            # Scope to PIDs we already tried to kill during this
            # update (killed_pids).  Anything new is a gateway that
            # started AFTER our restart attempt — respecting user
            # intent, we don't kill those.
            _stuck = [pid for pid in _surviving if pid in killed_pids]
            if _stuck:
                print()
                print(
                    f"  ⚠ {len(_stuck)} gateway process(es) ignored SIGTERM — force-killing"
                )
                from gateway.status import terminate_pid as _terminate_pid
                for pid in _stuck:
                    try:
                        # Routes through taskkill /T /F on Windows,
                        # SIGKILL on POSIX — _signal.SIGKILL doesn't
                        # exist on Windows so the old raw os.kill call
                        # used to crash the entire update path.
                        _terminate_pid(pid, force=True)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                # Give the OS a beat to reap the processes so the
                # watchers see them exit and respawn.
                _time.sleep(1.5)
        except Exception as _sweep_exc:
            logger.debug("Post-restart survivor sweep failed: %s", _sweep_exc)

    except Exception as e:
        logger.debug("Gateway restart during update failed: %s", e)
        # An exception escaping the whole phase means the drain/restart
        # output the user relies on never printed. Don't let that pass for
        # a clean update: surface it and treat the fleet as stale unless we
        # can positively prove no gateway is running (#78574).
        #
        # A positive-empty ``_surviving`` is only proof-of-safety when
        # nothing was running before we touched anything. If a gateway was
        # discovered pre-restart and none survive now, it was stopped and
        # its replacement was never verified — the same fail-open contract
        # this fix closes — so we must still fail closed on ``[]``.
        _surviving = _surviving_gateway_pids_after_failed_restart()
        if _restart_phase_failure_is_incomplete(
            _surviving, _pre_restart_gateway_pids
        ):
            gateway_fleet_restart_incomplete = True
            _warn_gateway_restart_phase_aborted(e, _surviving)
            if gateway_mode:
                _exit_code_path = get_hermes_home() / ".update_exit_code"
                try:
                    _exit_code_path.write_text("1", encoding="utf-8")
                except OSError:
                    pass
        try:
            from hermes_cli.update_receipt import record_gateway_restart

            record_gateway_restart(
                restarted_services=restarted_services,
                incomplete=gateway_fleet_restart_incomplete,
                phase_error=str(e),
            )
        except Exception:
            pass

    _m()._resume_windows_gateways_after_update(_windows_gateway_resume)

    # Warn if legacy Hermes gateway unit files are still installed.
    # When both hermes.service (from a pre-rename install) and the
    # current hermes-gateway.service are enabled, they SIGTERM-fight
    # for the same bot token (see PR #11909). Flagging here means
    # every `hermes update` surfaces the issue until the user migrates.
    try:
        from hermes_cli.gateway import (
            has_legacy_hermes_units,
            _find_legacy_hermes_units,
            supports_systemd_services,
        )

        if supports_systemd_services() and has_legacy_hermes_units():
            print()
            print("⚠ Legacy Hermes gateway unit(s) detected:")
            for name, path, is_sys in _find_legacy_hermes_units():
                scope = "system" if is_sys else "user"
                print(f"    {path}  ({scope} scope)")
            print()
            print("  These pre-rename units (hermes.service) fight the current")
            print("  hermes-gateway.service for the bot token and cause SIGTERM")
            print("  flap loops. Remove them with:")
            print()
            print("    hermes gateway migrate-legacy")
            print()
            print("  (add `sudo` if any are in system scope)")
    except Exception as e:
        logger.debug("Legacy unit check during update failed: %s", e)

    # Restart a managed dashboard through systemd, or stop stale manual
    # dashboard processes. Raw-killing a systemd-owned dashboard PID makes
    # systemd treat it as a clean stop, leaving the Cloudflare origin dead.
    # Preserve the safety rule above: a failed Node refresh leaves the
    # currently running dashboard untouched.
    #
    # Forward the systemd units restarted above (includes hermes-serve*,
    # #83438) so a Serve-only install's freshly restarted process isn't
    # found and restarted again below (review on #83595).
    _finish_dashboard_update_cleanup(
        node_failures, already_restarted_units=set(restarted_services)
    )

    print()
    print("Tip: You can now select a provider and model:")
    print("  hermes model              # Select provider and model")

    # Phase 1 (#91277): post-update fleet version verification. Compare
    # every live gateway's stamped code_sha against the freshly-updated
    # checkout and surface any gateway still serving pre-update code —
    # instead of assuming the restart phase worked (#88654, #69754).
    _fleet_snapshot: list = []
    try:
        from hermes_cli.update_receipt import (
            collect_fleet_versions,
            print_fleet_version_matrix,
        )

        _fleet_rows_expected = _m()._fleet_probe_expected_runtimes(
            _pre_update_plan,
            _pre_restart_gateway_pids,
            _windows_gateway_resume,
            restarted_services,
            killed_pids,
        )
        # A freshly restarted or detached-resumed gateway may need time to
        # publish gateway_state.json. Poll only when a row-capable pre-update
        # signal says a fleet row is expected; otherwise an empty fleet is a
        # valid idle state.
        if _fleet_rows_expected:
            _fleet_deadline = _time.monotonic() + 30.0
            while True:
                _time.sleep(2.0)
                _fleet_snapshot = collect_fleet_versions(
                    pre_restart_pids=_pre_restart_gateway_pids
                )
                if _fleet_snapshot and not any(
                    row.get("state") == "down" for row in _fleet_snapshot
                ):
                    break
                if _time.monotonic() >= _fleet_deadline:
                    break
        else:
            _fleet_snapshot = collect_fleet_versions(
                pre_restart_pids=_pre_restart_gateway_pids
            )
        if print_fleet_version_matrix(_fleet_snapshot):
            gateway_fleet_restart_incomplete = True
        elif not _fleet_snapshot and _fleet_rows_expected:
            print(
                "\n⚠ Fleet version check returned no rows even though"
                " gateway runtimes were expected — verification incomplete."
            )
            gateway_fleet_restart_incomplete = True
    except Exception as _fleet_exc:
        logger.debug("Fleet version verification failed: %s", _fleet_exc)

    # Plan-vs-execution reconciliation (#91277 Phase 2, restart via
    # declared mechanism): every runtime the PLAN saw must be accounted
    # for by the restart phase's bookkeeping. An unaccounted runtime is
    # the silent-miss class (a platform branch re-discovered its own
    # targets and skipped one the inventory knew about) — escalate it
    # exactly like a STALE/DOWN fleet row.
    _runtime_outcomes: list = []
    try:
        if _pre_update_plan is not None and _pre_update_plan.runtimes:
            from hermes_cli.update_inventory import (
                match_runtime_outcomes,
                report_unaccounted_runtimes,
            )

            _runtime_outcomes = match_runtime_outcomes(
                _pre_update_plan,
                restarted_services=restarted_services,
                relaunched_profiles=relaunched_profiles,
                externally_supervised_profiles=externally_supervised_profiles,
                killed_pids=killed_pids,
                failed_units=failed_or_stale_units,
            )
            if report_unaccounted_runtimes(_runtime_outcomes):
                gateway_fleet_restart_incomplete = True
            try:
                import hermes_cli.update_receipt as _ur

                if _ur._current is not None:
                    _ur._current.data["runtime_outcomes"] = _runtime_outcomes
            except Exception:
                pass
    except Exception as _outcome_exc:
        logger.debug("Runtime-outcome reconciliation failed: %s", _outcome_exc)

    try:
        from hermes_cli.update_receipt import finalize_update_receipt

        _receipt_path = finalize_update_receipt(
            "partial" if gateway_fleet_restart_incomplete else "success",
            fleet=_fleet_snapshot,
        )
        if _receipt_path is not None:
            logger.info("Update receipt written: %s", _receipt_path)
    except Exception as _receipt_exc:
        logger.debug("Update receipt finalize failed: %s", _receipt_exc)

    if gateway_fleet_restart_incomplete:
        # Code update itself succeeded, but at least one gateway still
        # runs pre-update modules — surface that as a failed update so
        # automation / operators do not treat the fleet as healthy.
        sys.exit(1)
    _clear_fleet_restart_pending_marker()


def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always
    restore stdio even on ``sys.exit``."""
    # A managed-runtime refresh can replace site-packages before the normal
    # ``.[all]`` install runs. Snapshot while the old environment can still
    # prove which optional backends the user had activated.
    active_lazy_features = _m()._capture_active_lazy_features()
    active_tool_dependencies = _m()._capture_active_tool_dependencies()

    # Captured before any pull so the completion line can report the transition.
    # Snapshot the pre-update version before files are replaced so the completion line can report the
    # transition (prime-agent#630 port).
    # Snapshot the pre-update version before any code is pulled so the completion line can report the
    # transition (prime-agent#630 port).
    pre_update_version = _read_project_version()
    gw_input_fn = (
        (lambda prompt, default="": _gateway_prompt(prompt, default)) if gateway_mode else None)
    assume_yes = bool(getattr(args, "yes", False))
    # --keep-stash (desktop updater): never re-apply the autostash; only when an update
    # landed — abort/no-op paths still restore since the tree is unchanged.
    keep_stash = bool(getattr(args, "keep_stash", False))
    # --switch-branch: prefer switching over an in-place merge so an update never writes the
    # branch's history; only meaningful with parked_branch_strategy "update_in_place".
    # See #89507.
    switch_branch = bool(getattr(args, "switch_branch", False))

    # Interactive terminals always stash-and-ask; only non-interactive updates consult
    # updates.non_interactive_local_changes (auto-restore vs discard).
    discard_local_changes = False
    if gateway_mode or assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        # A config read failure must never change the safe default.
        with _best_effort("Could not read updates.non_interactive_local_changes: %s"):
            _mode = str(_updates_config().get("non_interactive_local_changes", "stash")).lower()
            discard_local_changes = _mode == "discard"
    return _UpdateOptions(
        active_lazy_features=active_lazy_features,
        active_tool_dependencies=active_tool_dependencies, pre_update_version=pre_update_version,
        gw_input_fn=gw_input_fn, assume_yes=assume_yes, keep_stash=keep_stash,
        switch_branch=switch_branch, discard_local_changes=discard_local_changes)


def _begin_update_receipt_and_plan(args):
    """Open the receipt, snapshot the fleet, refuse on Windows shim holders. Returns the
    pre-update plan (None if the probe failed); ``sys.exit(2)`` when a non-gateway hermes.exe
    holds the venv shim."""
    # Structured receipt: record what this run discovers/does/skips so silent failures are diagnosable.
    with _best_effort('Update receipt unavailable: %s'):
        # See #74973, #81193, #85753, #88848, #91277.
        from hermes_cli.update_receipt import begin_update_receipt
        begin_update_receipt()

    # Plan phase: snapshot runtimes/supervisors/version (read-only; probe failure records
    # nothing). Re-read AFTER the restart phase to reconcile — the plan is the worklist.
    # Plan phase (#91277 Phase 2): snapshot the pre-update fleet — every running Hermes runtime, its
    # supervisor, and its running code version — into the receipt, so a post-mortem can compare what the
    # update SAW against what it did. ``_pre_update_plan`` is read again AFTER the restart phase to
    # reconcile every planned runtime against the phase's bookkeeping (restart via declared mechanism — the
    # plan is the worklist, not just a printout).
    _pre_update_plan = None
    with _best_effort('Update plan phase failed: %s'):
        from hermes_cli.update_inventory import collect_runtime_inventory, record_plan_in_receipt
        _pre_update_plan = collect_runtime_inventory()
        record_plan_in_receipt(_pre_update_plan)
        if _pre_update_plan.runtimes:
            _n = len(_pre_update_plan.runtimes)
            _profiles = ", ".join(sorted({r.profile for r in _pre_update_plan.runtimes}))
            print(f"→ Fleet: {_n} running service(s) across profiles: {_profiles}")

    # On Windows, abort early if another hermes.exe is holding the venv shim
    # open. Continuing would result in a string of WinError 32 warnings and
    # then either a deferred-rename leftover or a failed git-pull fast path
    # that silently falls back to the slower ZIP route. See issue #26670.
    if _m()._is_windows() and not getattr(args, "force", False):
        scripts_dir = _m()._venv_scripts_dir()
        if scripts_dir is not None:
            concurrent = _m()._detect_concurrent_hermes_instances(scripts_dir)
            if concurrent:
                # Same #37039 exception as the gate above: abort only on
                # instances not positively identified as a gateway runtime.
                # Gateways are paused (and resumed post-update) a few lines
                # below; aborting here would blame PIDs the user need not
                # kill and block an otherwise safe update.
                non_gateway = _m()._filter_non_gateway_concurrent_instances(
                    concurrent
                )
                if non_gateway:
                    print(
                        _format_concurrent_instances_message(
                            non_gateway, scripts_dir
                        )
                    )
                    sys.exit(2)

    # Pre-update backup — runs before any git/file mutation so users can
    # always roll back to the exact state they had before this update.
    # Returns the quick-snapshot id (or None when disabled/failed); the
    # post-update cron-jobs safety net uses it to detect job loss.
    pre_update_snapshot_id = _m()._run_pre_update_backup(args)
    try:
        from hermes_cli.update_receipt import record_step

        record_step(
            "pre_update_backup",
            pre_update_snapshot_id is not None,
            f"snapshot={pre_update_snapshot_id}" if pre_update_snapshot_id else "disabled or failed",
        )
    except Exception:
        pass

    _windows_gateway_resume = _m()._pause_windows_gateways_for_update()
    if _windows_gateway_resume:
        import atexit as _atexit

        _atexit.register(
            _m()._resume_windows_gateways_after_update,
            _windows_gateway_resume,
        )

    # With gateways paused, anything still running from the venv interpreter
    # (most commonly the Desktop app's `hermes serve` backend) will keep .pyd
    # files locked and corrupt the dependency sync below. Refuse rather than
    # race: killing the desktop backend is futile (the app supervises and
    # respawns it), so the user must close the app. Deliberately NOT bypassed
    # by plain --force: the desktop bootstrap updater passes --force to skip
    # the hermes.exe shim guard above, but its lock probe only checks the shim
    # and app.asar — a non-desktop venv python holding a .pyd would sail
    # through and corrupt the sync (the exact failure this guard exists for).
    # --force-venv is the explicit escape hatch.
    if _m()._is_windows() and not getattr(args, "force_venv", False):
        _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            _gateway_holders = _m()._leftover_pausable_gateway_pids(_venv_holders)
            if _gateway_holders is not None:
                # Every remaining holder is a gateway the pause machinery
                # already owns — respawned by its supervisor inside the
                # pause→guard window, or up through a spawn path discovery
                # does not map. Stop them and re-check instead of
                # dead-ending; the post-update resume (and the supervisor
                # that respawned them) brings gateways back afterwards.
                from gateway.status import terminate_pid

                print(
                    f"  ⚠ {len(_gateway_holders)} gateway process(es) still "
                    "hold the venv after the pause; stopping them"
                )
                for _pid in _gateway_holders:
                    try:
                        terminate_pid(int(_pid), force=True)
                    except Exception as exc:
                        logger.debug(
                            "Could not stop leftover gateway %s: %s", _pid, exc
                        )
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            # Positive-identity rung (runs FIRST, any update context): holders
            # the spawn ledger proves are orphaned Hermes backends — the
            # process self-registered (pid, create_time, purpose, spawner) at
            # startup and its recorded spawner is provably dead. No PPID
            # archaeology, no hand-off contract required.
            _ledger_backends = _m()._ledger_reapable_backend_pids(_venv_holders)
            if _ledger_backends:
                print(
                    f"  ⚠ {len(_ledger_backends)} ledger-identified orphaned "
                    "Hermes backend process(es) hold the venv; stopping their trees"
                )
                _m()._stop_process_trees(_ledger_backends)
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            _orphan_backends = _m()._orphaned_desktop_backend_pids(_venv_holders)
            if _orphan_backends:
                # Every remaining holder is a Desktop `serve` backend whose
                # supervising app is GONE — the GUI-updater handoff race:
                # Electron's teardown lost the SIGTERM race, exited, and left
                # its backend (and any .hermes-runtime child) holding the
                # venv. Nothing will respawn an orphan, so reap the tree and
                # re-check instead of dead-ending with "Hermes is still
                # running" while no window is open. Backends whose Desktop
                # is still alive never reach here (_orphaned_desktop_
                # backend_pids returns None for them) — that path keeps the
                # refusal, because the app would just respawn what we kill.
                print(
                    f"  ⚠ {len(_orphan_backends)} orphaned Desktop backend "
                    "process(es) still hold the venv; stopping their trees"
                )
                _m()._stop_process_trees(_orphan_backends)
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            # Manual serve/dashboard rung (#63206): a network-bound
            # `hermes serve --host <ip>` powering a REMOTE Desktop holds the
            # venv and used to dead-end the update with exit 2 — the user's
            # only option was killing the backend by hand, and nothing ever
            # brought it back (the remote client's endpoint stayed dead).
            # Positive ledger identity only: self-registered serve/dashboard
            # whose recorded spawner is not alive (Desktop-owned backends
            # keep the refusal — the app respawns what we kill). Stop them,
            # and register an idempotent atexit relaunch built from the
            # ledger's structured host/port/profile so the endpoint comes
            # back on the SAME bind after the update — success or failure.
            _serve_entries = _m()._ledger_manual_serve_holders(_venv_holders)
            if _serve_entries:
                print(
                    f"  ⚠ {len(_serve_entries)} manual serve/dashboard "
                    "backend(s) hold the venv; stopping them for the update "
                    "(they will be relaunched on their recorded endpoints)"
                )
                _m()._stop_process_trees(
                    [int(e["pid"]) for e in _serve_entries]
                )
                _serve_resume_token = {
                    "pending": True,
                    "entries": _serve_entries,
                }
                try:
                    from hermes_cli.update_receipt import record_step

                    record_step(
                        "serve_pause",
                        True,
                        f"stopped={len(_serve_entries)}",
                    )
                except Exception:
                    pass
                import atexit as _serve_atexit

                _serve_atexit.register(
                    _m()._relaunch_stopped_serves, _serve_resume_token
                )
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            # Final rung before the dead-end: a GUI-updater hand-off
            # (`update --gateway --force` with the update-incomplete marker
            # claimed) means the Desktop is contractually gone and nothing
            # legitimate will respawn a `serve` backend from this venv. The
            # orphan-only reap above bails the instant ANY holder still has a
            # live parent — which stranded a whole swarm of per-profile
            # backends (the tearing-down Electron parent / the venv
            # launcher→worker chain still mid-exit) and hung the update. In
            # the hand-off context those surviving Hermes backends are leaks,
            # live parent or not — reap them by cmdline instead of dead-ending.
            _handoff = False
            try:
                _handoff = bool(getattr(args, "gateway", False)) and _m()._update_marker_path().exists()
            except Exception:
                _handoff = False
            # Fail closed: if we cannot positively verify the shim state
            # (scripts dir unresolvable, detection raised), assume a live
            # shim exists and keep refusing rather than reap.
            _no_live_shim = False
            try:
                _scripts_dir = _m()._venv_scripts_dir()
                if _scripts_dir is not None:
                    _no_live_shim = not _m()._detect_concurrent_hermes_instances(_scripts_dir)
            except Exception:
                _no_live_shim = False
            if _handoff and _no_live_shim:
                _handoff_backends = _m()._handoff_reapable_backend_pids(_venv_holders)
                if _handoff_backends:
                    print(
                        f"  ⚠ {len(_handoff_backends)} Hermes backend process(es) "
                        "still hold the venv after the Desktop hand-off; "
                        "stopping their trees"
                    )
                    _m()._stop_process_trees(_handoff_backends)
                    _time.sleep(1.0)
                    _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            print(_format_venv_python_holders_message(_venv_holders))
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
            sys.exit(2)
    return _pre_update_plan


def _prepare_git_command() -> tuple[bool, list, bool]:
    """Return ``(use_zip_update, git_cmd, is_fork)``; ``sys.exit(1)`` when not a git repo
    on a non-Windows host (Windows falls back to ZIP: broken git file I/O, AV, NTFS filters)."""
    git_dir = _m().PROJECT_ROOT / ".git"
    use_zip_update = not git_dir.exists()
    if use_zip_update and sys.platform != "win32":
        print("✗ Not a git repository. Please reinstall:")
        print("  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash")
        sys.exit(1)

    git_cmd = _base_git_cmd()
    if sys.platform == "win32" and git_dir.exists():
        _git_run(git_cmd, ["config", "windows.appendAtomically", "false"])
    # A broken Git-for-Windows trampoline refuses every call with a "BUG (fork bomb)" guard;
    # swap in a real binary up front so git survives instead of degrading to ZIP.
    # See #87876.
    git_cmd = _ensure_non_trampoline_git(git_cmd)

    # Before stash/branch logic: npm rewrites package-lock.json non-deterministically and
    # line-ending churn is machine-made dirt; both would otherwise force an autostash every update.
    _discard_lockfile_churn(git_cmd, _m().PROJECT_ROOT)
    _normalize_managed_eol(git_cmd, _m().PROJECT_ROOT)

    origin_url = _m()._get_origin_url(git_cmd, _m().PROJECT_ROOT)
    is_fork = _is_fork(origin_url)

    if is_fork:
        print("⚠ Updating from fork:")
        print(f"  {origin_url}")
        print()
    return use_zip_update, git_cmd, is_fork


def _verify_head_after_pull(
    git_cmd, branch: str, pre_pull_sha, *, in_place_update: bool, _windows_gateway_resume
) -> str | None:
    """Return the post-pull HEAD SHA; ``sys.exit(1)`` if the pull was a no-op or landed off-branch."""
    # A detached checkout pinned to a SHA can report "N new commit(s)" and a successful
    # merge --ff-only yet stay put; surface the no-op instead of claiming "Code updated!".
    # Verify HEAD actually moved (issue #79678). ``merge --ff-only`` succeeding only means the merge
    # completed, not that the update applied: a checkout that is pinned to a raw SHA (detached HEAD) can
    # report "N new commit(s)" against origin yet still sit on the old commit afterward (the branch-switch
    # step re-detaches to the SHA). Before this guard, ``hermes update`` printed "✓ Code updated!" and
    # reinstalled deps + rebuilt the desktop app against the stale tree — no error, no warning, ``hermes
    # doctor`` healthy. Compare pre-pull and post-pull HEAD; if they match, surface the no-op instead of
    # claiming success.
    post_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
    if pre_pull_sha and post_pull_sha == pre_pull_sha:
        print()
        print("✗ Code did not move — update was a no-op.")
        print(
            f"  HEAD is pinned to {pre_pull_sha[:10]} (detached checkout); "
            f"origin/{branch} advanced but the working tree stayed put.")
        print(
            "  Reattach to the branch and retry: "
            f"git -C {_m().PROJECT_ROOT} checkout {branch} && hermes update")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)

    # HEAD must be on the target or "Code updated!" is a lie; an IN-PLACE update is the one
    # legitimate exception (origin/<target> merged INTO the checked-out branch).
    post_pull_branch = _current_branch_name(git_cmd)
    if not in_place_update and post_pull_branch and post_pull_branch not in {branch, "HEAD"}:
        print()
        print(
            f"✗ Update pulled origin/{branch}, but the checkout is on "
            f"'{post_pull_branch}' — not claiming success.")
        print(
            "  Switch to the target branch and retry: "
            f"git -C {_m().PROJECT_ROOT} checkout {branch} && hermes update")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)
    return post_pull_sha


def _current_branch_name(git_cmd, *, check: bool = False) -> str:
    """``rev-parse --abbrev-ref HEAD`` (literal "HEAD" when detached)."""
    return _git_run(git_cmd, ["rev-parse", "--abbrev-ref", "HEAD"], check=check).stdout.strip()


def _handle_update_called_process_error(
    e, args, gateway_mode: bool, had_desktop_app_before_update: bool) -> None:
    """Git/installer failure: ZIP-fallback when safe, else report and ``sys.exit(1)``."""
    stage = _format_update_failure_stage(e)
    if _should_zip_fallback_on_update_error(e):
        print(f"⚠ {stage}: {e}")
        print("→ Falling back to ZIP download...")
        print()
        desktop_build_ok = _update_via_zip(
            args, had_desktop_app_before_update=had_desktop_app_before_update)
        if gateway_mode:
            _write_gateway_update_exit_code(desktop_build_ok)
    else:
        print(f"✗ {stage}: {e}")
        _print_called_process_error_tail(e)
        if _called_process_error_is_python_dep_install(e):
            print(
                "  The git update already finished. Re-downloading the source "
                "ZIP cannot fix a dependency install error and would overwrite local files.")
            if _m()._is_windows():
                print("  Retry through the venv interpreter:")
                print(
                    '    venv\\Scripts\\python.exe -c '
                    '"from hermes_cli.main import main; main()" update --yes')
        _finalize_receipt("failed", 'Update receipt finalize failed: %s')
        sys.exit(1)


def _finalize_receipt(status: str, debug_message: str) -> None:
    """Best-effort ``finalize_update_receipt(status)``; the receipt must never break an update."""
    with _best_effort(debug_message):
        from hermes_cli.update_receipt import finalize_update_receipt
        finalize_update_receipt(status)


def _finish_already_up_to_date(
    git_cmd, branch: str, current_branch: str, _plan, *, assume_yes: bool, gateway_mode: bool,
    gw_input_fn, pre_update_snapshot_id, desktop_dir, had_desktop_app_before_update: bool,
    active_lazy_features, active_tool_dependencies, _windows_gateway_resume) -> None:
    """"Already up to date" path: restore stash/branch, repair the checkout, catch up the fleet.
    ``sys.exit(1)`` when the repair is incomplete (after gateway exit code + partial receipt)."""
    _invalidate_update_cache()

    # Restore stash and switch back if we moved. EXCEPTION: a parked branch verified clean +
    # fully merged stays on the target — re-parking on the stale branch recreates the incident.
    if _plan.auto_stash_ref is not None:
        _m()._restore_stashed_changes(
            git_cmd, _m().PROJECT_ROOT, _plan.auto_stash_ref, prompt_user=_plan.prompt_for_restore,
            input_fn=gw_input_fn)
    if _plan.parked_branch_switched:
        if _plan.switch_block_reason.startswith("unmerged:"):
            _count = _plan.switch_block_reason.split(":", 1)[1]
            print(
                f"  ✓ Checkout was parked on '{current_branch}' — switched back to {branch}; "
                f"{_count} unmerged commit(s) kept on '{current_branch}'.")
        else:
            print(f"  ✓ Checkout was parked on '{current_branch}' (fully merged) — switched back to {branch}.")
    elif current_branch not in {branch, "HEAD"}:
        _git_run(git_cmd, ["checkout", current_branch])

    current_checkout_complete = _repair_current_checkout(
        assume_yes=assume_yes, gateway_mode=gateway_mode,
        pre_update_snapshot_id=pre_update_snapshot_id, desktop_dir=desktop_dir,
        had_desktop_app_before_update=had_desktop_app_before_update,
        active_lazy_features=active_lazy_features,
        active_tool_dependencies=active_tool_dependencies, upstream_checked=_plan.upstream_checked,
        _windows_gateway_resume=_windows_gateway_resume)
    _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
    # A prior pull may still owe the fleet a restart; catch up here too, BEFORE the exit
    # gate so a partial outcome can't strand the fleet on stale code.
    # Catch up even on the "Already up to date" path — that early return is what left the gateway on stale
    # code for two days. Runs BEFORE the runtime-verification exit gate below: a vulnerable SQLite runtime
    # demotes the outcome to partial, but must not strand the fleet on stale code (#91277 fleet contract —
    # the pending-restart check always executes).
    _apply_pending_fleet_restart_catchup()
    if not current_checkout_complete:
        if gateway_mode:
            _write_gateway_update_exit_code(False)
        _finalize_receipt("partial", 'Update receipt finalize (current checkout) failed: %s')
        sys.exit(1)


def _apply_pulled_update(
    git_cmd, branch, pre_pull_sha, _plan, opts, *, gateway_mode, is_fork, desktop_dir,
    had_desktop_app_before_update, pre_update_snapshot_id, _pre_update_plan,
    _windows_gateway_resume) -> None:
    """Post-pull phase: verify HEAD, sync Python/Node/web/Desktop, maintenance, fleet restart."""
    _invalidate_update_cache()
    post_pull_sha = _verify_head_after_pull(
        git_cmd, branch, pre_pull_sha, in_place_update=_plan.in_place_update,
        _windows_gateway_resume=_windows_gateway_resume)

    # Gateways still serve pre-pull modules until the restart phase; an interrupt before a
    # completed restart leaves this marker so the next update catches up even when git is
    # current. Distinct from ``.update-incomplete`` (venv/install repair).
    # See #95294.
    _write_fleet_restart_pending_marker(expected_sha=post_pull_sha or "")
    # Stale .pyc would ImportError on gateway restart when new source references new names.
    _sweep_bytecode_after_update(branch)

    if is_fork and branch == "main":
        _m()._sync_with_upstream_if_needed(
            git_cmd, _m().PROJECT_ROOT, assume_yes=opts.assume_yes, input_fn=opts.gw_input_fn)

    # .[all], falling back to base + extras individually so one broken extra doesn't strip
    # the rest; the ownership preflight refuses first on foreign-owned (sudo-pip) venv files.
    _sync_python_dependencies_after_pull(
        git_cmd, branch, pre_pull_sha, active_lazy_features=opts.active_lazy_features,
        active_tool_dependencies=opts.active_tool_dependencies,
        _windows_gateway_resume=_windows_gateway_resume)

    node_failures = _update_node_dependencies()
    _m()._build_web_ui(_m().PROJECT_ROOT / "web")
    desktop_build_ok = _rebuild_desktop_after_update(
        desktop_dir, had_desktop_app_before_update=had_desktop_app_before_update)

    print()
    print(f"✓ Code updated!{_branch_head_suffix(git_cmd, _m().PROJECT_ROOT)}")

    update_complete = _run_post_update_maintenance(
        assume_yes=opts.assume_yes, gateway_mode=gateway_mode,
        pre_update_snapshot_id=pre_update_snapshot_id,
        had_desktop_app_before_update=had_desktop_app_before_update,
        node_failures=node_failures, desktop_build_ok=desktop_build_ok,
        pre_update_version=opts.pre_update_version)

    # Exit code *before* the restart: under --gateway this process lives in the gateway's
    # systemd cgroup and the systemctl-restart fallback SIGKILLs it (KillMode=mixed), so
    # the marker would never land and the new gateway's watcher would time out spuriously.
    if gateway_mode:
        _write_gateway_update_exit_code(update_complete)

    _restart = _restart_gateway_fleet_after_update(_pre_update_plan, gateway_mode)
    _resume_windows_gateways_and_merge_outcome(_restart, _windows_gateway_resume, gateway_mode)
    _verify_fleet_after_update(
        _restart, _pre_update_plan=_pre_update_plan, _windows_gateway_resume=_windows_gateway_resume,
        node_failures=node_failures, update_complete=update_complete)


def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always restore stdio even on
    ``sys.exit``. Self-lock deferral deliberately does NOT run here (pre-fetch it stranded users
    on the OLD checkout in an exit-2 loop); it runs right before the dependency sync."""
    opts = _resolve_update_options(args, gateway_mode)
    gw_input_fn, assume_yes = opts.gw_input_fn, opts.assume_yes

    print("⚕ Updating Hermes Agent...")
    print()

    _pre_update_plan = _begin_update_receipt_and_plan(args)

    # Backup before any git/file mutation; the snapshot id (None if disabled/failed) feeds
    # the post-update cron-jobs safety net.
    pre_update_snapshot_id = _m()._run_pre_update_backup(args)
    _record_update_step(
        "pre_update_backup", pre_update_snapshot_id is not None,
        f"snapshot={pre_update_snapshot_id}" if pre_update_snapshot_id else "disabled or failed")

    _windows_gateway_resume = _m()._pause_windows_gateways_for_update()
    if _windows_gateway_resume:
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, _windows_gateway_resume)

    # Any venv python still running (typically the Desktop `hermes serve` backend) keeps .pyd
    # locked and would corrupt the sync; refuse rather than race (the app respawns a killed
    # backend). NOT bypassed by --force (desktop updater, shim guard only); --force-venv is.
    if _m()._is_windows() and not getattr(args, "force_venv", False):
        _clear_windows_venv_holders_or_exit(args, gateway_mode, _windows_gateway_resume)

    # After every fail-closed venv guard, before either path can remove the release tree.
    # Self-lock deferral moved: the venv-holder sweep above excludes this process by design (a CLI `hermes
    # update` IS the venv python), and an updater that has imported a native venv extension cannot rewrite
    # its own mapped .pyd (#83569). That check used to run HERE — before the fetch — but firing pre-fetch
    # meant a deferral stranded the user on the OLD checkout, and any startup path that eagerly loaded
    # cryptography turned every Windows update into an exit-2 loop (#86735/#86780/#86781). It now runs via
    # _abort_dependency_sync_if_self_locked() after the code swap, immediately before the dependency sync —
    # the only phase the lock can actually break — and only when the sync would truly rewrite the loaded
    # distribution.
    desktop_dir = _m().PROJECT_ROOT / "apps" / "desktop"
    had_desktop_app_before_update = _desktop_app_present(desktop_dir)

    use_zip_update, git_cmd, is_fork = _prepare_git_command()

    if use_zip_update:
        try:
            desktop_build_ok = _update_via_zip(
                args, had_desktop_app_before_update=had_desktop_app_before_update)
        finally:
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        if gateway_mode:
            _write_gateway_update_exit_code(desktop_build_ok)
        return

    try:
        # Scoped fetch: a bare `git fetch origin` pulls thousands of branches and can stall.
        branch = _m()._resolve_update_branch(args)

        # Self-heal abandoned .git/*.lock files (crashed fetch) or the fetch fails "File exists".
        from hermes_cli.gitlock import clear_stale_git_locks, clear_stale_tmp_packs
        cleared = clear_stale_git_locks(_m().PROJECT_ROOT)
        if cleared:
            print("  (removed stale git lock(s): %s)" % ", ".join(cleared))
        swept = clear_stale_tmp_packs(_m().PROJECT_ROOT)
        if swept:
            print("  (removed %d aborted-fetch pack temp file(s))" % len(swept))

        # Surface autostashes left by earlier updates (--keep-stash, failed restores).
        # Surface autostash entries left behind by earlier updates (#63717 problem 6) — parked --keep-stash
        # runs and failed restores preserve the stash but nothing ever mentioned it again.
        _m()._warn_orphaned_update_autostashes(git_cmd, _m().PROJECT_ROOT)

        print("→ Fetching updates...")
        fetch_result = _git_run(git_cmd, ["fetch", "origin", branch], network=True)
        if fetch_result.returncode != 0:
            _print_fetch_failure(fetch_result.stderr)
            sys.exit(1)

        current_branch = _current_branch_name(git_cmd, check=True)
        _plan = _prepare_checkout_for_update(
            git_cmd, branch, current_branch, is_fork=is_fork, assume_yes=assume_yes,
            gateway_mode=gateway_mode, gw_input_fn=gw_input_fn, switch_branch=opts.switch_branch,
            _windows_gateway_resume=_windows_gateway_resume)
        commit_count = _plan.commit_count

        if commit_count == 0:
            _finish_already_up_to_date(
                git_cmd, branch, current_branch, _plan, assume_yes=assume_yes,
                gateway_mode=gateway_mode, gw_input_fn=gw_input_fn,
                pre_update_snapshot_id=pre_update_snapshot_id, desktop_dir=desktop_dir,
                had_desktop_app_before_update=had_desktop_app_before_update,
                active_lazy_features=opts.active_lazy_features,
                active_tool_dependencies=opts.active_tool_dependencies,
                _windows_gateway_resume=_windows_gateway_resume)
            return

        if commit_count > 0:
            print(f"→ Found {commit_count} new commit(s)")
        else:
            # Shallow, exact count unrecoverable — but the tips differ, so there IS an update.
            print("→ Updates available (commit count unknown on this shallow checkout)")

        print("→ Pulling updates...")
        pre_pull_sha = _pull_updates(
            git_cmd, branch, _plan.auto_stash_ref, prompt_for_restore=_plan.prompt_for_restore,
            gw_input_fn=gw_input_fn, discard_local_changes=opts.discard_local_changes,
            keep_stash=opts.keep_stash)
        _apply_pulled_update(
            git_cmd, branch, pre_pull_sha, _plan, opts, gateway_mode=gateway_mode,
            is_fork=is_fork, desktop_dir=desktop_dir,
            had_desktop_app_before_update=had_desktop_app_before_update,
        )

        print()
        print(f"✓ Code updated!{_branch_head_suffix(git_cmd, _m().PROJECT_ROOT)}")

        # ── macOS TCC stale-grant notice (#86385) ──────────────────────
        # Locally-built desktop bundles are re-signed on every update. With the
        # post-#73681 identifier-pinned DR, new grants survive rebuilds — but a
        # grant made to a pre-fix binary stays stale: the System Settings toggle
        # shows ON while macOS re-prompts on every capture, and the modern prompt
        # has no Allow button, so users loop. One line of guidance after update
        # tells affected users how to complete the one-time re-grant.
        if sys.platform == "darwin" and had_desktop_app_before_update:
            print()
            print(
                "  ℹ macOS: if Hermes re-prompts for permissions you already "
                "granted (toggle shows ON), the stored grant is stale — run "
                "`tccutil reset ScreenCapture com.nousresearch.hermes` (repeat "
                "per affected service), toggle it ON in System Settings, then "
                "fully quit & relaunch once."
            )

        # NOTE: the macOS TCC interpreter anchor that used to refresh here
        # (#95131/#95478) is REVERTED: the anchored real-file copy could not
        # load libpython (LC_RPATH resolved into venv/lib/), bricking every
        # hermes command on real Macs (#95425), and re-pointed aliases lost
        # the stdlib (#95541). `hermes doctor` now heals already-anchored
        # venvs back to symlinks. Re-land requires a dylib-complete design
        # verified on macOS hardware first.

        # ── Post-update state.db integrity guard (#68474) ─────────────────
        # Verify that state.db survived the update intact.  If the live file
        # is now corrupted (zeroed, missing header, integrity failure),
        # automatically restore from the pre-update snapshot rather than
        # letting the user discover silently that their sessions are gone.
        try:
            from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity

            _state_path = get_hermes_home() / "state.db"
            if _state_path.exists():
                _state_ok = verify_sqlite_integrity(
                    _state_path,
                    check_header=True,
                    run_pragma=True,
                )
                if _state_ok.get("valid"):
                    logger.debug(
                        "Post-update state.db integrity check: %s",
                        _state_ok.get("message"),
                    )
                else:
                    print()
                    print(
                        "⚠ state.db is corrupted after update: "
                        + _state_ok.get("message", "unknown error")
                    )
                    _pre_snap_id = pre_update_snapshot_id
                    if _pre_snap_id:
                        _snap_state = (
                            _quick_snapshot_root(get_hermes_home())
                            / _pre_snap_id
                            / "state.db"
                        )
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(
                                _snap_state, check_header=True, run_pragma=True
                            )
                            if _snap_ok.get("valid"):
                                try:
                                    if _restore_state_db_from_snapshot(
                                        _state_path, _snap_state
                                    ):
                                        print(
                                            "  ✓ Auto-restored from pre-update "
                                            f"snapshot ({_pre_snap_id})"
                                        )
                                    else:
                                        print(
                                            "  ✗ Auto-restore FAILED — restored "
                                            "copy also failed integrity"
                                        )
                                except OSError as _exc:
                                    print(
                                        f"  ✗ Auto-restore file copy failed: {_exc}"
                                    )
                            else:
                                print(
                                    "  ✗ Pre-update snapshot also failed integrity"
                                )
                        else:
                            print(
                                "  ⚠ Pre-update snapshot does not contain state.db"
                            )
                    else:
                        print("  ⚠ No pre-update snapshot was taken")
                    print()
        except Exception as exc:
            logger.debug("Post-update state.db integrity check failed: %s", exc)

        # Seed the model-catalog disk cache from the freshly-pulled checkout.
        # The repo ships the canonical catalog at
        # website/static/api/model-catalog.json, and `git pull` just made it
        # current — so copy it straight over ~/.hermes/cache/model_catalog.json
        # instead of waiting on a network fetch (which can be bot-gated or hit a
        # Portal hiccup). Keeps the model picker's curated/free lists in sync
        # with the version the user just installed. Non-fatal on failure: the
        # normal network refresh still applies on the next picker open.
        try:
            from hermes_cli.model_catalog import seed_cache_from_checkout

            if seed_cache_from_checkout(_m().PROJECT_ROOT):
                print("  ✓ Model catalog cache refreshed from checkout")
        except Exception as e:
            logger.debug("Model catalog seed during update failed: %s", e)

        # Sync bundled skills (copies new, updates changed, respects user deletions)
        try:
            from tools.skills_sync import sync_skills

            print()
            print("→ Syncing bundled skills...")
            result = sync_skills(quiet=True)
            if result["copied"]:
                print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
            if result.get("updated"):
                print(
                    f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}"
                )
            if result.get("user_modified"):
                print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
                print(
                    "    → see them: hermes skills list-modified  "
                    "(diff/reset to resume updates)"
                )
            if result.get("cleaned"):
                print(f"  − {len(result['cleaned'])} removed from manifest")
            if result.get("relocated"):
                print(
                    f"  → {len(result['relocated'])} moved to new upstream paths: "
                    f"{', '.join(result['relocated'])}"
                )
            if not result["copied"] and not result.get("updated"):
                print("  ✓ Skills are up to date")
        except Exception as e:
            logger.debug("Skills sync during update failed: %s", e)

        # Sync bundled skills to all profiles (including the active one).
        # seed_profile_skills() uses subprocess with an explicit HERMES_HOME so
        # it is not affected by sync_skills()'s module-level HERMES_HOME cache,
        # which means the active profile is reliably synced regardless of whether
        # the caller's HERMES_HOME env var points at the default or a named profile.
        try:
            from hermes_cli.profiles import (
                list_profiles,
                seed_profile_skills,
            )

            all_profiles = list_profiles()
            if all_profiles:
                print()
                print("→ Syncing bundled skills to all profiles...")
                for p in all_profiles:
                    try:
                        r = seed_profile_skills(p.path, quiet=True)
                        if r and r.get("skipped_opt_out"):
                            status = "opted out (--no-skills)"
                        elif r:
                            copied = len(r.get("copied", []))
                            updated = len(r.get("updated", []))
                            modified = len(r.get("user_modified", []))
                            parts = []
                            if copied:
                                parts.append(f"+{copied} new")
                            if updated:
                                parts.append(f"↑{updated} updated")
                            if modified:
                                parts.append(f"~{modified} user-modified")
                            status = ", ".join(parts) if parts else "up to date"
                        else:
                            status = "sync failed"
                        print(f"  {p.name}: {status}")
                    except Exception as pe:
                        print(f"  {p.name}: error ({pe})")
        except Exception:
            pass  # profiles module not available or no profiles

        # Backfill per-profile .env files for profiles created before the
        # .env-seeding fix (#44792). Copies the default install's .env so
        # those profiles keep the credentials they were effectively using.
        try:
            from hermes_cli.profiles import backfill_profile_envs

            backfilled = backfill_profile_envs(quiet=True)
            if backfilled:
                print()
                print(
                    f"→ Seeded .env for {len(backfilled)} profile(s) "
                    f"(copied from default): {', '.join(backfilled)}"
                )
        except Exception:
            pass  # profiles module not available or no profiles

        # Sync Honcho host blocks to all profiles
        try:
            from plugins.memory.honcho.cli import sync_honcho_profiles_quiet

            synced = sync_honcho_profiles_quiet()
            if synced:
                print(f"\n-> Honcho: synced {synced} profile(s)")
        except Exception:
            pass  # honcho plugin not installed or not configured

        # Check for config migrations (#91360).
        _check_and_apply_config_migration(
            assume_yes=assume_yes,
            gateway_mode=gateway_mode,
            pre_update_snapshot_id=pre_update_snapshot_id,
        )

        _print_update_summary(
            node_failures=node_failures,
            desktop_build_ok=desktop_build_ok,
            pre_update_version=pre_update_version,
        )

        # Search-index optimization notice (v23). Existing installs keep their
        # working search index untouched on update; the compact v23 layout —
        # which reclaims a large fraction of state.db on heavy users — is
        # opt-in. Surface it here (the moment the user is already thinking
        # about their install) with the exact command and the concrete size
        # win. Show-once-ish: only when a legacy index is actually present.
        try:
            _print_fts_optimize_available_notice()
        except Exception as e:
            logger.debug("FTS optimize notice failed: %s", e)

        # Curator first-run heads-up. Only prints when curator is enabled AND
        # has never run — i.e. the window where the ticker would otherwise
        # have fired against a fresh skill library. Kept silent on steady
        # state so we don't nag.
        try:
            _print_curator_first_run_notice()
        except Exception as e:
            logger.debug("Curator first-run notice failed: %s", e)

        # Most-recent curator run notice — show-once per run. Surfaces the
        # rename map (`old-name → umbrella`) on the high-attention update
        # surface so users learn about consolidations without having to
        # check `hermes curator status`. Self-stamps after printing so it
        # never repeats for the same run.
        try:
            _print_curator_recent_run_notice()
        except Exception as e:
            logger.debug("Curator recent-run notice failed: %s", e)

        # Repair RHEL-family root installs where /usr/local/bin isn't on PATH
        # for non-login interactive shells.  No-op on every other platform.
        try:
            _ensure_fhs_path_guard()
        except Exception as e:
            logger.debug("FHS PATH guard check failed: %s", e)

        # Self-heal the hermes-acp launcher for installs that predate it, so
        # ACP hosts (Zed, JetBrains, Buzz) can resolve Hermes on PATH without
        # a reinstall.  No-op on Windows (the launcher migration below owns
        # that) and when already present.
        try:
            _ensure_acp_launcher()
        except Exception as e:
            logger.debug("hermes-acp launcher self-heal failed: %s", e)

        # Migrate the Windows hermes launchers to the managed binary dir
        # (the default Hermes root's bin, next to the managed uv) and repair
        # them if they are missing. Earlier layouts put them inside the git
        # checkout (hermes-agent\bin) or put venv\Scripts itself on PATH; the
        # in-checkout copies were swept by this command's own pre-update
        # autostash (git stash push --include-untracked) and, with
        # --keep-stash (the desktop updater), never restored — `hermes`
        # stopped resolving in every new terminal. Updates never run
        # install.ps1, so this tail call is how existing installs reach the
        # new layout. No-op on POSIX and on source checkouts (root is not
        # the managed clone under the default Hermes root).
        try:
            from hermes_cli._install_repair import migrate_windows_bin_path

            migrate_windows_bin_path(_m().PROJECT_ROOT)
        except Exception as e:
            logger.debug("Windows bin launcher migration failed: %s", e)

        # Refresh the cua-driver binary used by the Computer Use toolset.
        # The upstream installer is gated on supported platforms and on the
        # binary already being on PATH, so this is a no-op for users who
        # don't have it. Tying the refresh to ``hermes update`` gives users a
        # predictable cadence (matches when they pull new agent code) without
        # adding startup latency or a per-launch GitHub API call.
        try:
            refresh_cua_driver = True
            try:
                from hermes_cli.config import load_config

                _update_cfg = (load_config() or {}).get("updates", {})
                if isinstance(_update_cfg, dict):
                    refresh_cua_driver = bool(
                        _update_cfg.get("refresh_cua_driver", True)
                    )
            except Exception as cfg_exc:
                logger.debug("Could not read updates.refresh_cua_driver: %s", cfg_exc)

            if (
                refresh_cua_driver
                and sys.platform in ("darwin", "win32", "linux")
                and shutil.which("cua-driver")
            ):
                from hermes_cli.tools_config import install_cua_driver

                print()
                print("→ Refreshing cua-driver (Computer Use)...")
                # require_confirmed_update: only run the (multi-minute,
                # silent) upstream installer when the driver's native
                # check-update verb positively reports a newer release.
                # An indeterminate check (offline, rate-limited, old
                # driver) keeps the installed version — `hermes update`
                # must stay fast; `hermes computer-use install --upgrade`
                # remains the force path. Windows also defers confirmed
                # updates and contract repairs to that explicit command
                # because the upstream installer may prompt for console/UAC
                # consent that this hidden updater cannot provide.
                install_cua_driver(
                    upgrade=True,
                    require_confirmed_update=True,
                    show_installer_progress=False,
                )
        except Exception as e:
            logger.debug("cua-driver refresh failed: %s", e)

        # Write exit code *before* the gateway restart attempt.
        # When running as ``hermes update --gateway`` (spawned by the gateway's
        # /update command), this process lives inside the gateway's systemd
        # cgroup.  A graceful SIGUSR1 restart keeps the drain loop alive long
        # enough for the exit-code marker to be written below, but the
        # fallback ``systemctl restart`` path (see below) kills everything in
        # the cgroup (KillMode=mixed → SIGKILL to remaining processes),
        # including us and the wrapping bash shell.  The shell never reaches
        # its ``printf $status > .update_exit_code`` epilogue, so the
        # exit-code marker file would never be created.  The new gateway's
        # update watcher would then poll for 30 minutes and send a spurious
        # timeout message.
        #
        # Writing the marker here — after git pull + pip install succeed but
        # before we attempt the restart — ensures the new gateway sees it
        # regardless of how we die. Gated on desktop_build_ok (#88251): a
        # Desktop rebuild failure must not be reported as "0" — the gateway's
        # /update watcher (gateway/run.py) polls this file.
        if gateway_mode:
            _write_gateway_update_exit_code(desktop_build_ok)

        _run_post_update_restart_in_fresh_process(
            gateway_mode=gateway_mode,
            node_failures=node_failures,
            _pre_update_plan=_pre_update_plan,
            _windows_gateway_resume=_windows_gateway_resume,
        )

    except _shim_quarantine_error_type() as e:
        # Strict quarantine refused BEFORE any installer ran — defer via marker, exit 2, no ZIP.
        # See #87331.
        _refuse_update_for_contended_shims(e)
    except subprocess.CalledProcessError as e:
        _handle_update_called_process_error(e, args, gateway_mode, had_desktop_app_before_update)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Optional  # noqa: F401,E402
from datetime import datetime  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import json  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
