"""Read-only native E2E state observer; never import SessionDB or migrate."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def process_role(argv):
    # Stop at Python's execution boundary: code/script arguments are data,
    # not a second interpreter invocation. Never scan the -c script body.
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in (b'-c', b'--'):
            return None
        if arg == b'-m':
            module = argv[index + 1] if index + 1 < len(argv) else None
            if module == b'tui_gateway.compute_host':
                return 'host'
            if module == b'hermes_cli.main' and b'serve' in argv[index + 2:]:
                return 'serve'
            return None
        if not arg.startswith(b'-'):
            return None
        index += 2 if arg in (b'-W', b'-X', b'--check-hash-based-pycs') else 1
    return None


def _read_once(home, include_processes, timeout=2):
    result = {'receipts': [], 'messages': [], 'processes': []}
    for dbfile in home.rglob('state.db'):
        db = sqlite3.connect(dbfile.as_uri() + '?mode=ro', uri=True, timeout=timeout)
        try:
            db.execute('BEGIN')
            tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
            if 'state_meta' in tables:
                meta = dict(db.execute('select key,value from state_meta'))
                for key, raw in meta.items():
                    if ':event:' not in key or key.endswith(':origin'):
                        continue
                    value = json.loads(raw)
                    if value.get('schema') not in ('SessionDBContextInputV1', 'SessionDBContextInputV2'):
                        continue
                    origin_raw = meta.get(key + ':origin')
                    result['receipts'].append({'db': str(dbfile.relative_to(home)), 'receipt': value,
                        'origin': json.loads(origin_raw) if origin_raw else None})
            if 'messages' in tables:
                result['messages'].extend({'db': str(dbfile.relative_to(home)), 'session_id': r[0], 'role': r[1], 'content': r[2]}
                    for r in db.execute("select session_id,role,content from messages where role='user'"))
        finally:
            db.close()
    if include_processes and sys.platform == 'linux':
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                env = (directory / 'environ').read_bytes().split(b'\0')
                sandboxed = any(e.startswith(b'HERMES_HOME=') and os.fsdecode(e.split(b'=', 1)[1]) == str(home) for e in env)
                if not sandboxed:
                    continue
                argv = (directory / 'cmdline').read_bytes().split(b'\0')
                role = process_role(argv)
                if role:
                    fields = (directory / 'stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
                    result['processes'].append({'pid': int(directory.name), 'birth': fields[19], 'role': role})
            except (OSError, ValueError):
                continue
    return result


def read_snapshot(home, *, include_processes=True):
    # Preserve the caller's existing ten-second subprocess limit. Every
    # attempt rebuilds the complete observation; no partial state escapes.
    deadline = time.monotonic() + 8
    attempts = busy = 0
    while True:
        attempts += 1
        try:
            remaining = max(.001, deadline - time.monotonic())
            result = _read_once(home, include_processes, timeout=min(1, remaining))
            result.update(readback_attempts=attempts, busy_observations=busy)
            return result
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            busy += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            print('SQLITE_READBACK_BUSY', file=sys.stderr, flush=True)
            time.sleep(min(.05, remaining))


def _read_process_birth(pid):
    fields = (Path('/proc') / str(pid) / 'stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
    return fields[19]


def observe_process_identity(pid, birth):
    result = {'pid': pid, 'birth': birth, 'status': 'unreadable'}
    if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth.isascii() or not birth.isdigit():
        return result
    try:
        observed = _read_process_birth(pid)
    except FileNotFoundError:
        try:
            # Missing stat alone is not death: verify the proc namespace is
            # readable and the exact process directory is actually absent.
            Path('/proc/self/stat').stat()
        except OSError:
            return result
        try:
            (Path('/proc') / str(pid)).stat()
        except FileNotFoundError:
            result['status'] = 'gone'
        except OSError:
            pass
        return result
    except (OSError, ValueError, IndexError):
        return result
    result['observed_birth'] = observed
    # Identity remains present even for a zombie or an exec/argv change.
    result['status'] = 'alive' if observed == birth else 'replaced'
    return result


def main():
    home = Path(sys.argv[1]).resolve()
    assert home.name == 'hermes-home' and home.parent.name.startswith('hermes-e2e-')
    previous = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
    result = read_snapshot(home)
    result['prior_owners'] = [observe_process_identity(row['pid'], row['birth']) for row in previous]
    print(json.dumps(result))


if __name__ == '__main__':
    main()
