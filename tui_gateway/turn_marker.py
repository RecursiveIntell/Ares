"""Best-effort interrupted-turn hints for Desktop/TUI, not outcome authority.

A running turn's progress can live only in process memory until SessionDB is
updated, so an app/backend/machine death mid-turn may leave no durable prompt
row. This sidecar records a bounded prompt hint at turn start and normally
clears it at conclusion. A retained marker SUGGESTS interruption, but a failed
write or clear, or a same-UID modification, means it cannot prove what ran,
what effect occurred, or whether a terminal frame reached the client.
``session.resume`` may present it as an advisory error snapshot; optional
auto-continue is a separate explicit config path, not permission from a marker.

Markers are stored per ``HERMES_HOME`` (callers pass the session's home so
profile sessions keep their state in their own profile directory). Future
writes prune entries older than ``_MAX_AGE_SECS`` and cap the count; a quiet
file does not expire itself on read.

Every function is best-effort by design — marker bookkeeping must never
break a turn — so I/O errors degrade to "no marker" instead of raising.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MARKER_DIR = "desktop"
_MARKER_FILE = "interrupted_turns.json"
_MAX_AGE_SECS = 24 * 3600
_MAX_ENTRIES = 32
# Enough to re-submit any realistic prompt; guards the sidecar against a
# pathological multi-megabyte paste being journaled on every turn.
_MAX_PROMPT_CHARS = 64_000

_lock = threading.Lock()


def _marker_path(home: Path | str) -> Path:
    return Path(home) / _MARKER_DIR / _MARKER_FILE


def _load(path: Path) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("unreadable turn-marker file %s; starting fresh", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _prune(entries: dict[str, dict], now: float) -> dict[str, dict]:
    fresh = {
        key: entry
        for key, entry in entries.items()
        if now - float(entry.get("started_at") or 0) <= _MAX_AGE_SECS
    }
    if len(fresh) <= _MAX_ENTRIES:
        return fresh
    newest = sorted(
        fresh.items(),
        key=lambda item: float(item[1].get("started_at") or 0),
        reverse=True,
    )[:_MAX_ENTRIES]
    return dict(newest)


def _store(path: Path, entries: dict[str, dict]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".turn-marker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_turn_start(
    home: Path | str, session_key: str, prompt: str, *, attempts: int = 0
) -> None:
    """Persist the marker for a turn that is about to run.

    ``attempts`` counts how many auto-continues led to this run: 0 for a
    user-initiated turn, N for the Nth automatic re-run — the crash-loop
    breaker reads it back on the next resume.
    """
    if not session_key or not prompt:
        return
    now = time.time()
    entry = {
        "attempts": max(0, int(attempts)),
        "prompt": prompt[:_MAX_PROMPT_CHARS],
        "started_at": now,
    }
    try:
        with _lock:
            path = _marker_path(home)
            entries = _prune(_load(path), now)
            entries[session_key] = entry
            _store(path, entries)
    except Exception:
        logger.debug("failed to record turn marker for %s", session_key, exc_info=True)


def clear_turn_marker(home: Path | str, session_key: str) -> None:
    """Remove the marker once its turn concluded (any outcome the client saw)."""
    if not session_key:
        return
    try:
        with _lock:
            path = _marker_path(home)
            entries = _load(path)
            if session_key not in entries:
                return
            del entries[session_key]
            _store(path, entries)
    except Exception:
        logger.debug("failed to clear turn marker for %s", session_key, exc_info=True)


def read_turn_marker(home: Path | str, session_key: str) -> dict[str, Any] | None:
    """Return a surviving, unauthenticated prompt hint, or None."""
    if not session_key:
        return None
    try:
        with _lock:
            entry = _load(_marker_path(home)).get(session_key)
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    prompt = str(entry.get("prompt") or "")
    if not prompt.strip():
        return None
    try:
        started_at = float(entry.get("started_at") or 0)
        attempts = max(0, int(entry.get("attempts") or 0))
    except (TypeError, ValueError):
        return None
    return {"attempts": attempts, "prompt": prompt, "started_at": started_at}
