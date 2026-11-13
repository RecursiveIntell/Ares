"""No declared or documented control surface without a consumer.

A control surface can lie in two ways:

1. The README documents an environment flag that no module reads, so an
   operator who sets it changes nothing.
2. A module declares feature flags nothing imports, so the flags appear to gate
   behaviour that is in fact always on.

Both are false control surfaces: the setting exists, the effect does not. This
test makes each mechanically visible instead of relying on someone remembering.

Scope: Python modules outside ``tests/``. Launcher shell scripts and the
installer legitimately read some of these names too, but a flag that only a
shell script reads is still a declared surface and is counted here as consumed
only if some Python module also honours it -- the measurement is intentionally
strict so an unread flag cannot hide behind shell usage.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
FEATURE_FLAGS = REPO_ROOT / "ares_runtime" / "feature_flags.py"

_FLAG_RE = re.compile(r"\bARES_[A-Z0-9_]+\b")

_PRUNE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
}

_SOURCE_CACHE: dict[str, str] | None = None


def _non_test_sources() -> dict[str, str]:
    global _SOURCE_CACHE
    if _SOURCE_CACHE is None:
        texts: dict[str, str] = {}
        for root, dirs, names in os.walk(REPO_ROOT):
            rel_root = Path(root).relative_to(REPO_ROOT)
            if rel_root.parts and rel_root.parts[0] == "tests":
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = Path(root) / name
                try:
                    texts[str(path.relative_to(REPO_ROOT))] = path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
        _SOURCE_CACHE = texts
    return _SOURCE_CACHE


def _flags_referenced_by(sources: dict[str, str], *, exclude: str | None = None) -> set[str]:
    found: set[str] = set()
    for rel, text in sources.items():
        if exclude is not None and rel == exclude:
            continue
        found.update(_FLAG_RE.findall(text))
    return found


def test_readme_documents_only_flags_some_module_reads() -> None:
    """A flag an operator can set but nothing honours is a lie."""
    documented = set(_FLAG_RE.findall(README.read_text(encoding="utf-8")))
    assert documented, "README mentions no ARES_* flags -- parser or doc regressed"

    honoured = _flags_referenced_by(
        _non_test_sources(), exclude="ares_runtime/feature_flags.py"
    )
    unread = sorted(documented - honoured)
    assert not unread, (
        "README documents ARES_* flags that no non-test module reads:\n  "
        + "\n  ".join(unread)
        + "\n\nEither wire the flag to a consumer or correct the documentation."
    )


def test_feature_flag_module_declares_only_consumed_flags() -> None:
    """A declared flag nothing reads is a switch with no circuit behind it.

    Deleting the module satisfies this test; so does wiring every flag. What is
    forbidden is keeping declarations that gate nothing.
    """
    if not FEATURE_FLAGS.exists():
        return

    declared = set(_FLAG_RE.findall(FEATURE_FLAGS.read_text(encoding="utf-8")))
    assert declared, "feature_flags.py declares no flags -- parser regressed"

    honoured = _flags_referenced_by(
        _non_test_sources(), exclude="ares_runtime/feature_flags.py"
    )
    dead = sorted(declared - honoured)
    assert not dead, (
        "declared but never consumed feature flags:\n  "
        + "\n  ".join(dead)
        + "\n\nWire them or delete the module; a declared flag that gates "
        "nothing misrepresents the system."
    )
