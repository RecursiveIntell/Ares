"""Supervisor for the dashboard compute-host child process.

The dashboard process owns sockets and JSON-RPC dispatch.  When
``dashboard.turn_isolation`` is enabled, agent turns move behind one persistent
``python -m tui_gateway.compute_host`` child so compute-heavy agent threads do
not contend with the serving process' event loop for the same GIL.
"""

from __future__ import annotations

import json

from tui_gateway.checkpoint_json import load_checkpoint_frame
import logging
import os
import queue
import select
import stat
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.environments.local import build_subprocess_env, hermes_subprocess_env

logger = logging.getLogger(__name__)
_Thread = threading.Thread

MUTATOR_ROUTE_TABLE: dict[str, str] = {
    "config.set.model": "run-concurrent",
    "config.set.fast": "idle-gated",
    "config.set.reasoning": "idle-gated",
    "prompt.submit": "turn-path",
    "session.interrupt": "turn-path",
    "session.steer": "run-concurrent",
    "session.redirect": "run-concurrent",
    "clarify.snapshot": "run-concurrent",
    "clarify.respond": "run-concurrent",
    "reload.mcp": "run-concurrent",
    "session.save": "run-concurrent",
    "session.run_checkpoint.claim": "run-concurrent",
    "session.run_checkpoint.refresh": "run-concurrent",
    "session.run_checkpoint.release": "run-concurrent",
    "session.run_checkpoint.basis": "run-concurrent",
    "session.compress": "idle-gated",
    "prompt.submit.truncate": "idle-gated",
    "slash.model": "idle-gated",
    "slash.personality": "idle-gated",
    "slash.prompt": "idle-gated",
    "slash.compress": "idle-gated",
    "session.reset": "idle-gated",
    "session.history.reload": "idle-gated",
    "slash.retry": "idle-gated",
}

_REGISTRY_NAME = "dashboard-compute-host.json"
_RESPAWN_WINDOW_SECS = 300.0
_SHUTDOWN_TIMEOUT_SECS = 10.0
_STARTUP_TIMEOUT_SECS = 10.0


