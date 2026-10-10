"""Skill-routing A/B runner.

Measures whether the task-fit routing prose changes an agent's *skill-loading
behavior*, through a minimal agent loop in which the ONLY variable between
arms is the ``<available_skills>`` block produced by
``agent/prompt_builder.py`` extracted from two git refs. Everything else —
model, temperature, task prompts, the skill catalog, the ``skill_view`` tool
behavior — is held constant.

The block under test is the routing prose + index: exactly what a real
session puts on the wire.

Usage:
  python3 evals/skill_routing/runner.py \
      --base e3e8a39d9427 --cand be3df4e52121 \
      --model deepseek/deepseek-v4.1-flash --reps 3

  # local, keyless (Ollama):
  python3 evals/skill_routing/runner.py --base <ref> --cand <ref> \
      --base-url http://127.0.0.1:11434/v1 --model granite4.2:3b --reps 3

  # limit to one task
  ... --tasks t4_abstain_math

Results append to results/<label>/<model-slug>.jsonl (resume-safe). Summarize
with report.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(REPO_ROOT))

from fixtures import build_catalog, skill_body  # noqa: E402
from tasks import SYSTEM_PREAMBLE, TASKS, TASKS_BY_ID  # noqa: E402

# The bridge tool the model uses to load a skill. Its schema is byte-identical
# across arms; only the system-prompt skills block varies.
SKILL_VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": (
            "Load the full instructions of a named skill. Call this when a "
            "skill in the library applies to the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill name to load."}
            },
            "required": ["name"],
        },
    },
}


def _load_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / ".ares" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def extract_arm(ref: str, workdir: Path, name: str) -> Path:
    """Extract agent/prompt_builder.py from a git ref."""
    out = subprocess.run(
        ["git", "show", f"{ref}:agent/prompt_builder.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"git show {ref}:agent/prompt_builder.py: {out.stderr.strip()}")
    path = workdir / f"pb_arm_{name}.py"
    path.write_text(out.stdout)
    return path


def load_arm(path: Path, name: str):
    """Import an extracted prompt_builder as its own module."""
    spec = importlib.util.spec_from_file_location(f"pb_arm_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pb_arm_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def render_skills_block(arm_mod, skills_dir: Path,
                        compact_categories: frozenset | None = None) -> str:
    """The arm's <available_skills> block for the shared catalog."""
    return arm_mod.build_skills_system_prompt(
        skills_dir_override=skills_dir,
        compact_categories=compact_categories,
    )


def run_one(client, model, arm_name, skills_block, task_id, prompt, oracle,
            max_iters: int = 6):
    system = f"{SYSTEM_PREAMBLE}\n\n{skills_block}"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]
    loaded: list[str] = []
    total_tokens = 0
    first_prompt_tokens = None
    final = ""
    t0 = time.time()
    for _ in range(max_iters):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=[SKILL_VIEW_TOOL],
            temperature=0.2, max_tokens=4096,
        )
        u = getattr(resp, "usage", None)
        if u:
            if first_prompt_tokens is None:
                first_prompt_tokens = u.prompt_tokens
            total_tokens += (u.total_tokens or 0)
        msg = resp.choices[0].message
        tcs = msg.tool_calls or []
        if not tcs:
            final = msg.content or ""
            break
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in tcs
            ],
        })
        for tc in tcs:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            if tc.function.name == "skill_view":
                name = str(args.get("name", "")).strip()
                loaded.append(name)
                out = skill_body(name) or f"(no skill named {name!r})"
            else:
                out = f"(unknown tool {tc.function.name!r})"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return {
        "task": task_id, "arm": arm_name, "model": model,
        "score": float(oracle(loaded, final)),
        "loaded": loaded,
        "n_loads": len(loaded),
        "first_prompt_tokens": first_prompt_tokens,
        "total_tokens": total_tokens,
        "wall_s": round(time.time() - t0, 1),
        "final": final[:1500],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="git ref for the baseline arm")
    ap.add_argument("--cand", required=True, help="git ref for the candidate arm")
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", nargs="*", default=None)
    ap.add_argument("--label", default="ab")
    ap.add_argument(
        "--demote-extra-arm",
        action="store_true",
        help=(
            "Add a third 'demoted' arm: the candidate builder with every "
            "category rendered names-only. Tests whether stripping skill "
            "descriptions from the index changes routing behavior."
        ),
    )
    args = ap.parse_args()

    from openai import OpenAI

    api_key = _load_api_key() or "ollama"
    client = OpenAI(base_url=args.base_url, api_key=api_key)

    with tempfile.TemporaryDirectory(prefix="skill_routing_") as td:
        tdir = Path(td)
        # Byte-identical catalog per arm (hermetic: no shared snapshot).
        base_skills = build_catalog(tdir / "base")
        cand_skills = build_catalog(tdir / "cand")

        arms = {
            "base": (load_arm(extract_arm(args.base, tdir, "base"), "base"), base_skills),
            "cand": (load_arm(extract_arm(args.cand, tdir, "cand"), "cand"), cand_skills),
        }
        # Render each arm's block once — it is constant across tasks/reps.
        blocks = {name: render_skills_block(mod, sd) for name, (mod, sd) in arms.items()}

        # Optional third arm: the candidate builder with EVERY category demoted
        # to names-only (the "does stripping descriptions hurt routing?" test).
        arm_names = ["base", "cand"]
        if args.demote_extra_arm:
            from fixtures import CATALOG
            all_cats = frozenset(cat for cat, _n, _d, _m in CATALOG)
            demote_skills = build_catalog(tdir / "demote")
            blocks["demoted"] = render_skills_block(
                arms["cand"][0], demote_skills, compact_categories=all_cats
            )
            arm_names.append("demoted")

        outdir = EVAL_DIR / "results" / args.label
        outdir.mkdir(parents=True, exist_ok=True)
        outpath = outdir / (re.sub(r"[^\w.-]", "_", args.model) + ".jsonl")
        done = set()
        if outpath.exists():
            for line in outpath.read_text().splitlines():
                try:
                    r = json.loads(line)
                    done.add((r["task"], r["arm"], r["rep"]))
                except Exception:
                    pass

        with open(outpath, "a", encoding="utf-8") as f:
            for task_id, task in TASKS_BY_ID.items():
                if args.tasks and task_id not in args.tasks:
                    continue
                for rep in range(args.reps):
                    for arm_name in arm_names:
                        if (task_id, arm_name, rep) in done:
                            continue
                        for attempt in range(3):
                            try:
                                r = run_one(client, args.model, arm_name,
                                            blocks[arm_name], task_id,
                                            task.prompt, task.oracle)
                                if (not r["final"].strip() and r["n_loads"] == 0
                                        and attempt < 2):
                                    print(f"NOISE-RETRY {task_id} {arm_name} rep{rep}")
                                    continue
                                r["rep"] = rep
                                r["family"] = task.family
                                r["base_ref"] = args.base
                                r["cand_ref"] = args.cand
                                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                                f.flush()
                                print(f"{task_id} {arm_name} rep{rep}: "
                                      f"score={r['score']} loads={r['n_loads']} "
                                      f"{r['loaded']}")
                                break
                            except Exception as e:  # noqa: BLE001
                                print(f"RETRY {task_id} {arm_name} rep{rep}: {e}")
                                traceback.print_exc()
                                time.sleep(3 * (attempt + 1))
        print("done ->", outpath)


if __name__ == "__main__":
    main()
