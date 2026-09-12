#!/usr/bin/env python3
"""Cede stale monolithic SessionDB methods to their modular canonical owners.

PR35 retains the Hermes v0.21.1 SessionDB mixin split while current Ares main
still carries older monolithic copies of some of the same methods.  A normal
three-way merge therefore leaves duplicate direct methods on ``SessionDB``;
Python resolves those before inherited mixin methods, silently shadowing newer
canonical behavior.

This temporary reconciliation transform removes only methods whose duplicate
ownership is proven by focused regression tests.  It never copies an
implementation: after removal, normal MRO dispatch resolves to the existing
mixin owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

TARGETS = {
    "_insert_session_row": ("hermes_state_sessions.py", "SessionSessionsMixin"),
    "list_sessions_rich": ("hermes_state_sessions.py", "SessionSessionsMixin"),
    "apply_telegram_topic_migration": (
        "hermes_state_telegram.py",
        "SessionTelegramTopicsMixin",
    ),
    "find_latest_gateway_session_for_peer": (
        "hermes_state_gateway.py",
        "SessionGatewayMixin",
    ),
    "maybe_auto_prune_and_vacuum": (
        "hermes_state_maintenance.py",
        "SessionMaintenanceMixin",
    ),
    "get_resume_message_count": (
        "hermes_state_messages.py",
        "SessionMessagesMixin",
    ),
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


def verify_mixin_owner(path: str, class_name: str, method_name: str) -> None:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    cls = direct_class(tree, class_name)
    methods = direct_methods(cls)
    if method_name not in methods:
        raise SystemExit(f"canonical owner missing: {path}:{class_name}.{method_name}")


def main() -> None:
    path = Path("hermes_state.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    cls = direct_class(tree, "SessionDB")
    methods = direct_methods(cls)

    removals: list[tuple[int, int, str]] = []
    for method_name, (owner_path, owner_class) in TARGETS.items():
        verify_mixin_owner(owner_path, owner_class, method_name)
        node = methods.get(method_name)
        if node is None:
            raise SystemExit(
                f"expected stale direct SessionDB.{method_name} before ownership reconcile"
            )
        start = min(
            [node.lineno]
            + [decorator.lineno for decorator in node.decorator_list]
        )
        if node.end_lineno is None:
            raise SystemExit(f"AST missing end_lineno for SessionDB.{method_name}")
        removals.append((start, node.end_lineno, method_name))

    lines = source.splitlines(keepends=True)
    for start, end, method_name in sorted(removals, reverse=True):
        del lines[start - 1 : end]

    reconciled = "".join(lines)
    ast.parse(reconciled, filename=str(path))
    path.write_text(reconciled, encoding="utf-8")

    final_tree = ast.parse(reconciled, filename=str(path))
    final_cls = direct_class(final_tree, "SessionDB")
    final_methods = direct_methods(final_cls)
    shadowed = sorted(set(TARGETS) & set(final_methods))
    if shadowed:
        raise SystemExit(f"stale direct SessionDB owners survived: {shadowed}")


if __name__ == "__main__":
    main()
