#!/usr/bin/env python3
"""Deterministic syntactic provider-call census, not whole-program proof.

Identity is file + qualified lexical function + provider operation suffix;
occurrence counts/lines witness additions inside that identity. Fixture entries
must specify the exact count: unknown siblings and duplicates fail the CLI gate.
"""

from __future__ import annotations
import argparse
import ast
from collections import Counter
import json
from pathlib import Path

PATTERNS = (
    "chat.completions.create",
    "responses.create",
    "messages.create",
    "messages.stream",
    "completions.create",
    "models.generate_content",
    "models.generate_content_stream",
    "_native.call_structured",
    "_native.call",
    "converse",
    "converse_stream",
    "invoke_model",
    "invoke_model_with_response_stream",
    "generate_content",
    "generate_content_stream",
    "generateContent",
    "streamGenerateContent",
    "OpenAI",
    "AsyncOpenAI",
    "Anthropic",
    "AsyncAnthropic",
    "GeminiNativeClient",
    "AsyncGeminiNativeClient",
    "boto3.client",
    "post",
    "stream",
    "send",
    "request",
    "urlopen",
    "legacy_thread_bridge",
    "unsupported_transport_scope",
)
CLASSIFICATIONS = {
    "main_physical",
    "auxiliary_physical",
    "adapter_physical",
    "unsupported_enforce",
    "not_inference",
    "test_fixture",
}


def census_source(file, source):
    tree = ast.parse(source, filename=file)
    found = {}
    aliases = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in {"openai", "anthropic"}
        for alias in node.names
    }

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def scoped(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = scoped
        visit_AsyncFunctionDef = scoped
        visit_ClassDef = scoped

        def visit_Call(self, node):
            parts = []
            value = node.func
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
            if parts:
                parts[-1] = aliases.get(parts[-1], parts[-1])
            dotted = ".".join(reversed(parts))
            pattern = next(
                (p for p in PATTERNS if dotted == p or dotted.endswith("." + p)), None
            )
            if pattern:
                key = (file, ".".join(self.scope) or "<module>", pattern)
                found.setdefault(key, []).append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return [
        dict(
            file=f, function=fn, pattern=p, occurrences=len(lines), lines=sorted(lines)
        )
        for (f, fn, p), lines in sorted(found.items())
    ]


def qualify_rows(rows, fixture):
    mapping = {}
    for entry in fixture:
        key = tuple(entry[k] for k in ("file", "function", "pattern"))
        if (
            key in mapping
            or entry["classification"] not in CLASSIFICATIONS
            or not entry.get("evidence")
        ):
            raise ValueError("invalid or duplicate census fixture entry: " + str(key))
        mapping[key] = entry
    result = []
    for row in rows:
        entry = mapping.get(tuple(row[k] for k in ("file", "function", "pattern")))
        result.append(
            dict(
                row,
                classification=entry["classification"] if entry else "unrouted",
                evidence=entry["evidence"] if entry else None,
                expected_occurrences=entry.get("occurrences") if entry else None,
            )
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = sorted({
        *root.glob("*.py"),
        *(
            p
            for name in ("agent", "ares_runtime", "hermes_cli", "tools", "gateway")
            for p in (root / name).rglob("*.py")
        ),
    })
    rows = []
    for path in files:
        rows.extend(census_source(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
    fixture = json.loads(args.fixture.read_text(encoding="utf-8")) if args.fixture else []
    qualified = qualify_rows(rows, fixture)
    keys = {tuple(r[k] for k in ("file", "function", "pattern")) for r in rows}
    stale = [
        entry
        for entry in fixture
        if tuple(entry[k] for k in ("file", "function", "pattern")) not in keys
    ]
    invalid = [
        r
        for r in qualified
        if r["classification"] == "unrouted"
        or r["expected_occurrences"] != r["occurrences"]
    ]
    document = dict(
        schema="ares.provider-call-census/v1",
        root=str(root),
        files_scanned=len(files),
        limitations="Static attribute-call suffixes only; aliases, dynamic getattr and opaque native internals require independent review.",
        rows=qualified,
        stale_fixture=stale,
        unqualified=len(invalid),
        classification_counts=dict(
            sorted(Counter(r["classification"] for r in qualified).items())
        ),
    )
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 1 if args.check and (invalid or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
