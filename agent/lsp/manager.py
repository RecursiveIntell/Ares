"""Service-level orchestration for LSP clients.

The :class:`LSPService` is the bridge between the synchronous
file_operations layer and the async :class:`agent.lsp.client.LSPClient`.

Design choices:

- A **single asyncio event loop** runs in a background thread.  All
  client work happens on that loop.  Synchronous callers from
  ``tools/file_operations.py`` use :meth:`get_diagnostics_sync` to
  open + wait + drain in one blocking call.

- One client per ``(server_id, workspace_root)`` key.  Lazy spawn:
  the first request for a key spawns the client; subsequent requests
  re-use it.

- A **broken-set** records failed ``(server_id, workspace_root)`` pairs.
  The same executable/root/config identity is not retried; a changed
  identity invalidates the failure so a repaired server can recover.

- A **delta baseline** map keeps "diagnostics-as-of-the-last-snapshot"
  per file.  ``snapshot_baseline()`` is called BEFORE a write; the
  next ``get_diagnostics_sync()`` returns only diagnostics that
  weren't in the baseline.  This is the lift from Claude Code's
  ``beforeFileEdited`` / ``getNewDiagnostics`` pattern, except wired
  to the local LSP layer instead of MCP IDE RPC.

The service is **enabled by default** — call :meth:`is_active` to check
whether it's actually doing anything.  When LSP is disabled in
config, when no git workspace can be detected, when all configured
servers are missing binaries and auto-install is off, ``is_active``
returns False and the file_operations layer falls through to the
in-process syntax check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.lsp import eventlog
from agent.lsp.client import (
    DIAGNOSTICS_DOCUMENT_WAIT,
    LSPClient,
)
from agent.lsp.servers import (
    ServerContext,
    find_server_for_file,
    language_id_for,
)
from agent.lsp.workspace import (
    clear_cache,
    resolve_workspace_for_file,
)

logger = logging.getLogger("agent.lsp.manager")

DEFAULT_IDLE_TIMEOUT = 600  # seconds; servers idle for >10min get reaped
MIN_IDLE_TIMEOUT = 30  # floor for config values; must exceed any per-op wait budget
ENRICHMENT_INIT_BUDGET = 3.0
ENRICHMENT_MAX_WAIT = 10.0
ENRICHMENT_JOIN_MARGIN = 0.5


@dataclass
class EnrichmentResult:
    """Optional diagnostic projection, never a syntax/validation verdict.

    ``complete`` means fresh server data, including a genuinely empty
    result. ``degraded`` means no fresh verdict from an acquired server;
    ``unavailable`` means no server could be acquired. Consumers must
    preserve the quality even when diagnostics is empty.
    """

    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    quality: str = "unavailable"
    reason: str = "not_enabled"
    phases: Dict[str, float] = field(default_factory=dict)


class _BackgroundLoop:
    """A daemon thread that owns one asyncio event loop.

    Provides :meth:`run` for synchronous callers — submits a coroutine
    to the loop and blocks until it finishes (or a timeout fires).
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_forever,
            name="hermes-lsp-loop",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_forever(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self, coro, *, timeout: Optional[float] = None) -> Any:
        """Submit a coroutine to the loop and block until done.

        Returns the coroutine's result, or raises its exception.
        """
        from agent.async_utils import safe_schedule_threadsafe
        if self._loop is None:
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("background loop not started")
        fut = safe_schedule_threadsafe(coro, self._loop)
        if fut is None:
            raise RuntimeError("background loop not running")
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            raise

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._loop = None
        self._thread = None


