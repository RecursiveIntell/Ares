"""``hermes logs`` — view and filter Hermes log files.

Supports tailing, following, session filtering, level filtering,
component filtering, and relative time ranges.  All log files live
under ``~/.hermes/logs/``.

Usage examples::

    hermes logs                    # last 50 lines of agent.log
    hermes logs -f                 # follow agent.log in real time
    hermes logs errors             # last 50 lines of errors.log
    hermes logs gateway -n 100    # last 100 lines of gateway.log
    hermes logs gui -f            # follow gui.log (dashboard/pty/ws)
    hermes logs desktop -f        # follow desktop.log (Electron app boot/backend)
    hermes logs --level WARNING    # only WARNING+ lines
    hermes logs --session abc123   # filter by session ID substring
    hermes logs --component tools  # only tool-related lines
    hermes logs --since 1h         # lines from the last hour
    hermes logs --since 30m -f     # follow, starting 30 min ago
"""

import io
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from typing import Optional, Sequence

from hermes_constants import get_hermes_home, display_hermes_home

# Known log files (name → filename)
LOG_FILES = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
    "gui": "gui.log",
    "desktop": "desktop.log",
    # Legacy raw stderr and the index of profile-owned stdio attempt files.
    "mcp": "mcp-stderr.log",
}

# Log line timestamp regex — matches "2026-04-05 22:35:00,123" or
# "2026-04-05 22:35:00" at the start of a line.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")

# Level extraction — matches " INFO ", " WARNING ", " ERROR ", " DEBUG ", " CRITICAL "
_LEVEL_RE = re.compile(r"\s(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s")

# Logger name extraction — after level and optional session tag, the next
# non-space token before ":" is the logger name.
# Matches: "INFO gateway.run:" or "INFO [sess_abc] tools.terminal_tool:"
_LOGGER_NAME_RE = re.compile(
    r"\s(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)"  # level
    r"(?:\s+\[.*?\])?"                           # optional session tag
    r"\s+(\S+):"                                 # logger name
)

# Level ordering for >= filtering
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _parse_since(since_str: str) -> Optional[datetime]:
    """Parse a relative time string like '1h', '30m', '2d' into a datetime cutoff.

    Returns None if the string can't be parsed.
    """
    since_str = since_str.strip().lower()
    match = re.match(r"^(\d+)\s*([smhd])$", since_str)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    delta = {
        "s": timedelta(seconds=value),
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
    }[unit]
    return datetime.now() - delta


def _parse_line_timestamp(line: str) -> Optional[datetime]:
    """Extract timestamp from a log line. Returns None if not parseable."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _extract_level(line: str) -> Optional[str]:
    """Extract the log level from a line."""
    m = _LEVEL_RE.search(line)
    return m.group(1) if m else None


def _extract_logger_name(line: str) -> Optional[str]:
    """Extract the logger name from a log line."""
    m = _LOGGER_NAME_RE.search(line)
    return m.group(1) if m else None


def _line_matches_component(line: str, prefixes: Sequence[str]) -> bool:
    """Check if a log line's logger name starts with any of *prefixes*."""
    name = _extract_logger_name(line)
    if name is None:
        return False
    return name.startswith(tuple(prefixes))


def _matches_filters(
    line: str,
    *,
    min_level: Optional[str] = None,
    session_filter: Optional[str] = None,
    since: Optional[datetime] = None,
    component_prefixes: Optional[Sequence[str]] = None,
) -> bool:
    """Check if a log line passes all active filters."""
    if since is not None:
        ts = _parse_line_timestamp(line)
        if ts is not None and ts < since:
            return False

    if min_level is not None:
        level = _extract_level(line)
        if level is not None:
            if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(min_level, 0):
                return False

    if session_filter is not None:
        if session_filter not in line:
            return False

    if component_prefixes is not None:
        if not _line_matches_component(line, component_prefixes):
            return False

    return True


