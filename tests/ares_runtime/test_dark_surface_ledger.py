"""Dark-surface ledger for the ``ares_runtime`` public export map.

An export that no product code consumes is *dark* surface. Dark surface
accumulates silently: a module can be complete, tested, and still unreachable.
This test enumerates it mechanically so it cannot grow unnoticed, and so the
question "what is wired up?" is answered by a check rather than by memory.

Three tiers, computed from source text (no import, so the ledger works on a
tree whose dependencies are absent):

    Tier A - consumed by product code: a ``*.py`` file outside ``tests/``
             and outside ``ares_runtime/``.
    Tier B - consumed by another ``ares_runtime`` module, excluding this
             package's ``__init__.py`` export map (which references every
             symbol by construction) and excluding the defining module (whose
             own internal use is not evidence of integration).
    Tier C - orphan: referenced by nothing but the export map, tests, and its
             own module. The export advertises an API the product never calls.

Tier C entries must appear in ``ORPHAN_LEDGER`` with the remedy that closes
them: ``wire`` (the owning module has no product caller at all) or
``unexport`` (the module is used internally, so the export map is simply
over-advertising an internal symbol).
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_PATH = REPO_ROOT / "ares_runtime" / "__init__.py"

# Tier C, classified: symbol -> (remedy, task, rationale).
ORPHAN_LEDGER: dict[str, tuple[str, str, str]] = {
    # --- remedy "wire": the owning module has no product caller at all ---
    "AresReleaseActivator": ("wire", "4.3", "activation owner reconciliation"),
    "AresRuntimeResolver": ("wire", "4.3", "stable runtime resolution owner"),
    "GovernedContextMaterializer": ("wire", "2.2", "pre-egress materialization"),
    "MANAGED_MODEL_CALL_KINDS": ("wire", "3.1", "managed-call kind registry"),
    "plan_legacy_memory_import": ("wire", "3.1", "legacy memory import planner"),
    "BlindWitness": ("wire", "2.2", "witness construction"),
    "ClosureProjector": ("wire", "2.2", "closure projection"),
    "ContractBindings": ("wire", "2.2", "contract binding set"),
    "replay_mutations": ("wire", "2.2", "replay mutation harness"),
    "closure_ui_projection": ("wire", "2.2", "closure UI projection"),
    "evaluation_ui_projection": ("wire", "2.2", "evaluation UI projection"),
    "DesktopProductionApprovalWitnessProvider": ("wire", "2.2", "approval witness"),
    # --- remedy "unexport": used inside its own module, over-advertised ---
    "ACTIVATION_GRANT_SCHEMA": ("unexport", "1.3", "internal schema constant"),
    "ActivationResult": ("unexport", "4.3", "internal activation record"),
    "ActivationState": ("unexport", "4.3", "internal activation state enum"),
    "BaselineResultV1": ("unexport", "1.3", "internal contract artifact"),
    "DesktopProductionApprovalController": ("unexport", "1.3", "internal controller"),
    "DesktopProductionApprovalEnvelope": ("unexport", "1.3", "internal envelope"),
    "EvidenceItemV1": ("unexport", "1.3", "internal contract artifact"),
    "FindingV1": ("unexport", "1.3", "internal contract artifact"),
    "FrozenReplayCorpusV1": ("unexport", "1.3", "internal contract artifact"),
    "HandoffPacketV1": ("unexport", "1.3", "internal contract artifact"),
    "INSTALLED_RUNTIME_POINTER_SCHEMA": ("unexport", "1.3", "internal schema constant"),
    "ManagedCallKind": ("unexport", "2.2", "internal enum used by the materializer"),
    "ManagedMaterialization": ("unexport", "2.2", "internal result envelope"),
    "MaterializationReceiptStore": ("unexport", "2.3", "internal receipt store"),
    "MaterializedContextV1": ("unexport", "2.2", "internal artifact"),
    "MemoryResolutionState": ("unexport", "2.2", "internal state enum"),
    "MissionContractV1": ("unexport", "1.3", "internal contract artifact"),
    "PermitBridgeOutcome": ("unexport", "1.3", "internal permit enum"),
    "PermitBridgeState": ("unexport", "1.3", "internal permit enum"),
    "PersistedManagedMaterialization": ("unexport", "2.3", "internal persisted record"),
    "RELEASE_MANIFEST_SCHEMA": ("unexport", "1.3", "internal schema constant"),
    "RUNTIME_IDENTITY_SCHEMA": ("unexport", "1.3", "internal schema constant"),
    "ResolvedPolicyBasisV2": ("unexport", "2.1", "consumed through its V1 base type"),
    "ResolvedRuntime": ("unexport", "4.3", "internal resolution record"),
    "RoleContractV1": ("unexport", "1.3", "internal contract artifact"),
    "RuntimeImage": ("unexport", "1.3", "internal image record"),
    "SemanticMemoryObservationV1": ("unexport", "2.2", "internal observation"),
    "SemanticMemoryWitnessedPort": ("unexport", "2.2", "internal port protocol"),
    "TestRequestV1": ("unexport", "1.3", "internal contract artifact"),
    "freeze_replay_corpus": ("unexport", "1.3", "internal helper"),
    "make_artifact": ("unexport", "1.3", "internal artifact factory"),
    "materialize_candidate_release": ("unexport", "4.3", "internal materializer entry"),
}

_USABLE_REMEDIES = {"wire", "unexport"}

# Directories that are never source we care about, pruned during the walk.
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

_FILE_TEXTS: dict[str, str] | None = None
_INDEX_CACHE: dict[str, dict[str, set[str]]] | None = None
_CLASSIFY_CACHE: dict[str, list[str]] | None = None


def _exported_symbols() -> dict[str, str]:
    """Parse ``_EXPORT_MODULES`` without importing the package."""
    tree = ast.parse(INIT_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "_EXPORT_MODULES" for t in node.targets
        ):
            continue
        mapping = ast.literal_eval(node.value)
        if not isinstance(mapping, dict) or not mapping:
            raise AssertionError("_EXPORT_MODULES parsed to an empty mapping")
        return {str(k): str(v) for k, v in mapping.items()}
    raise AssertionError("_EXPORT_MODULES not found in ares_runtime/__init__.py")


def _iter_source_files(include_internal: bool) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO_ROOT):
        rel_root = Path(root).relative_to(REPO_ROOT)
        if rel_root.parts and rel_root.parts[0] == "tests":
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        in_runtime = bool(rel_root.parts) and rel_root.parts[0] == "ares_runtime"
        if in_runtime and not include_internal:
            dirs[:] = []
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = Path(root) / name
            rel = path.relative_to(REPO_ROOT)
            is_internal = rel.parts[0] == "ares_runtime"
            if is_internal and not include_internal:
                continue
            if is_internal and rel.as_posix() == "ares_runtime/__init__.py":
                continue  # the export map references every symbol by construction
            files.append(path)
    return files


def _source_texts() -> dict[str, str]:
    global _FILE_TEXTS
    if _FILE_TEXTS is None:
        texts: dict[str, str] = {}
        for path in {*_iter_source_files(True), *_iter_source_files(False)}:
            try:
                texts[str(path.relative_to(REPO_ROOT))] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
        _FILE_TEXTS = texts
    return _FILE_TEXTS


def _symbol_index() -> dict[str, dict[str, set[str]]]:
    """One combined regex pass per file, not one pass per symbol per file."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    exports = _exported_symbols()
    names = sorted(exports, key=len, reverse=True)
    combined = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")
    product: dict[str, set[str]] = {}
    internal: dict[str, set[str]] = {}
    for rel, text in _source_texts().items():
        found = set(combined.findall(text))
        if not found:
            continue
        bucket = internal if rel.startswith("ares_runtime/") else product
        for symbol in found:
            bucket.setdefault(symbol, set()).add(rel)
    _INDEX_CACHE = {"product": product, "internal": internal}
    return _INDEX_CACHE


