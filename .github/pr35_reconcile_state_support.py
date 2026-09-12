#!/usr/bin/env python3
"""Restore Ares-owned SessionDB support owners after the pinned main merge.

Temporary PR35 reconciliation scaffolding.  The branch keeps the Hermes v0.21.1
modular SessionDB shape, while this transform restores the exact current-Ares
helper/import owners required by Ares call sites that survive that merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PINNED_MAIN = "0451a66cbb765a3ede359660e37b1b2fbe857525"


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

    # Re-export the exact Ares common owners.  PR35's modular Hermes file kept
    # only escape_like/stat identity, while merged Ares call sites reference
    # lineage, recovery, preview and last-active owners from hermes_state_common.
    common_start = "from hermes_state_common import ("
    merged_common_end = "from hermes_state_errors import (\n"
    main_common_end = "from hermes_state_portability import SessionPortabilityMixin\n"
    merged_common = between(merged, common_start, merged_common_end)
    canonical_common = between(ares_main, common_start, main_common_end)
    merged = merged.replace(merged_common, canonical_common, 1)

    # Restore the exact Ares helper prelude used by the surviving monolithic
    # SessionDB methods.  This is deliberately copied from current main rather
    # than reimplemented here: one source of truth for workspace matching,
    # model-config row absence, delegate cascade selection and cwd prefix SQL.
    branch_support_start = "# Billing buckets that aren't a routable provider identity:"
    support_end = 'T = TypeVar("T")\n'
    canonical_support_start = "def workspace_key(row: Dict[str, Any]) -> Optional[str]:\n"
    merged_support = between(merged, branch_support_start, support_end)
    canonical_support = between(ares_main, canonical_support_start, support_end)
    merged = merged.replace(merged_support, canonical_support, 1)

    path.write_text(merged, encoding="utf-8")

    checked = path.read_text(encoding="utf-8")
    required = (
        "_MODEL_CONFIG_ROW_MISSING = object()",
        "def _cwd_prefix_clause(cwd_prefix: str)",
        "def _workspace_key_clause(key: str)",
        "def _collect_delegate_child_ids(conn, parent_ids: List[str])",
        "def _delete_delegate_children(conn, parent_ids: List[str])",
        "_LISTABLE_CHILD_SQL,",
        "_RECOVERABLE_END_REASONS_SQL,",
        "_RESET_END_REASONS_SQL,",
        "_sql_session_last_active,",
        "_sql_session_last_active_by_id,",
    )
    missing = [owner for owner in required if owner not in checked]
    if missing:
        raise SystemExit(f"SessionDB support owners missing after reconcile: {missing}")


if __name__ == "__main__":
    main()
