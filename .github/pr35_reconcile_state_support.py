#!/usr/bin/env python3
"""Restore Ares SessionDB references to the modular canonical owners.

Temporary PR35 reconciliation scaffolding. The branch keeps the Hermes v0.21.1
modular SessionDB shape; surviving Ares call sites must import/re-export those
module owners rather than duplicating their implementations in hermes_state.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PINNED_MAIN = "94a17d17521d92407b6418e57e755e279e40cc6f"


def between(text: str, start: str, end: str) -> str:
    if text.count(start) != 1:
        raise SystemExit(f"unexpected ownership start anchor: {start!r}")
    a = text.index(start)
    try:
        b = text.index(end, a + len(start))
    except ValueError as exc:
        raise SystemExit(
            f"missing ownership end anchor after {start!r}: {end!r}"
        ) from exc
    return text[a:b]


def main() -> None:
    path = Path("hermes_state.py")
    merged = path.read_text(encoding="utf-8")
    ares_main = subprocess.check_output(
        ["git", "show", f"{PINNED_MAIN}:hermes_state.py"], text=True
    )

    # Re-export current Ares common owners while retaining the modular
    # SessionDB's db-file identity alias.  Both names resolve to the single
    # canonical implementation in hermes_state_common; no helper is copied.
    common_start = "from hermes_state_common import ("
    merged_common_end = "from hermes_state_errors import (\n"
    main_common_end = "from hermes_state_portability import SessionPortabilityMixin\n"
    merged_common = between(merged, common_start, merged_common_end)
    canonical_common = between(ares_main, common_start, main_common_end)
    identity_alias = "    stat_db_file_identity as _stat_db_file_identity,\n"
    if identity_alias not in canonical_common:
        escape_alias = "    escape_like as _escape_like,\n"
        if canonical_common.count(escape_alias) != 1:
            raise SystemExit("unexpected current-main escape_like import shape")
        canonical_common = canonical_common.replace(
            escape_alias, escape_alias + identity_alias, 1
        )
    merged = merged.replace(merged_common, canonical_common, 1)

    # The modular SessionDB split moved these helpers into hermes_state_sessions.
    # Import/re-export that canonical owner instead of recreating local copies.
    old_sessions_import = "from hermes_state_sessions import SessionSessionsMixin\n"
    canonical_sessions_import = (
        "from hermes_state_sessions import (\n"
        "    SessionSessionsMixin,\n"
        "    _MODEL_CONFIG_ROW_MISSING,\n"
        "    _collect_delegate_child_ids,\n"
        "    _cwd_prefix_clause,\n"
        "    _delete_delegate_children,\n"
        "    _delegate_from_json,\n"
        "    _workspace_key_clause,\n"
        "    classify_session_status,\n"
        "    workspace_key,\n"
        ")\n"
    )
    if canonical_sessions_import not in merged:
        if merged.count(old_sessions_import) != 1:
            raise SystemExit("unexpected hermes_state_sessions import shape")
        merged = merged.replace(old_sessions_import, canonical_sessions_import, 1)

    path.write_text(merged, encoding="utf-8")

    checked = path.read_text(encoding="utf-8")
    required = (
        "stat_db_file_identity as _stat_db_file_identity,",
        "_MODEL_CONFIG_ROW_MISSING,",
        "_collect_delegate_child_ids,",
        "_cwd_prefix_clause,",
        "_delete_delegate_children,",
        "_delegate_from_json,",
        "_workspace_key_clause,",
        "classify_session_status,",
        "workspace_key,",
        "_LISTABLE_CHILD_SQL,",
        "_RECOVERABLE_END_REASONS_SQL,",
        "_RESET_END_REASONS_SQL,",
        "_sql_session_last_active,",
        "_sql_session_last_active_by_id,",
    )
    missing = [owner for owner in required if owner not in checked]
    if missing:
        raise SystemExit(f"SessionDB support owner imports missing after reconcile: {missing}")


if __name__ == "__main__":
    main()
