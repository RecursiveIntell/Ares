"""Directory layout for CandidateStore-authorized Ares release materialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import require_sha256
from .errors import AresRuntimeError


@dataclass(frozen=True)
class AresRuntimeLayout:
    """Paths owned by one installation-scoped Ares runtime root."""

    root: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise AresRuntimeError("INVALID_PATH", "ares root")

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    def release_dir(self, release_id: str) -> Path:
        if release_id.startswith("legacy-"):
            require_sha256(release_id.removeprefix("legacy-"), "legacy release id")
        else:
            require_sha256(release_id, "release id")
        return self.releases_dir / release_id
