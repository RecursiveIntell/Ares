"""Deterministic skill catalog for the skill-routing eval.

Builds a small, fixed skills tree whose category/description shapes are chosen
so a task can be *directly* relevant to one skill while being merely keyword-
adjacent to a decoy. Both eval arms receive a byte-identical copy of this
catalog, so the only variable between arms is the routing prose under test.

Deterministic: same bytes every call.
"""

from __future__ import annotations

from pathlib import Path

# (category, skill_name, description, body_marker)
CATALOG: list[tuple[str, str, str, str]] = [
    (
        "devops",
        "deploy-runbook",
        "Run a service through a gated production deploy.",
        "DEPLOY_STEPS",
    ),
    (
        "devops",
        "rollback-runbook",
        "Roll a bad deploy back to the last known-good version.",
        "ROLLBACK_STEPS",
    ),
    (
        "writing",
        "changelog-format",
        "Format a release changelog entry.",
        "CHANGELOG_SECTIONS",
    ),
    (
        "data",
        "csv-parser",
        "Parse a delimited file into typed rows.",
        "CSV_HEADER_ROW",
    ),
    (
        "github",
        "pr-flow",
        "Open a tested pull request against a protected branch.",
        "PR_CHECKLIST",
    ),
    (
        "smart-home",
        "hue-scenes",
        "Recall and apply Philips Hue lighting scenes.",
        "SCENE_TABLE",
    ),
]


def build_catalog(dest: str | Path) -> Path:
    """Write the catalog under *dest*/skills/ and return the skills dir."""
    skills_dir = Path(dest) / "skills"
    for category, name, desc, marker in CATALOG:
        skill = skills_dir / category / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {desc}\n"
            "---\n\n"
            f"# {name}\n\n"
            f"marker: {marker}\n",
            encoding="utf-8",
        )
    return skills_dir


def skill_body(name: str) -> str:
    """Return the body a skill_view(name) call would produce (or '')."""
    for _cat, sname, desc, marker in CATALOG:
        if sname == name:
            return f"# {sname}\n\ndescription: {desc}\n\nmarker: {marker}\n"
    return ""


def known_names() -> set[str]:
    return {name for _cat, name, _d, _m in CATALOG}


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/skill-routing-cat"
    print(build_catalog(target))
