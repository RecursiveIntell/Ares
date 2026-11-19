"""Graph → profile-panel adapter (PANEL card, P3).

Translates ONE graph operation into ONE explicit profile-panel invocation
through the installed runner's argv contract. The runner and policy files are
read-only owners here: this adapter never rewrites a profile, never auto-runs
the full panel, and never substitutes a blocked route. Receipt verification
stays with the canonical ``verify_receipt.py``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_RUNNER = (
    Path.home()
    / ".ares/skills/productivity/profile-collaboration/scripts/run_panel.py"
)
DEFAULT_VERIFIER = (
    Path.home()
    / ".ares/skills/productivity/profile-collaboration/scripts/verify_receipt.py"
)
DEFAULT_POLICY_ROOT = Path.home() / ".ares" / "specialist-system"

_AUTHOR_FAMILIES = {
    "anthropic", "openai", "google", "xai", "deepseek",
    "zhipu", "qwen", "meta", "mistral",
}


class PanelAdmissionError(RuntimeError):
    """Typed refusal before any panel process is spawned."""


def _manifest_profiles(policy_root: Path) -> tuple[set[str], set[str]]:
    manifest_path = policy_root / "POLICY_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PanelAdmissionError(f"policy_manifest_unreadable:{type(exc).__name__}") from exc
    active = set(manifest.get("active_advisory_profiles", []))
    separate = set(manifest.get("separate_gate_profiles", []))
    retired = set(manifest.get("retired_profiles", []))
    return active | separate, retired


def build_panel_argv(
    *,
    runner: Path,
    profile: str,
    workspace: Path,
    brief: str,
    out_dir: Path,
    author_family: str,
    interpreter: Optional[str] = None,
    extra: Optional[list[str]] = None,
) -> list[str]:
    """Build the exact runner argv for ONE explicit profile. No full panel."""
    if profile in {"*", "", "all"} or "," in profile:
        raise PanelAdmissionError("full_panel_not_permitted")
    if author_family not in _AUTHOR_FAMILIES:
        raise PanelAdmissionError("author_family_unqualified")
    if not brief or not brief.strip():
        raise PanelAdmissionError("brief_required")
    argv = [
        interpreter or sys.executable,
        str(runner),
        "--profiles", profile,
        "--workspace", str(workspace),
        "--brief", brief,
        "--out", str(out_dir),
        "--author-family", author_family,
    ]
    if extra:
        argv.extend(extra)
    return argv


class GraphPanelAdapter:
    """One graph operation → one admitted panel run → canonical verification."""

    def __init__(
        self,
        runner: Path = DEFAULT_RUNNER,
        verifier: Path = DEFAULT_VERIFIER,
        policy_root: Path = DEFAULT_POLICY_ROOT,
        timeout_seconds: int = 900,
    ) -> None:
        self.runner = Path(runner)
        self.verifier = Path(verifier)
        self.policy_root = Path(policy_root)
        self.timeout_seconds = timeout_seconds
        if not self.runner.is_file():
            raise PanelAdmissionError("runner_unavailable")

    def _policy_descriptor_digest(self) -> str:
        manifest_path = self.policy_root / "POLICY_MANIFEST.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = manifest["descriptor_manifest"]
            descriptor_path = self.policy_root / entry["path"]
            actual = hashlib.sha256(descriptor_path.read_bytes()).hexdigest()
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise PanelAdmissionError(f"descriptor_unreadable:{type(exc).__name__}") from exc
        declared = str(entry["sha256"]).removeprefix("sha256:")
        if actual != declared:
            raise PanelAdmissionError("descriptor_digest_mismatch")
        return "sha256:" + actual

    def admit(
        self,
        *,
        profile: str,
        workspace: Path,
        brief: str,
        out_dir: Path,
        author_family: str,
    ) -> dict[str, Any]:
        """Validate policy + profile BEFORE any spawn. Returns admission info."""
        allowed, retired = _manifest_profiles(self.policy_root)
        if profile in retired:
            raise PanelAdmissionError("profile_retired")
        if profile not in allowed:
            raise PanelAdmissionError("profile_not_in_live_manifest")
        digest = self._policy_descriptor_digest()
        argv = build_panel_argv(
            runner=self.runner, profile=profile, workspace=workspace,
            brief=brief, out_dir=out_dir, author_family=author_family,
        )
        return {"profile": profile, "descriptor_digest": digest, "argv": argv}

    def run(
        self,
        *,
        profile: str,
        workspace: Path,
        brief: str,
        out_dir: Path,
        author_family: str,
        extra: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        admission = self.admit(
            profile=profile, workspace=workspace, brief=brief,
            out_dir=out_dir, author_family=author_family,
        )
        argv = admission["argv"]
        if extra:
            argv = argv + list(extra)
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout_seconds,
        )
        verify_argv = [sys.executable, str(self.verifier), "--receipt", str(out_dir)]
        if completed.returncode != 0:
            # Typed blocked outcome. No alternate profile, no generic persona,
            # no legacy substitution — the caller reconciles or reports.
            return {
                "status": "blocked",
                "profile": profile,
                "exit_code": completed.returncode,
                "stderr_tail": completed.stderr[-2000:],
                "substituted": False,
                "receipt_dir": str(out_dir),
                "verify_argv": verify_argv,
            }
        return {
            "status": "completed",
            "profile": profile,
            "exit_code": 0,
            "descriptor_digest": admission["descriptor_digest"],
            "receipt_dir": str(out_dir),
            "verify_argv": verify_argv,
            "substituted": False,
        }
