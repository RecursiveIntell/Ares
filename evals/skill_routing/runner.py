"""Skill-routing A/B runner.

Measures whether the task-fit routing prose changes an agent's *skill-loading
behavior*, through a minimal agent loop in which the ONLY variable between
arms is the ``<available_skills>`` block produced by
``agent/prompt_builder.py`` extracted from two git refs. Everything else —
model, temperature, task prompts, the skill catalog, the ``skill_view`` tool
behavior — is held constant.

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
import urllib.parse
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(REPO_ROOT))

from fixtures import build_catalog, skill_body  # noqa: E402
from tasks import SYSTEM_PREAMBLE, TASKS_BY_ID  # noqa: E402

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

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _read_env_key(path: Path, name: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _load_api_key(env_name: str) -> str:
    """Resolve *env_name* from the process env, then the profile-aware Hermes
    home (ARES_HOME/HERMES_HOME, then ~/.ares and ~/.hermes)."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    for home in (os.environ.get("ARES_HOME"), os.environ.get("HERMES_HOME")):
        if home:
            v = _read_env_key(Path(home) / ".env", env_name)
            if v:
                return v
    for path in (Path.home() / ".ares" / ".env", Path.home() / ".hermes" / ".env"):
        v = _read_env_key(path, env_name)
        if v:
            return v
    return ""


def _is_local_endpoint(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def resolve_bearer(base_url: str, explicit_key: str, key_env: str) -> str:
    """Never send a real cloud credential to an arbitrary endpoint.

    A keyless local URL gets a dummy token. OpenRouter uses OPENROUTER_API_KEY.
    Any other host requires an endpoint-specific key via --api-key-env, so
    pointing --base-url at a third-party service cannot leak the OpenRouter key.
    """
    if explicit_key:
        return explicit_key
    if _is_local_endpoint(base_url):
        return "local"
    if "openrouter.ai" in base_url:
        key = _load_api_key("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit(
                "OPENROUTER_API_KEY not set (env or ~/.ares/.env) for the "
                "OpenRouter endpoint."
            )
        return key
    if key_env:
        key = _load_api_key(key_env)
        if not key:
            raise SystemExit(f"{key_env} not set (for endpoint {base_url!r}).")
        return key
    raise SystemExit(
        f"Refusing to send a shared credential to {base_url!r}. Pass "
        "--api-key-env <VAR> naming an endpoint-specific key, or --api-key."
    )


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


def _repair_jsonl_tail(path: Path) -> None:
    """Drop a truncated final line so appends don't concatenate onto it.

    An interrupted write can leave a partial JSON line without a trailing
    newline; appending the next record onto it would corrupt both. Truncate to
    the last complete line before opening for append.
    """
    if not path.exists():
        return
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    idx = data.rfind(b"\n")
    path.write_bytes(data[: idx + 1] if idx >= 0 else b"")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="git ref for the baseline arm")
    ap.add_argument("--cand", required=True, help="git ref for the candidate arm")
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    ap.add_argument("--api-key", default="", help="explicit bearer token")
    ap.add_argument(
        "--api-key-env",
        default="",
        help="env var holding an endpoint-specific key for a non-local, "
             "non-OpenRouter --base-url",
    )
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", nargs="+", default=None, metavar="TASK")
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

    # Validate the task limiter so a typo cannot silently run nothing (or an
    # empty --tasks cannot silently run the whole experiment).
    if args.tasks is not None:
        unknown = [t for t in args.tasks if t not in TASKS_BY_ID]
        if unknown:
            ap.error(
                f"unknown task id(s): {', '.join(unknown)}. "
                f"valid: {', '.join(sorted(TASKS_BY_ID))}"
            )

    from openai import OpenAI

    bearer = resolve_bearer(args.base_url, args.api_key, args.api_key_env)
    client = OpenAI(base_url=args.base_url, api_key=bearer)

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

        # Resume: only skip cells already recorded for THIS experiment identity
        # (same refs + model). Mixing refs would silently report an old run as
        # a new one, so refuse rather than merge.
        done = set()
        if outpath.exists():
            _repair_jsonl_tail(outpath)
            for line in outpath.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("base_ref"), r.get("cand_ref"), r.get("model")) != (
                    args.base, args.cand, args.model
                ):
                    raise SystemExit(
                        f"{outpath} contains rows from a different experiment "
                        "(different --base/--cand/--model). Use a new --label."
                    )
                done.add((r["task"], r["arm"], r["rep"]))

        failures: list[str] = []
        with open(outpath, "a", encoding="utf-8") as f:
            for task_id, task in TASKS_BY_ID.items():
                if args.tasks and task_id not in args.tasks:
                    continue
                for rep in range(args.reps):
                    for arm_name in arm_names:
                        if (task_id, arm_name, rep) in done:
                            continue
                        attempts = 3
                        for attempt in range(attempts):
                            try:
                                r = run_one(client, args.model, arm_name,
                                            blocks[arm_name], task_id,
                                            task.prompt, task.oracle)
                                if (not r["final"].strip() and r["n_loads"] == 0
                                        and attempt < attempts - 1):
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
                                if attempt == attempts - 1:
                                    failures.append(f"{task_id}/{arm_name}/rep{rep}")
                                else:
                                    time.sleep(3 * (attempt + 1))

        # A cell that exhausted its retries leaves the JSONL incomplete; report
        # a nonzero status rather than printing "done" over biased results.
        if failures:
            raise SystemExit(
                f"INCOMPLETE: {len(failures)} cell(s) failed all attempts: "
                f"{', '.join(failures)}. Results in {outpath} are partial."
            )
        print("done ->", outpath)


if __name__ == "__main__":
    main()