class LSPService:
    """The process-wide LSP service.

    Created once via :meth:`create_from_config`; the
    :func:`agent.lsp.get_service` accessor manages the singleton.
    Most callers should use that accessor rather than constructing
    :class:`LSPService` directly.
    """

    # ------------------------------------------------------------------
    # construction + factory
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        enabled: bool,
        wait_mode: str,
        wait_timeout: float,
        install_strategy: str,
        binary_overrides: Optional[Dict[str, List[str]]] = None,
        env_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        init_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        disabled_servers: Optional[List[str]] = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        profile_boundary=None,
    ) -> None:
        from agent.secret_scope import build_profile_env_boundary
        from hermes_constants import get_hermes_home, get_process_hermes_home

        boundary = profile_boundary or build_profile_env_boundary(
            get_process_hermes_home(), get_hermes_home(),
        )
        self._profile_home = boundary.target_home
        self._source_home = boundary.source_home
        self._profile_generation = boundary.target_generation
        self._enabled = enabled
        self._wait_mode = wait_mode if wait_mode in {"document", "full"} else "document"
        try:
            wait_timeout = float(wait_timeout)
        except (ValueError, TypeError):
            wait_timeout = DIAGNOSTICS_DOCUMENT_WAIT
        if not math.isfinite(wait_timeout) or wait_timeout <= 0:
            wait_timeout = DIAGNOSTICS_DOCUMENT_WAIT
        self._wait_timeout = min(wait_timeout, ENRICHMENT_MAX_WAIT)
        self._install_strategy = install_strategy
        self._binary_overrides = binary_overrides or {}
        self._env_overrides = env_overrides or {}
        self._init_overrides = init_overrides or {}
        self._disabled_servers = set(disabled_servers or [])
        self._idle_timeout = idle_timeout

        self._loop = _BackgroundLoop()
        if self._enabled:
            self._loop.start()

        # Per-(server_id, workspace_root) state
        self._clients: Dict[Tuple[str, str], LSPClient] = {}
        self._broken: set = set()
        self._spawning: Dict[Tuple[str, str], asyncio.Future] = {}
        self._last_used: Dict[Tuple[str, str], float] = {}
        self._identities: Dict[Tuple[str, str], tuple] = {}
        self._commands: Dict[Tuple[str, str], List[str]] = {}
        self._invalidated: set = set()
        self._state_lock = threading.Lock()
        self._idle_reaper_task: Optional[asyncio.Task] = None

        # Delta baseline: file path → snapshot of diagnostics taken
        # immediately before a write.  ``get_diagnostics_sync`` filters
        # out anything in the baseline so the agent only sees errors
        # introduced by the current edit.
        self._delta_baseline: Dict[str, List[Dict[str, Any]]] = {}
        self._baseline_owner: Dict[str, tuple] = {}

        if self._enabled and self._idle_timeout > 0:
            self._loop.run(self._start_idle_reaper(), timeout=2.0)

    @classmethod
    def create_from_config(cls, *, config=None, profile_boundary=None) -> Optional["LSPService"]:
        """Build a service from ``hermes_cli.config`` settings.

        Returns ``None`` if the config can't be loaded.  The service
        itself returns ``is_active()`` False when LSP is disabled.
        """
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() if config is None else config
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP config load failed: %s", e)
            return None

        lsp_cfg = (cfg.get("lsp") or {}) if isinstance(cfg, dict) else {}
        if not isinstance(lsp_cfg, dict):
            lsp_cfg = {}

        enabled = bool(lsp_cfg.get("enabled", True))
        wait_mode = lsp_cfg.get("wait_mode", "document")
        wait_timeout = lsp_cfg.get("wait_timeout", DIAGNOSTICS_DOCUMENT_WAIT)
        install_strategy = lsp_cfg.get("install_strategy", "auto")
        try:
            idle_timeout = float(lsp_cfg.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))
        except (TypeError, ValueError):
            idle_timeout = DEFAULT_IDLE_TIMEOUT
        if 0 < idle_timeout < MIN_IDLE_TIMEOUT:
            # A timeout below the per-operation wait budget could reap a
            # client mid-flight; the resulting outer timeout would then
            # mark the (server, workspace) pair broken for the process
            # lifetime.  Clamp to a safe floor (0 still disables).
            idle_timeout = MIN_IDLE_TIMEOUT
        servers_cfg = lsp_cfg.get("servers") or {}
        disabled = []
        binary_overrides: Dict[str, List[str]] = {}
        env_overrides: Dict[str, Dict[str, str]] = {}
        init_overrides: Dict[str, Dict[str, Any]] = {}
        if isinstance(servers_cfg, dict):
            for name, sub in servers_cfg.items():
                if not isinstance(sub, dict):
                    continue
                if sub.get("disabled"):
                    disabled.append(name)
                cmd = sub.get("command")
                if isinstance(cmd, list) and cmd:
                    binary_overrides[name] = cmd
                env = sub.get("env")
                if isinstance(env, dict):
                    env_overrides[name] = {k: str(v) for k, v in env.items()}
                init = sub.get("initialization_options")
                if isinstance(init, dict):
                    init_overrides[name] = init

        return cls(
            enabled=enabled,
            wait_mode=wait_mode,
            wait_timeout=wait_timeout,
            install_strategy=install_strategy,
            binary_overrides=binary_overrides,
            env_overrides=env_overrides,
            init_overrides=init_overrides,
            disabled_servers=disabled,
            idle_timeout=idle_timeout,
            profile_boundary=profile_boundary,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Return True iff this service should be consulted at all."""
        return self._enabled

    def enabled_for(self, file_path: str) -> bool:
        """Return True iff LSP should run for this specific file.

        Gates on workspace detection (file or cwd inside a git worktree),
        on whether any registered server matches the extension, and
        on whether the (server_id, workspace_root) pair is in the
        broken-set from a previous spawn failure with the same identity.

        Files in already-broken pairs return False so the file_operations
        layer skips the LSP path entirely — no spawn attempts, no
        timeout cost — until the executable, root, or configuration changes
        or the service is restarted (``hermes lsp restart``).
        """
        if not self._enabled:
            return False
        srv = find_server_for_file(file_path)
        if srv is None or srv.server_id in self._disabled_servers:
            return False
        ws_root, gated_in = resolve_workspace_for_file(file_path)
        if not (ws_root and gated_in):
            return False
        # Match the spawn path's exact project root and exclude gate.
        try:
            per_server_root = srv.resolve_root(file_path, ws_root)
        except Exception:  # noqa: BLE001
            return False
        if per_server_root is None:
            return False
        self._refresh_identity(srv, per_server_root, ws_root)
        if (srv.server_id, per_server_root) in self._broken:
            return False
        return True

    def snapshot_baseline(self, file_path: str) -> EnrichmentResult:
        """Snapshot current diagnostics for ``file_path`` as the delta baseline.

        Called BEFORE a write so the next ``get_diagnostics_sync()``
        can filter out pre-existing errors.  Best-effort — failures
        are silently swallowed so a flaky server can't break a write.

        Outer timeouts (e.g. server hangs during initialize) mark the
        (server_id, workspace_root) pair as broken so subsequent edits
        skip it instantly instead of re-paying the timeout cost.
        """
        result = self._collect_sync(file_path, baseline=True)
        path = os.path.abspath(file_path)
        self._delta_baseline[path] = list(result.diagnostics)
        self._baseline_owner[path] = self._owner_for_file(file_path)
        return result

    def get_diagnostics_sync(
        self,
        file_path: str,
        *,
        delta: bool = True,
        timeout: Optional[float] = None,
        line_shift: Optional[Callable[[int], Optional[int]]] = None,
    ) -> List[Dict[str, Any]]:
        """Synchronously open ``file_path`` in the right server, wait for
        diagnostics, return them.

        If ``delta`` is True (default), the result is filtered against
        any baseline previously captured via :meth:`snapshot_baseline`.
        Diagnostics present in the baseline are removed so the caller
        only sees errors introduced by the current edit.

        When ``line_shift`` is provided, baseline diagnostics are
        remapped through it before the set-difference.  This handles
        the case where the edit deleted or inserted lines, causing
        pre-existing diagnostics below the edit point to surface at
        different line numbers in the post-edit snapshot — without
        the shift, they'd all look "introduced by this edit".  Pass
        a callable built by
        :func:`agent.lsp.range_shift.build_line_shift` (pre_text,
        post_text).  Omit when pre/post content isn't available;
        the unshifted comparison still catches diagnostics that
        didn't move.

        Returns an empty list when LSP is disabled, when no workspace
        can be detected, when no server matches, or when the server
        can't be spawned.  Never raises.
        """
        # Compatibility for existing diagnostic-list consumers. New edit
        # enrichment consumers must retain the accompanying quality.
        return self.get_enrichment_sync(
            file_path, delta=delta, timeout=timeout, line_shift=line_shift,
        ).diagnostics

    def get_enrichment_sync(
        self, file_path: str, *, delta: bool = True,
        timeout: Optional[float] = None,
        line_shift: Optional[Callable[[int], Optional[int]]] = None,
    ) -> EnrichmentResult:
        """Return bounded diagnostics and explicit projection quality.

        Declared consumer: file-edit enrichment, where an empty diagnostic
        list must not turn optional server failure into validation success.
        This API cannot waive the caller's mandatory syntax checks.
        """
        result = self._collect_sync(file_path, timeout=timeout)
        path = os.path.abspath(file_path)
        owner = self._owner_for_file(file_path)
        if result.quality != "complete":
            self._delta_baseline.pop(path, None)
            self._baseline_owner.pop(path, None)
            return result
        fresh = list(result.diagnostics)
        if delta:
            baseline = []
            if self._baseline_owner.get(path) == owner:
                baseline = self._delta_baseline.get(path) or []
            if baseline:
                if line_shift is not None:
                    from agent.lsp.range_shift import shift_baseline
                    baseline = shift_baseline(baseline, line_shift)
                seen = {_diag_key(d) for d in baseline}
                result.diagnostics = [d for d in fresh if _diag_key(d) not in seen]
            # Use this exact fresh snapshot, even when clean. No second
            # wait or lookup by a different (git rather than project) root.
            self._delta_baseline[path] = fresh
            self._baseline_owner[path] = owner
        return result

    def _collect_sync(self, file_path: str, *, baseline=False, timeout=None) -> EnrichmentResult:
        phase = "baseline" if baseline else "post_edit"
        spans = {"initialize": 0.0, phase: 0.0}
        try:
            if not self.enabled_for(file_path):
                return EnrichmentResult(reason="not_enabled_or_cached_failure", phases=spans)
            budget = ENRICHMENT_INIT_BUDGET + self._wait_timeout
            if timeout is not None:
                limit = float(timeout)
                if math.isfinite(limit) and limit > 0:
                    budget = min(budget, limit)
            result = self._loop.run(
                self._enrich_async(file_path, baseline=baseline, budget=budget, spans=spans),
                timeout=budget + ENRICHMENT_JOIN_MARGIN,
            )
            return result
        except Exception as exc:  # optional enrichment never breaks writes
            logger.debug("LSP enrichment failed for %s: %s", file_path, exc)
            self._mark_broken_for_file(file_path, exc)
            return EnrichmentResult(reason="submission_failed", phases=dict(spans))

    async def _enrich_async(self, file_path: str, *, baseline, budget, spans) -> EnrichmentResult:
        deadline = time.monotonic() + budget
        phase = "baseline" if baseline else "post_edit"
        started = time.monotonic()
        try:
            client = await asyncio.wait_for(
                self._get_or_spawn(file_path), timeout=min(ENRICHMENT_INIT_BUDGET, budget),
            )
        except Exception as exc:
            logger.debug("LSP initialize enrichment failed: %s", exc)
            return EnrichmentResult(reason="initialize_failed", phases=spans)
        finally:
            spans["initialize"] = time.monotonic() - started
        if client is None:
            return EnrichmentResult(reason="server_unavailable", phases=spans)
        started = time.monotonic()
        try:
            remaining = max(0.0, min(self._wait_timeout, deadline - started))
            work = self._snapshot_async(file_path) if baseline else self._open_and_wait_async(file_path)
            diags = await asyncio.wait_for(work, timeout=remaining)
            if diags is None:
                return EnrichmentResult(quality="degraded", reason="fresh_diagnostics_timeout", phases=spans)
            srv = find_server_for_file(file_path)
            if diags:
                eventlog.log_diagnostics(srv.server_id, file_path, len(diags))
            else:
                eventlog.log_clean(srv.server_id, file_path)
            return EnrichmentResult(diagnostics=diags, quality="complete", reason="fresh", phases=spans)
        except asyncio.TimeoutError:
            return EnrichmentResult(quality="degraded", reason="fresh_diagnostics_timeout", phases=spans)
        except Exception as exc:
            logger.debug("LSP document enrichment failed: %s", exc)
            # A raised server/transport failure is different from a slow
            # diagnostics publication: suppress repeat work for this identity.
            self._broken.add((client.server_id, client.workspace_root))
            return EnrichmentResult(quality="degraded", reason="server_error", phases=spans)
        finally:
            spans[phase] = time.monotonic() - started

    def _owner_for_file(self, file_path: str):
        srv = find_server_for_file(file_path)
        ws, gated = resolve_workspace_for_file(file_path)
        if not (srv and ws and gated):
            return None
        try:
            root = srv.resolve_root(file_path, ws)
        except Exception:
            return None
        key = (srv.server_id, root)
        with self._state_lock:
            client = self._clients.get(key)
            return key, self._identities.get(key), client

    @staticmethod
    def _path_identity(path, *, directory=False):
        try:
            stat = os.stat(path)
            return (stat.st_dev, stat.st_ino) if directory else (
                stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_mode,
            )
        except OSError:
            return None

    def _refresh_identity(self, srv, root, workspace):
        key = (srv.server_id, root)
        command = self._commands.get(key) or self._binary_overrides.get(srv.server_id) or []
        executable = (shutil.which(command[0]) or command[0]) if command else ""
        identity = (
            self._path_identity(root, directory=True),
            self._path_identity(os.path.join(workspace, ".git"), directory=True),
            executable, self._path_identity(executable),
            tuple((arg, self._path_identity(arg)) for arg in command[1:] if os.path.isabs(arg)),
            tuple((path, self._path_identity(path)) for path in [
                *os.get_exec_path(), str(self._profile_home / "lsp" / "bin"),
            ]),
            json.dumps([self._binary_overrides, self._env_overrides, self._init_overrides,
                        self._install_strategy], sort_keys=True),
            tuple((arg, self._path_identity(shutil.which(arg) or arg))
                  for arg in self._binary_overrides.get(srv.server_id, []) if arg),
        )
        with self._state_lock:
            previous = self._identities.get(key)
            if previous is not None and previous != identity:
                self._broken.discard(key)
                self._invalidated.add(key)
            self._identities[key] = identity
        return identity

    def _mark_broken_for_file(self, file_path: str, exc: BaseException) -> None:
        """Mark the (server_id, workspace_root) pair as broken so subsequent
        edits skip it instantly instead of re-paying timeout cost.

        Called when the outer ``_loop.run`` timeout cancels an in-flight
        spawn/initialize that the inner ``_get_or_spawn`` task was still
        holding open.  Without this, every subsequent write would re-enter
        the spawn path and re-pay the full ``snapshot_baseline``
        timeout (8s) until the binary is fixed.

        Also kills any orphan client process that survived the cancelled
        future, and emits a single eventlog WARNING so the user knows
        which server gave up.

        ``exc`` is whatever exception the outer wrapper caught — used
        only for logging, never re-raised.
        """
        srv = find_server_for_file(file_path)
        if srv is None:
            return
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            return
        try:
            per_server_root = srv.resolve_root(file_path, ws_root) or ws_root
        except Exception:  # noqa: BLE001
            per_server_root = ws_root
        key = (srv.server_id, per_server_root)
        already_broken = key in self._broken
        self._broken.add(key)

        # Kill any client we managed to spawn before the timeout.  The
        # cancelled future never reached the broken-set add inside
        # ``_get_or_spawn`` so the client may still be hanging in
        # ``_clients`` with a half-initialized state.
        with self._state_lock:
            client = self._clients.pop(key, None)
            self._last_used.pop(key, None)
        if client is not None:
            try:
                # Fire-and-forget shutdown — give it a second to cleanup,
                # but don't block.  We're already on a slow path.
                self._loop.run(client.shutdown(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass

        if not already_broken:
            eventlog.log_spawn_failed(srv.server_id, per_server_root, exc)

    def shutdown(self) -> None:
        """Tear down all clients and stop the background loop."""
        if not self._enabled:
            return
        try:
            self._loop.run(self._shutdown_async(), timeout=10.0)
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)
        self._loop.stop()
        self._enabled = False
        clear_cache()

    # ------------------------------------------------------------------
    # async internals
    # ------------------------------------------------------------------

    async def _snapshot_async(self, file_path: str) -> Optional[List[Dict[str, Any]]]:
        client = await self._get_or_spawn(file_path)
        if client is None:
            return None
        version = await client.open_file(file_path, language_id=language_id_for(file_path))
        fresh = await client.wait_for_diagnostics(
            file_path, version, mode=self._wait_mode, timeout=self._wait_timeout,
        )
        self._touch(client)
        if not fresh:
            # No fresh data for the pre-edit content — an empty baseline
            # is safe: worst case the delta filter removes less, never
            # more.  Never seed the baseline from stale stores.
            return None
        return list(client.diagnostics_for(file_path, fresh_only=True))

    async def _open_and_wait_async(self, file_path: str) -> Optional[List[Dict[str, Any]]]:
        """Open + wait for FRESH diagnostics.

        Returns the fresh diagnostic list, or ``None`` when the server
        never produced post-change data within the wait budget.  The
        distinction matters: ``[]`` means "server checked the new
        content, it's clean", ``None`` means "no verdict" — the caller
        must not substitute stale data for either.
        """
        client = await self._get_or_spawn(file_path)
        if client is None:
            return None
        version = await client.open_file(file_path, language_id=language_id_for(file_path))
        await client.save_file(file_path)
        fresh = await client.wait_for_diagnostics(
            file_path, version, mode=self._wait_mode, timeout=self._wait_timeout
        )
        self._touch(client)
        if not fresh:
            return None
        return list(client.diagnostics_for(file_path, fresh_only=True))

    async def _get_or_spawn(self, file_path: str) -> Optional[LSPClient]:
        if not self._enabled:
            return None
        from agent.secret_scope import build_profile_env_boundary
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        try:
            boundary = build_profile_env_boundary(self._source_home, self._profile_home)
        except Exception:
            return None
        if boundary.target_generation != self._profile_generation:
            return None
        srv = find_server_for_file(file_path)
        if srv is None:
            return None
        if srv.server_id in self._disabled_servers:
            eventlog.log_disabled(srv.server_id, file_path, "disabled in config")
            return None
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            eventlog.log_no_project_root(srv.server_id, file_path)
            return None
        per_server_root = srv.resolve_root(file_path, ws_root)
        if per_server_root is None:
            eventlog.log_disabled(
                srv.server_id, file_path, "exclude marker hit (server gated off)"
            )
            return None  # exclude marker hit, server gated off

        key = (srv.server_id, per_server_root)
        self._refresh_identity(srv, per_server_root, ws_root)
        if key in self._broken:
            return None
        with self._state_lock:
            invalidated = key in self._invalidated
            self._invalidated.discard(key)
            old = self._clients.pop(key, None) if invalidated else None
        if old is not None:
            await old.shutdown()
        with self._state_lock:
            client = self._clients.get(key)
            if client is not None and client.is_running:
                self._last_used[key] = time.time()
                eventlog.log_active(srv.server_id, per_server_root)
                return client
            spawning = self._spawning.get(key)
        if spawning is not None:
            try:
                return await asyncio.shield(spawning)
            except Exception:  # noqa: BLE001
                return None

        # Begin spawn
        loop = asyncio.get_running_loop()
        spawn_future: asyncio.Future = loop.create_future()
        with self._state_lock:
            self._spawning[key] = spawn_future
        try:
            ctx = ServerContext(
                workspace_root=per_server_root,
                install_strategy=self._install_strategy,
                binary_overrides=self._binary_overrides,
                env_overrides=self._env_overrides,
                init_overrides=self._init_overrides,
            )
            def build_spawn():
                home_token = set_hermes_home_override(self._profile_home)
                try:
                    return srv.build_spawn(per_server_root, ctx)
                finally:
                    reset_hermes_home_override(home_token)

            # Install/resolution is synchronous; it must not block the
            # background loop and defeat the caller's finite join budget.
            spec = await asyncio.to_thread(build_spawn)
            if spec is None:
                # ``build_spawn`` returns None when the binary can't be
                # located (auto-install disabled, manual-only server,
                # or install attempt failed).  Surface this once via
                # the structured logger so the user can act on it.
                eventlog.log_server_unavailable(srv.server_id, srv.server_id)
                # Resolution may create the profile-owned staging directory
                # even when no executable is available. Cache this failed
                # observation, not its pre-resolution filesystem identity.
                self._refresh_identity(srv, per_server_root, ws_root)
                self._invalidated.discard(key)
                self._broken.add(key)
                spawn_future.set_result(None)
                return None
            self._commands[key] = list(spec.command)
            # Record the resolved executable, including installer recovery.
            self._refresh_identity(srv, per_server_root, ws_root)
            self._invalidated.discard(key)
            client = LSPClient(
                server_id=srv.server_id,
                workspace_root=spec.workspace_root,
                command=spec.command,
                env=spec.env,
                cwd=spec.cwd,
                initialization_options=spec.initialization_options,
                seed_diagnostics_on_first_push=spec.seed_diagnostics_on_first_push or srv.seed_first_push,
                profile_boundary=boundary,
            )
            try:
                await client.start()
            except (Exception, asyncio.CancelledError) as e:
                eventlog.log_spawn_failed(srv.server_id, per_server_root, e)
                self._broken.add(key)
                spawn_future.set_result(None)
                if isinstance(e, asyncio.CancelledError):
                    raise
                return None
            with self._state_lock:
                self._clients[key] = client
                self._last_used[key] = time.time()
            eventlog.log_active(srv.server_id, per_server_root)
            spawn_future.set_result(client)
            return client
        except (Exception, asyncio.CancelledError):
            self._broken.add(key)
            raise
        finally:
            if not spawn_future.done():
                spawn_future.set_result(None)
            with self._state_lock:
                self._spawning.pop(key, None)

    async def _start_idle_reaper(self) -> None:
        self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    def _touch(self, client: LSPClient) -> None:
        """Refresh the last-used timestamp for a client we just used.

        Guarded on membership so a reaped-mid-operation client can't
        resurrect an orphan ``_last_used`` entry after the reaper popped
        the key.  All writers and the reaper run on the background loop
        thread; the lock keeps this consistent with the reader anyway.
        """
        key = (client.server_id, client.workspace_root)
        with self._state_lock:
            if key in self._clients:
                self._last_used[key] = time.time()

    async def _idle_reaper_loop(self) -> None:
        interval = min(60.0, self._idle_timeout)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reap_idle_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # A transient sweep error must not kill the reaper —
                # otherwise one bad shutdown permanently re-opens the
                # unbounded-accumulation leak this loop exists to fix.
                logger.debug("LSP idle reaper sweep error: %s", e)

    async def _reap_idle_once(self) -> None:
        cutoff = time.time() - self._idle_timeout
        with self._state_lock:
            idle_keys = [
                key
                for key in self._clients
                if self._last_used.get(key, 0) < cutoff
            ]
            clients = [self._clients.pop(key) for key in idle_keys]
            for key in idle_keys:
                self._last_used.pop(key, None)
        if clients:
            eventlog.log_reaped(
                [(c.server_id, c.workspace_root) for c in clients],
                self._idle_timeout,
            )
            await asyncio.gather(
                *(client.shutdown() for client in clients),
                return_exceptions=True,
            )

    async def _shutdown_async(self) -> None:
        reaper = self._idle_reaper_task
        self._idle_reaper_task = None
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        with self._state_lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._broken.clear()
            self._identities.clear()
            self._commands.clear()
            self._invalidated.clear()
            self._last_used.clear()
        await asyncio.gather(
            *(c.shutdown() for c in clients),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # status / introspection (used by ``hermes lsp status``)
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of the service for the CLI status command."""
        with self._state_lock:
            clients = [
                {
                    "server_id": k[0],
                    "workspace_root": k[1],
                    "state": c.state,
                    "running": c.is_running,
                }
                for k, c in self._clients.items()
            ]
            broken = list(self._broken)
        return {
            "enabled": self._enabled,
            "wait_mode": self._wait_mode,
            "wait_timeout": self._wait_timeout,
            "install_strategy": self._install_strategy,
            "clients": clients,
            "broken": broken,
            "disabled_servers": sorted(self._disabled_servers),
        }


def _diag_key(d: Dict[str, Any]) -> str:
    """Content equality key used for cross-edit delta filtering.

    Includes the diagnostic's position range — when used together
    with :func:`agent.lsp.range_shift.shift_baseline`, the baseline
    is line-shifted into post-edit coordinates BEFORE this key is
    computed, so identical-but-shifted diagnostics hash equal.  Two
    genuinely distinct diagnostics at different lines (e.g. the same
    error class introduced at a second site) hash differently and
    are surfaced as new.

    Mirrors :func:`agent.lsp.client._diagnostic_key`; intentionally
    identical so the two layers agree on diagnostic identity.
    """
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    code = d.get("code")
    if code is not None and not isinstance(code, str):
        code = str(code)
    return "\x00".join(
        [
            str(d.get("severity") or 1),
            str(code or ""),
            str(d.get("source") or ""),
            str(d.get("message") or "").strip(),
            f"{start.get('line', 0)}:{start.get('character', 0)}-{end.get('line', 0)}:{end.get('character', 0)}",
        ]
    )


__all__ = ["LSPService", "EnrichmentResult"]
