#!/usr/bin/env python3
"""Cede stale monolithic SessionDB methods to their modular canonical owners.

PR35 retains the Hermes v0.21.1 SessionDB mixin split while current Ares main
still carries older monolithic copies of some of the same methods. A normal
three-way merge therefore leaves duplicate direct methods on ``SessionDB``;
Python resolves those before inherited mixin methods, silently shadowing newer
canonical behavior.

This temporary reconciliation transform removes duplicate owners only when the
modular owner already exists. It never copies an implementation: after removal,
normal MRO dispatch resolves to the existing mixin owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Explicit single-method ownership decisions already proven by focused failures.
EXPLICIT_TARGETS = {
    "_insert_session_row": ("hermes_state_sessions.py", "SessionSessionsMixin"),
    "list_sessions_rich": ("hermes_state_sessions.py", "SessionSessionsMixin"),
    "find_latest_gateway_session_for_peer": (
        "hermes_state_gateway.py",
        "SessionGatewayMixin",
    ),
    "get_resume_message_count": (
        "hermes_state_messages.py",
        "SessionMessagesMixin",
    ),
    "assert_resume_safe": (
        "hermes_state_messages.py",
        "SessionMessagesMixin",
    ),
}

# These are coherent modular ownership boundaries. Current-main monolithic
# methods from the same cluster are stale shadows, so cede the complete direct
# intersection instead of whack-a-mole signature patches.
CLUSTER_OWNERS = {
    ("hermes_state_telegram.py", "SessionTelegramTopicsMixin"): {
        "apply_telegram_topic_migration",
        "bind_telegram_topic",
        "get_telegram_topic_binding",
    },
    ("hermes_state_maintenance.py", "SessionMaintenanceMixin"): {
        "maybe_auto_prune_and_vacuum",
        "sweep_orphaned_sessions",
        "_prune_filter_where",
    },
}


def direct_class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one class {name!r}, found {len(matches)}")
    return matches[0]


def direct_methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in out:
                raise SystemExit(f"duplicate direct method {cls.name}.{node.name}")
            out[node.name] = node
    return out


def owner_methods(path: str, class_name: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    return direct_methods(direct_class(tree, class_name))


def main() -> None:
    path = Path("hermes_state.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    cls = direct_class(tree, "SessionDB")
    methods = direct_methods(cls)

    targets: dict[str, tuple[str, str]] = dict(EXPLICIT_TARGETS)

    # Cede every duplicate within the two proven coherent modular clusters.
    # Require the failure-driving names so an unexpected source shape fails
    # closed rather than silently reconciling the wrong ownership boundary.
    for (owner_path, owner_class), required in CLUSTER_OWNERS.items():
        canonical = owner_methods(owner_path, owner_class)
        missing_required = sorted(required - set(canonical))
        if missing_required:
            raise SystemExit(
                f"canonical {owner_class} missing required owners: {missing_required}"
            )
        intersection = sorted(set(methods) & set(canonical))
        missing_shadow = sorted(required - set(intersection))
        if missing_shadow:
            raise SystemExit(
                f"expected stale SessionDB shadows missing for {owner_class}: {missing_shadow}"
            )
        if not intersection:
            raise SystemExit(f"no duplicate owners found for {owner_class}")
        print(f"ceding {owner_class} direct shadows: {', '.join(intersection)}")
        for method_name in intersection:
            targets[method_name] = (owner_path, owner_class)

    removals: list[tuple[int, int, str]] = []
    for method_name, (owner_path, owner_class) in sorted(targets.items()):
        canonical = owner_methods(owner_path, owner_class)
        if method_name not in canonical:
            raise SystemExit(
                f"canonical owner missing: {owner_path}:{owner_class}.{method_name}"
            )
        node = methods.get(method_name)
        if node is None:
            raise SystemExit(
                f"expected stale direct SessionDB.{method_name} before ownership reconcile"
            )
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        if node.end_lineno is None:
            raise SystemExit(f"AST missing end_lineno for SessionDB.{method_name}")
        removals.append((start, node.end_lineno, method_name))

    lines = source.splitlines(keepends=True)
    for start, end, _method_name in sorted(removals, reverse=True):
        del lines[start - 1 : end]

    reconciled = "".join(lines)
    ast.parse(reconciled, filename=str(path))
    path.write_text(reconciled, encoding="utf-8")

    final_methods = direct_methods(direct_class(ast.parse(reconciled), "SessionDB"))
    shadowed = sorted(set(targets) & set(final_methods))
    if shadowed:
        raise SystemExit(f"stale direct SessionDB owners survived: {shadowed}")


if __name__ == "__main__":
    main()