def append_log_record(path: str | Path, record: str) -> None:
    """Append one log record using O_APPEND and exactly one os.write call."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = record if record.endswith("\n") else f"{record}\n"
    data = text.encode("utf-8", errors="replace")
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _build_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_repo_root()),
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return "unknown"


def _default_registry_path() -> Path:
    return get_hermes_home() / "state" / _REGISTRY_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        return False


def _pid_command(pid: int) -> str:
    if pid <= 0:
        return ""
    # Linux fast path.
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        data = proc_cmdline.read_bytes()
        if data:
            return data.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def is_compute_host_identity(pid: int) -> bool:
    cmd = _pid_command(pid)
    return "tui_gateway.compute_host" in cmd


class HostSendNotSent(TimeoutError):
    """Transport refused this frame with proven zero bytes offered."""


class HostSendUncertain(TimeoutError):
    """Some bytes were offered; never replay this frame automatically."""


class HostBootMismatch(HostSendNotSent, RuntimeError):
    """Turn refused before any frame bytes were offered to a changed host."""


class HostSupervisor:
    """Own one persistent compute-host child and relay its frames."""

    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        argv: list[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        rpc_sink: Callable[[dict], None] | None = None,
        on_crash: Callable[[], None] | None = None,
        respawn_max: int = 3,
        heartbeat_secs: int = 15,
        expected_build_sha: str | None = None,
        expected_hermes_home: str | None = None,
        autostart: bool = True,
    ) -> None:
        self.registry_path = Path(registry_path) if registry_path is not None else _default_registry_path()
        self.argv = argv or [sys.executable, "-m", "tui_gateway.compute_host"]
        self.cwd = Path(cwd) if cwd is not None else _repo_root()
        self.env = env
        self.rpc_sink = rpc_sink or (lambda _obj: None)
        self.on_crash = on_crash
        self.respawn_max = max(0, int(respawn_max))
        self.heartbeat_secs = max(1, int(heartbeat_secs))
        self.expected_build_sha = expected_build_sha if expected_build_sha is not None else _build_sha()
        self.expected_hermes_home = expected_hermes_home if expected_hermes_home is not None else str(get_hermes_home())
        from agent.secret_scope import build_profile_env_boundary, is_multiplex_active
        from hermes_constants import get_process_hermes_home

        # Capture owner authority while the constructing profile context is
        # available. Crash recovery runs in a fresh wait thread whose
        # ContextVars are intentionally not trusted for authorization.
        source_home = Path(get_process_hermes_home()).resolve()
        target_home = Path(self.expected_hermes_home).resolve()
        self._profile_env_boundary = None
        if is_multiplex_active() or source_home != target_home:
            self._profile_env_boundary = build_profile_env_boundary(
                source_home=source_home,
                target_home=target_home,
            )
            if self._profile_env_boundary.target_home != target_home:
                raise RuntimeError("compute host owner/profile boundary mismatch")

        self._lock = threading.RLock()
        self._registry_lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._startup_guard = threading.Lock()
        self._startup_result = None
        self._poisoned_proc = None
        self._proc: subprocess.Popen[str] | None = None
        self._ready_proc: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._hello_event = threading.Event()
        self._hello: dict[str, Any] = {}
        self._closing = False
        self._stopped_respawning = False
        self._restart_times: list[float] = []
        self._pending_turns: dict[str, tuple[str, Callable[[dict], None] | None, str]] = {}
        self._pending_controls: dict[str, queue.Queue[dict]] = {}
        self._session_observers: dict[str, tuple[str, Callable[[dict], None], str]] = {}
        self._terminal_queues: dict[str, deque[tuple[dict, Callable[[dict], None]]]] = {}
        self._terminal_workers: set[str] = set()
        self._stderr_tail: list[str] = []
        self._last_progress_counter = 0

        if autostart:
            self.start()

    @property
    def pid(self) -> int:
        proc = self._proc
        return int(proc.pid or 0) if proc is not None else 0

    @property
    def hello(self) -> dict[str, Any]:
        return dict(self._hello)

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and not self._stopped_respawning

    def is_ready(self) -> bool:
        """Liveness alone cannot authorize traffic to an unvalidated child."""
        proc = self._proc
        return not self._closing and proc is not None and self._ready_proc is proc and self.is_running()

    def start(self) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("compute host supervisor is closing")
            if self.is_running():
                return
            self.reconcile_startup_orphan()
            self._spawn_locked(reason="startup")

    def shutdown(self) -> None:
        # Signal waiters before acquiring the lifecycle lock, which startup
        # holds while validating hello. Shutdown must cancel pending admission.
        self._closing = True
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                self._send_frame({"type": "shutdown", "request_id": f"shutdown-{uuid.uuid4().hex}"})
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECS)
        except Exception:
            self._terminate_process(proc)
        finally:
            self._remove_registry()

    def reconcile_startup_orphan(self) -> str:
        """Terminate a stale registered host, guarding against PID reuse."""
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "none"
        except Exception:
            self._remove_registry()
            return "invalid-registry"

        try:
            pid = int(data.get("host_pid") or 0)
        except Exception:
            pid = 0
        if pid <= 0 or not _pid_alive(pid):
            self._remove_registry()
            return "not-running"
        if not self._pid_matches_compute_host(pid):
            # PID was reused by another process. Never signal it.
            self._remove_registry()
            return "pid-reuse-ignored"

        self._terminate_pid(pid, timeout=_SHUTDOWN_TIMEOUT_SECS)
        self._remove_registry()
        return "terminated"

    @property
    def boot_id(self) -> str:
        """Identity of the currently handshaken host, not its session ID."""
        return str(self._hello.get("boot_id") or "")

    def submit_turn(
        self,
        frame: dict[str, Any],
        *,
        on_complete: Callable[[dict], None] | None = None,
        timeout: float = 15.0,
    ) -> str:
        deadline = time.monotonic() + timeout
        self._ensure_started_by(deadline)
        request_id = str(frame.get("request_id") or uuid.uuid4().hex)
        sid = str(frame.get("sid") or "")
        payload = dict(frame)
        payload["type"] = "turn.start"
        payload["request_id"] = request_id
        if not self._registry_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise HostSendNotSent("compute-host admission deadline exceeded; not sent")
        try:
            if request_id in self._pending_turns:
                raise ValueError("duplicate compute-host request ID")
            boot = self.boot_id
            self._pending_turns[request_id] = (sid, on_complete, boot)
            # Private caller-owned admission receipt, assigned by this owner
            # before any byte is offered. It is not supplied by a host payload.
            frame["_admitted_host_boot_id"] = boot
        finally:
            self._registry_lock.release()
        # Only a proven zero-byte refusal retires this registration. Partial
        # or unknown writes retain it for a late terminal or host crash.
        try:
            self._send_frame(payload, deadline=deadline, expected_boot_id=boot)
        except HostSendNotSent:
            with self._registry_lock:
                self._pending_turns.pop(request_id, None)
            raise
        return request_id

    def _ensure_started_by(self, deadline: float) -> None:
        """Share one startup attempt; a timeout never sends a delayed frame."""
        if self._closing:
            raise HostSendNotSent("compute-host startup cancelled by shutdown; not sent")
        if time.monotonic() >= deadline:
            raise HostSendNotSent("compute-host startup deadline exceeded; not sent")
        if self.is_ready():
            return
        if not self._startup_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            raise HostSendNotSent("compute-host startup deadline exceeded; not sent")
        try:
            if self._closing:
                raise HostSendNotSent("compute-host startup cancelled by shutdown; not sent")
            if time.monotonic() >= deadline:
                raise HostSendNotSent("compute-host startup deadline exceeded; not sent")
            if self.is_ready():
                return
            attempt = self._startup_result
            if attempt is None or attempt[0].is_set():
                done = threading.Event()
                errors: list[Exception] = []
                attempt = (done, errors)
                self._startup_result = attempt
                def start_once() -> None:
                    try:
                        self.start()
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        done.set()
                try:
                    _Thread(target=start_once, name="compute-host-startup", daemon=True).start()
                except Exception as exc:
                    errors.append(exc)
                    done.set()
        finally:
            self._startup_guard.release()
        while not attempt[0].is_set():
            if self._closing:
                raise HostSendNotSent("compute-host startup cancelled by shutdown; not sent")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostSendNotSent("compute-host startup deadline exceeded; not sent")
            attempt[0].wait(min(remaining, 0.05))
        if self._closing:
            raise HostSendNotSent("compute-host startup cancelled by shutdown; not sent")
        if attempt[1]:
            raise HostSendNotSent(f"compute-host startup failed; not sent: {attempt[1][0]}") from attempt[1][0]
        if not self.is_ready():
            raise HostSendNotSent("compute-host startup did not establish readiness; not sent")

    def wait_ready(self, *, timeout: float = _STARTUP_TIMEOUT_SECS) -> None:
        """Wait for startup without offering or scheduling any request frame.

        Resume calls this outside its ownership lock before spending the short
        owner-query budget. Mutation controls keep their single total deadline.
        """
        self._ensure_started_by(time.monotonic() + timeout)

    def _send_owner_control_bounded(self, frame: dict[str, Any], *, deadline: float,
                                    expected_boot_id: str | None = None) -> None:
        self._ensure_started_by(deadline)
        self._send_frame(frame, deadline=deadline, expected_boot_id=expected_boot_id)

    def interrupt(self, sid: str, *, request_id: str | None = None,
                  target_request_id: str | None = None, wait: bool = False,
                  timeout: float = 5.0, expected_boot_id: str | None = None) -> dict | None:
        if target_request_id is not None and (type(target_request_id) is not str or not target_request_id):
            raise ValueError("invalid host interrupt target request id")
        deadline = time.monotonic() + timeout
        interrupt_id = request_id or uuid.uuid4().hex
        waiter = queue.Queue(maxsize=1) if wait else None
        if waiter is not None:
            with self._control_lock:
                if interrupt_id in self._pending_controls:
                    raise HostSendNotSent("duplicate host interrupt request id; not sent")
                self._pending_controls[interrupt_id] = waiter
        frame = {"type": "interrupt", "sid": sid, "request_id": interrupt_id,
                 **({"target_request_id": target_request_id} if target_request_id is not None else {})}
        try:
            self._send_owner_control_bounded(frame, deadline=deadline, expected_boot_id=expected_boot_id)
            if waiter is None:
                return None
            try:
                ack = waiter.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise HostSendUncertain("compute-host interrupt acknowledgement timed out; unconfirmed") from exc
            if (ack.get("type") != "interrupt.ack" or ack.get("request_id") != interrupt_id
                    or ack.get("sid") != sid or type(ack.get("applied")) is not bool
                    or target_request_id is not None and ack.get("target_request_id") != target_request_id):
                raise HostSendUncertain("compute-host interrupt acknowledgement mismatch; unconfirmed")
            return ack
        finally:
            if waiter is not None:
                with self._control_lock:
                    self._pending_controls.pop(interrupt_id, None)

    def lookup_session_key(self, session_key: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
        """Return the host-side owner of one stored session, if exactly one exists.

        The serving process may have reaped its websocket-facing mirror while the
        persistent compute host still owns the live agent.  A resume must adopt
        that owner rather than create a second runtime ID for the same durable
        transcript.  Multiple owners are deliberately an error: guessing would
        make Stop and model controls target an arbitrary turn.
        """
        key = str(session_key or "")
        if not key:
            return None
        deadline = time.monotonic() + timeout
        self._ensure_started_by(deadline)
        expected_boot_id = self.boot_id or None
        request_id = f"session-lookup-{uuid.uuid4().hex}"
        q: queue.Queue[dict] = queue.Queue(maxsize=1)
        with self._control_lock:
            self._pending_controls[request_id] = q
        try:
            self._send_owner_control_bounded(
                {"type": "session.lookup", "request_id": request_id, "session_key": key},
                deadline=deadline, expected_boot_id=expected_boot_id,
            )
            try:
                frame = q.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise HostSendUncertain("compute-host owner lookup acknowledgement timed out; unconfirmed") from exc
        finally:
            with self._control_lock:
                self._pending_controls.pop(request_id, None)
        if frame.get("type") == "error":
            raise RuntimeError(str(frame.get("message") or "compute-host session lookup failed"))
        observed_boot_id = frame.get("_host_boot_id")
        if (type(observed_boot_id) is not str or not observed_boot_id
                or (expected_boot_id is not None and observed_boot_id != expected_boot_id)
                or observed_boot_id != self.boot_id):
            raise HostSendUncertain("compute-host owner lookup belongs to another or unknown boot; unconfirmed")
        matches = frame.get("sessions")
        if not isinstance(matches, list):
            raise RuntimeError("compute-host session lookup returned an invalid response")
        matches = [item for item in matches if isinstance(item, dict) and item.get("session_id")]
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError(
                f"compute host has {len(matches)} live runtimes for stored session {key}; refusing ambiguous ownership"
            )
        return {**matches[0], "host_boot_id": observed_boot_id}

    def observe_session(self, sid: str, callback: Callable[[dict], None], *, request_id: str,
                        expected_boot_id: str | None = None) -> None:
        """Receive only the exact re-adopted request's terminal frame."""
        if not sid or type(request_id) is not str or not request_id:
            raise ValueError("session and request identity required for host observation")
        with self._registry_lock:
            if expected_boot_id is not None and self.boot_id != expected_boot_id:
                raise HostBootMismatch("compute-host boot changed before observer registration")
            self._session_observers[sid] = (request_id, callback, self.boot_id)

    def unobserve_session(self, sid: str, *, request_id: str,
                          expected_boot_id: str | None = None) -> bool:
        """Retire only an unconsumed observer for this request and boot."""
        with self._registry_lock:
            observed = self._session_observers.get(sid)
            if observed is None or observed[0] != request_id:
                return False
            if expected_boot_id is not None and observed[2] != expected_boot_id:
                return False
            self._session_observers.pop(sid)
            return True

    def reload_mcp(self, sid: str, *, request_id: str | None = None) -> dict:
        return self.control(
            sid,
            route_name="reload.mcp",
            payload={"type": "reload_mcp", "sid": sid, "request_id": request_id or uuid.uuid4().hex},
            wait=True,
        )

    def control(
        self,
        sid: str,
        *,
        route_name: str,
        payload: dict[str, Any] | None = None,
        wait: bool = True,
        timeout: float = 30.0,
        expected_boot_id: str | None = None,
    ) -> dict:
        if route_name not in MUTATOR_ROUTE_TABLE:
            raise ValueError(f"unclassified host mutator route: {route_name}")
        deadline = time.monotonic() + timeout
        request_id = str((payload or {}).get("request_id") or uuid.uuid4().hex)
        frame = dict(payload or {})
        frame.setdefault("type", "control")
        frame["sid"] = sid
        frame["route_name"] = route_name
        frame["request_id"] = request_id
        q: queue.Queue[dict] | None = None
        if wait:
            q = queue.Queue(maxsize=1)
            with self._control_lock:
                if request_id in self._pending_controls:
                    raise HostSendNotSent("duplicate pending control request id; not sent")
                self._pending_controls[request_id] = q
        try:
            self._send_owner_control_bounded(frame, deadline=deadline, expected_boot_id=expected_boot_id)
            if q is None:
                return {"status": "sent", "request_id": request_id}
            try:
                return q.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise HostSendUncertain("compute-host control acknowledgement timed out; unconfirmed") from exc
        finally:
            if q is not None:
                with self._control_lock:
                    self._pending_controls.pop(request_id, None)

    def _spawn_locked(self, *, reason: str) -> None:
        if self._stopped_respawning:
            raise RuntimeError("compute host respawn disabled after crash loop")
        boundary = self._profile_env_boundary
        if boundary is not None:
            from agent.secret_scope import build_profile_env_boundary

            # Refresh temporal authority at every initial spawn/respawn using
            # the immutable owner identities captured at construction.  A wait
            # thread's ContextVars are not authorization, and a prior value
            # snapshot must not resurrect rotated or revoked credentials.
            boundary = build_profile_env_boundary(
                source_home=boundary.source_home,
                target_home=boundary.target_home,
            )
            self._profile_env_boundary = boundary
        env = hermes_subprocess_env(
            inherit_credentials=True,
            profile_boundary=boundary,
        )
        if self.env:
            if boundary is not None:
                explicit_env = build_subprocess_env(
                    base={},
                    extra=self.env,
                    profile_home=boundary.target_home,
                    source_profile_home=boundary.source_home,
                    enforce_profile_boundary=True,
                )
            else:
                explicit_env = build_subprocess_env(base={}, extra=self.env)
            env.update(explicit_env)
        env["HERMES_COMPUTE_HOST_HEARTBEAT_SECS"] = str(self.heartbeat_secs)
        env.setdefault("PYTHONPATH", str(_repo_root()))
        if str(_repo_root()) not in env["PYTHONPATH"].split(os.pathsep):
            env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env["PYTHONPATH"]
        proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Lossy UTF-8 decode — the compute host emits UTF-8; a
            # locale-mismatched byte must not raise inside the drain
            # threads and kill the supervisor (#52649).
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        # Reader provenance and publication of a new process share this gate.
        # Never hold it while waiting for hello from the reader thread.
        with self._registry_lock:
            self._hello_event.clear()
            self._hello = {}
            self._ready_proc = None
            self._proc = proc
        self._stdout_thread = _Thread(target=self._drain_stdout, args=(proc,), name="compute-host-stdout", daemon=True)
        self._stderr_thread = _Thread(target=self._drain_stderr, args=(proc,), name="compute-host-stderr", daemon=True)
        self._wait_thread = _Thread(target=self._wait_for_exit, args=(proc,), name="compute-host-wait", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._wait_thread.start()
        try:
            if not self._hello_event.wait(timeout=_STARTUP_TIMEOUT_SECS):
                raise RuntimeError(f"compute host did not send hello; stderr={self._stderr_tail[-5:]}")
            self._validate_hello()
            self._persist_registry()
            with self._registry_lock:
                if self._closing or self._proc is not proc or proc.poll() is not None:
                    raise RuntimeError("compute host exited before readiness")
                self._ready_proc = proc
        except Exception:
            # Retire this exact rejected child before releasing the lifecycle
            # lock. Later callers must never bypass its failed startup result.
            with self._registry_lock:
                if self._proc is proc:
                    self._proc = None
                    self._ready_proc = None
                    self._hello = {}
            self._terminate_process(proc)
            self._remove_registry()
            raise
        logger.info("compute host started pid=%s reason=%s", proc.pid, reason)

    def _validate_hello(self) -> None:
        hello = self._hello
        if not hello:
            raise RuntimeError("compute host missing hello")
        got_home = str(hello.get("hermes_home") or "")
        if got_home and got_home != self.expected_hermes_home:
            raise RuntimeError(f"compute host HERMES_HOME mismatch: {got_home} != {self.expected_hermes_home}")
        got_sha = str(hello.get("build_sha") or "")
        if self.expected_build_sha != "unknown" and got_sha not in {"", "unknown", self.expected_build_sha}:
            raise RuntimeError(f"compute host build mismatch: {got_sha} != {self.expected_build_sha}")

    def _persist_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        payload = {
            "host_pid": self.pid,
            "boot_id": self._hello.get("boot_id") or "",
            "build_sha": self._hello.get("build_sha") or "",
            "started_at": time.time(),
            "argv": self.argv,
        }
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.registry_path)

    def _remove_registry(self) -> None:
        try:
            self.registry_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("failed to remove compute host registry", exc_info=True)

    def _send_frame(self, frame: dict[str, Any], *, deadline: float | None = None,
                    expected_boot_id: str | None = None) -> None:
        """Serialize one frame, with no sender thread or post-timeout replay.

        Only POSIX pipes support this contract. Never call an arbitrary stream's
        blocking write/flush. A partial line poisons that process's transport:
        subsequent frames cannot safely be appended to it. Recovery belongs to
        the process owner, not an implicit resend of an uncertain mutation.
        """
        if deadline is None:
            deadline = time.monotonic() + 5.0
        data = (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        offered = 0
        proc = None
        boot = expected_boot_id

        def failure(message: str) -> TimeoutError:
            if offered:
                self._poisoned_proc = proc
                return HostSendUncertain(message + "; partial frame, unconfirmed")
            return HostSendNotSent(message + "; not sent")

        def acquire(lock: Any) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not lock.acquire(timeout=remaining):
                raise failure("compute-host transport deadline exceeded")

        acquire(self._write_lock)
        try:
            while offered < len(data):
                acquire(self._lock)
                try:
                    current = self._proc
                    if proc is None:
                        proc = current
                        if boot is None:
                            boot = self.boot_id
                    if current is not proc or (boot is not None and self.boot_id != boot):
                        if not offered:
                            raise HostBootMismatch("compute-host boot changed before frame write")
                        raise failure("compute-host boot changed during frame write")
                    if expected_boot_id == "":
                        raise HostBootMismatch("compute-host boot is not pinned")
                    if proc is None or proc.poll() is not None or proc.stdin is None:
                        raise failure("compute host is not running")
                    if self._poisoned_proc is proc:
                        raise failure("compute-host transport has an incomplete frame")
                    try:
                        if os.name != "posix":
                            raise OSError("bounded pipe transport unsupported on this platform")
                        fd = proc.stdin.fileno()
                        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                            raise OSError("bounded transport requires a POSIX pipe")
                        os.set_blocking(fd, False)
                        if time.monotonic() >= deadline:
                            raise failure("compute-host transport deadline exceeded")
                        # Replacement and this nonblocking syscall are atomic
                        # under _lock; no registry is held across either.
                        try:
                            count = os.write(fd, data[offered:offered + 65536])
                        except BlockingIOError:
                            count = 0
                        offered += count
                    except (OSError, ValueError, AttributeError) as exc:
                        if isinstance(exc, (HostSendNotSent, HostSendUncertain)):
                            raise
                        raise failure(str(exc)) from exc
                finally:
                    self._lock.release()
                if offered < len(data):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise failure("compute-host transport deadline exceeded")
                    try:
                        # Short poll slices revalidate replacement even when
                        # the old pipe remains full indefinitely.
                        select.select([], [fd], [], min(remaining, 0.05))
                    except (OSError, ValueError, AttributeError) as exc:
                        raise failure(str(exc)) from exc
        finally:
            self._write_lock.release()

    def _drain_stdout(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                frame = load_checkpoint_frame(raw)
            except json.JSONDecodeError:
                logger.warning("compute host emitted invalid json")
                continue
            if isinstance(frame, dict):
                with self._registry_lock:
                    if self._proc is not proc:
                        return
                    # This tag is assigned by the pipe owner, not trusted from
                    # an arbitrary payload. Hello establishes that owner's boot.
                    if frame.get("type") != "hello":
                        frame = {**frame, "_host_boot_id": self.boot_id}
                    if frame.get("type") != "rpc":
                        self._handle_host_frame(frame)
                        continue
                # Output delivery can block. It must not hold the ownership
                # registry or prevent control/observer registration. Carry its
                # observed origin with the event for consumer reconciliation.
                if self._proc is proc:
                    self._handle_host_frame(frame)

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            text = raw.rstrip("\n")
            if text:
                self._stderr_tail = (self._stderr_tail + [text])[-80:]
                logger.warning("compute host stderr: %s", text)

    def _handle_host_frame(self, frame: dict[str, Any]) -> None:
        ftype = str(frame.get("type") or "")
        if ftype == "hello":
            self._hello = dict(frame)
            self._hello_event.set()
            return
        if ftype == "hb":
            self._last_progress_counter = int(frame.get("progress_counter") or self._last_progress_counter)
            logger.debug("compute host heartbeat: %s", frame)
            return
        if ftype == "rpc":
            message = frame.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("params"), dict) and frame.get("_host_boot_id"):
                    message = {**message, "params": {**message["params"], "host_boot_id": frame["_host_boot_id"]}}
                self.rpc_sink(message)
            return
        if ftype in {"turn.end", "turn.error"}:
            self._complete_turn(frame)
            return
        if ftype in {
            "control.ack",
            "control.error",
            "interrupt.ack",
            "reload_mcp.ack",
            "session.lookup.ack",
            "shutdown.ack",
        }:
            request_id = str(frame.get("request_id") or "")
            with self._control_lock:
                q = self._pending_controls.get(request_id)
            if q is not None:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass
            return
        if ftype == "error" and frame.get("request_id"):
            request_id = str(frame.get("request_id") or "")
            with self._control_lock:
                q = self._pending_controls.get(request_id)
            if q is not None:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass

    def _complete_turn(self, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("request_id") or "")
        sid = str(frame.get("sid") or "")
        boot_id = str(frame.get("_host_boot_id", self.boot_id))
        with self._registry_lock:
            pending = self._pending_turns.get(request_id)
            if pending is not None and (pending[0] != sid or pending[2] != boot_id):
                # A malformed or stale terminal cannot consume another owner.
                return
            pending = self._pending_turns.pop(request_id, None)
            observed = self._session_observers.get(sid)
            observer = None
            if observed is not None and observed[0] == request_id and observed[2] == boot_id:
                self._session_observers.pop(sid)
                observer = observed[1]
            # A re-adopted mirror is the sole current projection owner.
            callback = observer if observer is not None else (pending[1] if pending is not None else None)
            if callback is None:
                return
            if boot_id:
                frame = {**frame, "_host_boot_id": boot_id}
            self._terminal_queues.setdefault(sid, deque()).append((dict(frame), callback))
            launch = sid not in self._terminal_workers
            if launch:
                self._terminal_workers.add(sid)
        if launch:
            try:
                _Thread(target=self._settle_terminals, args=(sid,),
                        name="compute-host-terminal", daemon=True).start()
            except Exception:
                with self._registry_lock:
                    self._terminal_workers.discard(sid)
                # Retain the unstarted work. Running it on this thread would
                # reintroduce ACK starvation; publication failure is not success.
                logger.exception("could not start terminal settlement worker for %s", sid)

    def _settle_terminals(self, sid: str) -> None:
        """One callback worker per SID; no callback executes on the ACK reader."""
        while True:
            with self._registry_lock:
                waiting = self._terminal_queues.get(sid)
                if not waiting:
                    self._terminal_queues.pop(sid, None)
                    self._terminal_workers.discard(sid)
                    return
                frame, callback = waiting.popleft()
            try:
                callback(frame)
            except Exception:
                logger.exception("compute host terminal projection failed for %s", sid)

    def _wait_for_exit(self, proc: subprocess.Popen[str]) -> None:
        code = proc.wait()
        if self._closing:
            return
        with self._lock:
            if self._proc is not proc:
                return
            with self._registry_lock:
                self._proc = None
                self._ready_proc = None
                self._hello = {}
        self._remove_registry()
        self._fail_pending_turns(reason="crash", message=f"compute host exited with code {code}")
        if self.on_crash is not None:
            try:
                self.on_crash()
            except Exception:
                logger.exception("compute host crash notification failed")
        self._maybe_respawn_after_crash()

    def _fail_pending_turns(self, *, reason: str, message: str) -> None:
        with self._registry_lock:
            pending = {(request_id, sid, boot) for request_id, (sid, _cb, boot) in self._pending_turns.items()}
            pending.update((request_id, sid, boot) for sid, (request_id, _cb, boot) in self._session_observers.items())
        for request_id, sid, boot in pending:
            frame = {
                "type": "turn.error",
                "sid": sid,
                "request_id": request_id,
                "_host_boot_id": boot,
                "reason": reason,
                "message": message,
            }
            try:
                self.rpc_sink(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": sid,
                            "payload": {"message": message, "reason": reason},
                        },
                    }
                )
            except Exception:
                logger.exception("compute host crash notification failed")
            self._complete_turn(frame)

    def _maybe_respawn_after_crash(self) -> None:
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t <= _RESPAWN_WINDOW_SECS]
        if len(self._restart_times) >= self.respawn_max:
            self._stopped_respawning = True
            logger.error("compute host crash loop: max %s restarts per 5min reached; not respawning", self.respawn_max)
            return
        self._restart_times.append(now)
        # Small bounded backoff; tests and first recovery stay quick.
        delay = min(5.0, 0.25 * (2 ** max(0, len(self._restart_times) - 1)))

        def _respawn() -> None:
            time.sleep(delay)
            with self._lock:
                if self._closing or self._stopped_respawning or self._proc is not None:
                    return
                try:
                    self._spawn_locked(reason="crash")
                except Exception:
                    logger.exception("compute host respawn failed")

        _Thread(target=_respawn, name="compute-host-respawn", daemon=True).start()

    def _pid_matches_compute_host(self, pid: int) -> bool:
        return is_compute_host_identity(pid)

    def _terminate_pid(self, pid: int, *, timeout: float = _SHUTDOWN_TIMEOUT_SECS) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("failed to SIGTERM compute host pid=%s", pid, exc_info=True)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("failed to SIGKILL compute host pid=%s", pid, exc_info=True)

    def _terminate_process(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECS)
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


__all__ = [
    "MUTATOR_ROUTE_TABLE",
    "HostSupervisor",
    "append_log_record",
    "is_compute_host_identity",
]