def tail_log(
    log_name: str = "agent",
    *,
    num_lines: int = 50,
    follow: bool = False,
    level: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[str] = None,
    component: Optional[str] = None,
) -> None:
    """Read and display log lines, optionally following in real time.

    Parameters
    ----------
    log_name
        Which log to read: ``"agent"``, ``"errors"``, ``"gateway"``, ``"gui"``.
    num_lines
        Number of recent lines to show (before follow starts).
    follow
        If True, keep watching for new lines (Ctrl+C to stop).
    level
        Minimum log level to show (e.g. ``"WARNING"``).
    session
        Session ID substring to filter on.
    since
        Relative time string (e.g. ``"1h"``, ``"30m"``).
    component
        Component name to filter by (e.g. ``"gateway"``, ``"tools"``).
    """
    filename = LOG_FILES.get(log_name)
    if filename is None:
        print(f"Unknown log: {log_name!r}. Available: {', '.join(sorted(LOG_FILES))}")
        sys.exit(1)

    log_path = get_hermes_home() / "logs" / filename
    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        print("(Logs are created when Hermes runs — try 'hermes chat' first)")
        sys.exit(1)

    # Parse --since into a datetime cutoff
    since_dt = None
    if since:
        since_dt = _parse_since(since)
        if since_dt is None:
            print(f"Invalid --since value: {since!r}. Use format like '1h', '30m', '2d'.")
            sys.exit(1)

    min_level = level.upper() if level else None
    if min_level and min_level not in _LEVEL_ORDER:
        print(f"Invalid --level: {level!r}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        sys.exit(1)

    # Resolve component to logger name prefixes
    component_prefixes = None
    if component:
        from hermes_logging import COMPONENT_PREFIXES
        component_lower = component.lower()
        if component_lower not in COMPONENT_PREFIXES:
            available = ", ".join(sorted(COMPONENT_PREFIXES))
            print(f"Unknown component: {component!r}. Available: {available}")
            sys.exit(1)
        component_prefixes = COMPONENT_PREFIXES[component_lower]

    has_filters = (
        min_level is not None
        or session is not None
        or since_dt is not None
        or component_prefixes is not None
    )

    # Read and display the tail
    try:
        read_tail = _read_mcp_tail if log_name == "mcp" else _read_tail
        lines = read_tail(log_path, num_lines, has_filters=has_filters,
                           min_level=min_level, session_filter=session,
                           since=since_dt, component_prefixes=component_prefixes)
    except PermissionError:
        print(f"Permission denied: {log_path}")
        sys.exit(1)

    # Print header
    filter_parts = []
    if min_level:
        filter_parts.append(f"level>={min_level}")
    if session:
        filter_parts.append(f"session={session}")
    if component:
        filter_parts.append(f"component={component}")
    if since:
        filter_parts.append(f"since={since}")
    filter_desc = f" [{', '.join(filter_parts)}]" if filter_parts else ""

    if follow:
        print(f"--- {display_hermes_home()}/logs/{filename}{filter_desc} (Ctrl+C to stop) ---")
    else:
        print(f"--- {display_hermes_home()}/logs/{filename}{filter_desc} (last {num_lines}) ---")

    for line in lines:
        print(line, end="")

    if not follow:
        return

    # Follow mode — poll for new content
    try:
        follow_log = _follow_mcp_log if log_name == "mcp" else _follow_log
        follow_log(log_path, min_level=min_level, session_filter=session,
                     since=since_dt, component_prefixes=component_prefixes)
    except KeyboardInterrupt:
        print("\n--- stopped ---")


_MCP_MAX_ATTEMPTS = 128
_MCP_INDEX_LINES = 2000
_MCP_TAIL_BYTES = 1048576
_MCP_MAX_PREVIEW_LINES = 10000
_MCP_FOLLOW_BYTES = 65536
_MCP_MAX_LINE_BYTES = 65536


class _MCPPreviewNotice(str):
    """Reader metadata, distinct from child content subject to log filters."""


def _mcp_open_regular(path: Path):
    return open(path, "rb", opener=lambda name, flags: os.open(
        name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    ))


def _mcp_oversized_line(index: bool) -> str:
    source = "index" if index else "stderr"
    return _MCPPreviewNotice(f"MCP {source} line exceeds MCP preview limit; full content remains in the log file.\n")


def _mcp_tail_rows(path: Path, n: int, *, index: bool = False) -> list:
    """A finite byte window; the generic log reader is unchanged."""
    if n <= 0:
        return []
    rows = deque(maxlen=min(n, _MCP_MAX_PREVIEW_LINES))
    try:
        with _mcp_open_regular(path) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return []
            start = max(0, info.st_size - _MCP_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(_MCP_TAIL_BYTES)
        if start:
            fragment, separator, data = data.partition(b"\n")
            if not separator or len(fragment) > _MCP_MAX_LINE_BYTES:
                rows.append(_mcp_oversized_line(index))
        for row in io.BytesIO(data):
            payload = row.removesuffix(b"\n").removesuffix(b"\r")
            if len(payload) > _MCP_MAX_LINE_BYTES:
                rows.append(_mcp_oversized_line(index))
            else:
                decoded = row.decode("utf-8", errors="replace")
                # An EOF preview is a display record, even before the child
                # finishes its line. The stored bytes remain unchanged.
                rows.append(decoded if decoded.endswith("\n") else decoded + "\n")
    except OSError:
        return []
    return list(rows)


def _mcp_control_record(line: str) -> Optional[dict]:
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) and record.get("kind") == "mcp.stdio.attempt" else None


def _mcp_attempt(record: dict, index: Path) -> Optional[dict]:
    """Resolve only this profile's UUID-named capture, never an arbitrary path."""
    attempt = record.get("attempt_id")
    if (not isinstance(attempt, str) or re.fullmatch(r"[0-9a-f]{32}", attempt) is None
            or not isinstance(record.get("server"), str)
            or type(record.get("parent_pid")) is not int or record["parent_pid"] <= 0
            or record.get("destination") != "file"
            or not isinstance(record.get("config_home"), str)
            or not isinstance(record.get("stderr_path"), str)):
        return None
    try:
        home = index.parent.parent.resolve()
        expected = home / "logs" / "mcp-stderr" / f"{attempt}.log"
        if (Path(record["config_home"]).resolve() != home
                or Path(record["stderr_path"]).resolve() != expected
                or not expected.is_file()):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return {"attempt_id": attempt, "server": record["server"], "parent_pid": record["parent_pid"],
            "config_home": str(home), "stderr_path": str(expected), "destination": "file"}


def _mcp_child_line(line: str, record: dict, filters: dict) -> Optional[str]:
    control = _mcp_control_record(line)
    if control is not None and control.get("attempt_id") == record["attempt_id"]:
        return None
    owner = " ".join(f"{key}={json.dumps(value)}" for key, value in (
        ("profile", record["config_home"]), ("server", record["server"]),
        ("attempt", record["attempt_id"]),
    ))
    rendered = f"[mcp {owner}] {line}"
    session = filters.get("session_filter")
    if session is not None and session not in rendered:
        return None
    if isinstance(line, _MCPPreviewNotice):
        return rendered
    if not _matches_filters(line, **{**filters, "session_filter": None}):
        return None
    return rendered


def _read_mcp_tail(path: Path, num_lines: int, *, has_filters: bool = False, **filters) -> list:
    """Read actual stderr in attempt order; legacy rows keep their own format."""
    if num_lines <= 0:
        return []
    requested = num_lines
    num_lines = min(num_lines, _MCP_MAX_PREVIEW_LINES)
    result = []
    remaining = _MCP_TAIL_BYTES
    exhausted = False
    seen = set()

    def append(line):
        nonlocal remaining, exhausted
        if len(line) > remaining:
            exhausted = True
            return
        result.append(line)
        remaining -= len(line)

    for line in reversed(_mcp_tail_rows(path, max(num_lines * 20, _MCP_INDEX_LINES), index=True)):
        control = _mcp_control_record(line)
        if control is None:
            if isinstance(line, _MCPPreviewNotice) or _matches_filters(line, **filters):
                append(line)
        else:
            record = _mcp_attempt(control, path)
            if record is None or record["attempt_id"] in seen or len(seen) >= _MCP_MAX_ATTEMPTS:
                continue
            seen.add(record["attempt_id"])
            rows = _mcp_tail_rows(Path(record["stderr_path"]),
                                  max(num_lines * 20, _MCP_INDEX_LINES) if has_filters else num_lines + 4)
            for row in reversed(rows):
                rendered = _mcp_child_line(row, record, filters)
                if rendered is not None:
                    append(rendered)
                if exhausted or len(result) >= num_lines:
                    break
        if exhausted or len(result) >= num_lines:
            break
    lines = list(reversed(result))
    if exhausted or requested > _MCP_MAX_PREVIEW_LINES:
        lines.insert(0, "MCP preview output limit reached; full content remains in the log files.\n")
    return lines


def _mcp_cursor(path: Path):
    try:
        with _mcp_open_regular(path) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            # Seed only the bounded final fragment. If it later completes,
            # display its complete new record; never replay complete history.
            start = max(0, info.st_size - _MCP_MAX_LINE_BYTES - 1)
            stream.seek(start)
            recent = stream.read(min(info.st_size, _MCP_MAX_LINE_BYTES + 1))
            pending = recent.rsplit(b"\n", 1)[-1]
            dropping = len(pending) > _MCP_MAX_LINE_BYTES
            return ((info.st_dev, info.st_ino), info.st_size, b"" if dropping else pending, dropping)
    except OSError:
        return None


def _mcp_read_chunk(path: Path, cursor, *, index: bool = False):
    """Frame bounded binary chunks; retain fragments, never file descriptors."""
    try:
        with _mcp_open_regular(path) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return [], cursor
            identity = (info.st_dev, info.st_ino)
            retained = cursor is not None and cursor[0] == identity and cursor[1] <= info.st_size
            offset, pending, dropping = cursor[1:] if retained else (0, b"", False)
            stream.seek(offset)
            chunk = stream.read(_MCP_FOLLOW_BYTES)
            offset = stream.tell()
        data = pending + chunk
        lines = []
        if dropping:
            _fragment, separator, data = data.partition(b"\n")
            if not separator:
                return [], (identity, offset, b"", True)
        parts = data.split(b"\n")
        for row in parts[:-1]:
            lines.append(_mcp_oversized_line(index) if len(row) > _MCP_MAX_LINE_BYTES
                         else row.decode("utf-8", errors="replace") + "\n")
        pending = parts[-1]
        dropping = len(pending) > _MCP_MAX_LINE_BYTES
        if dropping:
            lines.append(_mcp_oversized_line(index))
            pending = b""
        return lines, (identity, offset, pending, dropping)
    except OSError:
        return [], cursor


def _follow_mcp_log(path: Path, **filters) -> None:
    records = {}

    def remember(line):
        control = _mcp_control_record(line)
        record = _mcp_attempt(control, path) if control is not None else None
        if record is not None:
            records[record["attempt_id"]] = record
            if len(records) > _MCP_MAX_ATTEMPTS:
                records.pop(next(iter(records)))
                print("MCP follow preview retains the latest 128 attempts; older captures remain in the log files.")

    for line in _mcp_tail_rows(path, _MCP_INDEX_LINES, index=True):
        remember(line)
    positions = {key: _mcp_cursor(Path(record["stderr_path"])) for key, record in records.items()}
    for key, cursor in positions.items():
        if cursor is not None and cursor[3]:
            rendered = _mcp_child_line(_mcp_oversized_line(False), records[key], filters)
            if rendered is not None:
                print(rendered, end="")
    index_cursor = _mcp_cursor(path)
    if index_cursor is not None and index_cursor[3]:
        print(_mcp_oversized_line(True), end="")
    while True:
        lines, index_cursor = _mcp_read_chunk(path, index_cursor, index=True)
        for line in lines:
            if _mcp_control_record(line) is not None:
                remember(line)
            elif isinstance(line, _MCPPreviewNotice) or _matches_filters(line, **filters):
                print(line, end="")
                sys.stdout.flush()
        for key, record in records.items():
            if _mcp_attempt(record, path) is None:
                continue
            lines, positions[key] = _mcp_read_chunk(Path(record["stderr_path"]), positions.get(key))
            for line in lines:
                rendered = _mcp_child_line(line, record, filters)
                if rendered is not None:
                    print(rendered, end="")
                    sys.stdout.flush()
        positions = {key: positions[key] for key in records if key in positions}
        time.sleep(0.3)


def _read_tail(
    path: Path,
    num_lines: int,
    *,
    has_filters: bool = False,
    min_level: Optional[str] = None,
    session_filter: Optional[str] = None,
    since: Optional[datetime] = None,
    component_prefixes: Optional[Sequence[str]] = None,
) -> list:
    """Read the last *num_lines* matching lines from a log file.

    When filters are active, we read more raw lines to find enough matches.
    """
    if has_filters:
        # Read more lines to ensure we get enough after filtering.
        # For large files, read last 10K lines and filter down.
        raw_lines = _read_last_n_lines(path, max(num_lines * 20, 2000))
        filtered = [
            l for l in raw_lines
            if _matches_filters(l, min_level=min_level,
                                session_filter=session_filter, since=since,
                                component_prefixes=component_prefixes)
        ]
        return filtered[-num_lines:]
    else:
        return _read_last_n_lines(path, num_lines)


def _read_last_n_lines(path: Path, n: int) -> list:
    """Efficiently read the last N lines from a file.

    For files under 1MB, reads the whole file (fast, simple).
    For larger files, reads chunks from the end.
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return []

        # For files up to 1MB, just read the whole thing — simple and correct.
        if size <= 1_048_576:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            return all_lines[-n:]

        # For large files, read chunks from the end.
        with open(path, "rb") as f:
            chunk_size = 8192
            lines = []
            pos = size

            while pos > 0 and len(lines) <= n + 1:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                chunk_lines = chunk.split(b"\n")
                if lines:
                    # Merge the last partial line of the new chunk with the
                    # first partial line of what we already have.
                    lines[0] = chunk_lines[-1] + lines[0]
                    lines = chunk_lines[:-1] + lines
                else:
                    lines = chunk_lines
                chunk_size = min(chunk_size * 2, 65536)

            # Decode and return last N non-empty lines.
            decoded = []
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    decoded.append(raw.decode("utf-8", errors="replace") + "\n")
                except Exception:
                    decoded.append(raw.decode("latin-1") + "\n")
            return decoded[-n:]

    except Exception:
        # Fallback: read entire file
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return all_lines[-n:]


def _follow_log(
    path: Path,
    *,
    min_level: Optional[str] = None,
    session_filter: Optional[str] = None,
    since: Optional[datetime] = None,
    component_prefixes: Optional[Sequence[str]] = None,
) -> None:
    """Poll a log file for new content and print matching lines."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        # Seek to end
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                if _matches_filters(line, min_level=min_level,
                                    session_filter=session_filter, since=since,
                                    component_prefixes=component_prefixes):
                    print(line, end="")
                    sys.stdout.flush()
            else:
                time.sleep(0.3)


def list_logs() -> None:
    """Print available log files with sizes."""
    log_dir = get_hermes_home() / "logs"
    if not log_dir.exists():
        print(f"No logs directory at {display_hermes_home()}/logs/")
        return

    print(f"Log files in {display_hermes_home()}/logs/:\n")
    found = False
    for entry in sorted(log_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".log":
            size = entry.stat().st_size
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if size < 1024:
                size_str = f"{size}B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f}KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f}MB"
            age = datetime.now() - mtime
            if age.total_seconds() < 60:
                age_str = "just now"
            elif age.total_seconds() < 3600:
                age_str = f"{int(age.total_seconds() / 60)}m ago"
            elif age.total_seconds() < 86400:
                age_str = f"{int(age.total_seconds() / 3600)}h ago"
            else:
                age_str = mtime.strftime("%Y-%m-%d")
            print(f"  {entry.name:<25} {size_str:>8}   {age_str}")
            found = True

    if not found:
        print("  (no log files yet — run 'hermes chat' to generate logs)")
