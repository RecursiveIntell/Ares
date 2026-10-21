from __future__ import annotations

import ast
import re
from pathlib import Path


SKILL_ROOT = Path(__file__).parents[1]


def _runner_profiles() -> tuple[str, ...]:
    tree = ast.parse((SKILL_ROOT / "scripts" / "run_panel.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROFILES" for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("run_panel.py must declare its explicit selectable profile set")


def _documented_obligations() -> dict[str, str]:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    marker = "Obligation classes map to profiles:"
    end_marker = "The router preserves one-executor default behavior"
    assert marker in text and end_marker in text
    section = text.split(marker, 1)[1].split(end_marker, 1)[0]
    rows = re.findall(r"- `([^`]+)` → `([^`]+)`", section)
    mapping = dict(rows)
    assert len(mapping) == len(rows), "duplicate obligation class in routing table"
    return mapping


def test_every_documented_obligation_targets_a_selectable_profile() -> None:
    mapping = _documented_obligations()
    selectable = set(_runner_profiles())
    assert mapping
    assert set(mapping.values()) <= selectable
    assert "evidence-synthesizer" not in mapping.values()


def test_career_search_is_gated_to_job_scout() -> None:
    mapping = _documented_obligations()
    assert mapping.get("career_search") == "job-scout"
    text = " ".join((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").split())
    assert "only when the operator requests current" in text
    assert "Fictional career fixtures do not activate a real search" in text


def test_measurement_and_quantitative_owners_remain_distinct() -> None:
    mapping = _documented_obligations()
    assert mapping.get("quantitative_inference") == "statistician"
    assert mapping.get("measurement_validity") == "psychometrician"
    assert mapping["quantitative_inference"] != mapping["measurement_validity"]


def test_routine_no_gap_does_not_appear_as_a_specialist_obligation() -> None:
    mapping = _documented_obligations()
    assert not any(term in mapping for term in ("routine", "simple_lookup", "no_material_gap"))
