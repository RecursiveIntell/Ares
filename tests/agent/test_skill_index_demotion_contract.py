"""Skill-index demotion: the withdrawal is locked, and demotion never hides.

Index demotion depends on "names-only" being the *permitted* ceiling. The
withdrawn pruning revision in ``agent/coding_context.py`` (see
``compact_skill_categories`` docstring) caused silent capability loss by
removing categories from the index entirely, so "demoted, never hidden" is a
load-bearing invariant, not a nicety.

Two negative witnesses:

  1. Category demotion stays OFF under ``auto``/``on`` — the default posture
     must never silently reshape the index (the "too surprising in practice"
     withdrawal). Fails if anyone re-enables default demotion.
  2. Even under ``focus``, every skill NAME survives in the rendered index.
     Fails if anyone reintroduces pruning.

The second test drives the real renderer (``build_skills_system_prompt``),
not a mock, so it exercises the exact code path that ships.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent import coding_context as cc


def _git_init(path: Path) -> None:
    """Make *path* a real code workspace so the coding posture applies."""
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(path),
    }
    (path / "main.py").write_text("print('hi')\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init commit"],
    ):
        subprocess.run(
            [shutil.which("git"), "-C", str(path), *args], check=True, env=env
        )


def _write_skill(skills_dir: Path, category: str, name: str, desc: str) -> None:
    skill = skills_dir / category / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_auto_and_on_never_demote_categories(tmp_path):
    """Negative witness: the default posture leaves the skill index untouched.

    If this fails, the withdrawn auto-demotion behavior has been reintroduced.
    ``agent/coding_context.py`` documents the withdrawal: names-only demotion
    under ``auto`` "proved too surprising in practice, even names-only ones" —
    a dropped description is information the model no longer weighs.
    """
    _git_init(tmp_path)
    for raw in ("auto", "on"):
        mode = cc.resolve_runtime_mode(
            platform="cli", cwd=tmp_path, config={"agent": {"coding_context": raw}}
        )
        assert mode.compact_skill_categories() == frozenset(), (
            f"demotion must stay off under {raw!r} — see the withdrawal noted in "
            "compact_skill_categories()"
        )


def test_demoted_categories_never_hide_skill_names(tmp_path):
    """Negative witness: demotion drops descriptions, never names.

    Builds a real index with two categories, demotes one, and asserts every
    skill name is still present — the invariant the withdrawn pruning
    revision broke.
    """
    from agent.prompt_builder import build_skills_system_prompt

    skills_dir = tmp_path / "skills"
    # One demoted category, one kept — a realistic focus-mode split.
    _write_skill(skills_dir, "smart-home", "hue-scenes", "Control Hue scenes.")
    _write_skill(skills_dir, "github", "pr-flow", "Open a tested PR.")

    prompt = build_skills_system_prompt(
        compact_categories=frozenset({"smart-home"}),
        skills_dir_override=skills_dir,
    )

    # Both names survive regardless of demotion.
    assert "hue-scenes" in prompt, "demoted skill name was hidden — pruning regression"
    assert "pr-flow" in prompt, "kept skill name missing from the index"

    # The demoted category's description is dropped (the point of demotion)...
    assert "Control Hue scenes." not in prompt, "demoted description was not dropped"
    # ...while the kept category's description remains.
    assert "Open a tested PR." in prompt, "kept category lost its description"

    # And the footer tells the model the demotion happened (no silent loss).
    assert "names only" in prompt.lower(), "demotion footer note missing"