def _classify() -> dict[str, list[str]]:
    global _CLASSIFY_CACHE
    if _CLASSIFY_CACHE is not None:
        return _CLASSIFY_CACHE
    exports = _exported_symbols()
    index = _symbol_index()
    tiers: dict[str, list[str]] = {"A": [], "B": [], "C": []}
    for symbol, module in exports.items():
        defining = f"ares_runtime/{module.lstrip('.')}.py"
        if index["product"].get(symbol):
            tiers["A"].append(symbol)
        elif index["internal"].get(symbol, set()) - {defining}:
            tiers["B"].append(symbol)
        else:
            tiers["C"].append(symbol)
    _CLASSIFY_CACHE = tiers
    return tiers


def test_no_unexplained_dark_exports() -> None:
    """Tier C must be exactly the classified ledger."""
    tiers = _classify()
    unexplained = sorted(set(tiers["C"]) - set(ORPHAN_LEDGER))
    assert not unexplained, (
        "exports consumed by nothing outside the export map / tests / own module:\n  "
        + "\n  ".join(unexplained)
        + "\n\nWire them, un-export them, or add a ledger entry naming the task."
    )


def test_ledger_has_no_stale_entries() -> None:
    """A symbol that is consumed (Tier A/B) must leave the ledger."""
    tiers = _classify()
    consumed = set(tiers["A"]) | set(tiers["B"])
    stale = sorted(set(ORPHAN_LEDGER) & consumed)
    assert not stale, f"ledger names symbols that are now consumed: {stale}"

    exports = _exported_symbols()
    unknown = sorted(set(ORPHAN_LEDGER) - set(exports))
    assert not unknown, f"ledger names symbols that are no longer exported: {unknown}"


def test_ledger_entries_are_classified() -> None:
    for symbol, entry in ORPHAN_LEDGER.items():
        assert isinstance(entry, tuple) and len(entry) == 3, symbol
        remedy, task, rationale = entry
        assert remedy in _USABLE_REMEDIES, f"{symbol}: bad remedy {remedy!r}"
        assert re.fullmatch(r"\d+\.\d+", task), f"{symbol}: bad task id {task!r}"
        assert rationale and len(rationale) > 8, f"{symbol}: rationale too thin"


def test_scan_still_resolves_its_own_anchors() -> None:
    """Guard against the scan silently degrading to 'everything is dark'."""
    tiers = _classify()
    for symbol in ("dispatcher_boundary", "ContractError", "DaemonPermitReceiptAdapter"):
        assert symbol in tiers["A"], f"{symbol} should be Tier A (product-integrated)"
    assert len(tiers["A"]) >= 5, "Tier A collapsed - the scanner, not the product, broke"


def test_tier_counts_are_reported() -> None:
    """Informational: makes the surface size visible in the test log."""
    tiers = _classify()
    print(
        f"\nares_runtime exports: A(product)={len(tiers['A'])} "
        f"B(package-internal)={len(tiers['B'])} C(orphan)={len(tiers['C'])}"
    )
    assert len(tiers["C"]) == len(ORPHAN_LEDGER)
