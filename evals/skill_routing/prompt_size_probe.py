"""Skill-index context-cost probe.

Records how much of a fresh session's system prompt the ``<available_skills>``
index occupies, so the index's share of the fixed prompt budget is measured
rather than asserted. Reuses the canonical ``hermes prompt-size`` accounting
(``hermes_cli.prompt_size.compute_prompt_breakdown``) instead of re-deriving
byte counts — one source of truth for prompt measurement.

Usage:
  python3 evals/skill_routing/prompt_size_probe.py --json
  python3 evals/skill_routing/prompt_size_probe.py --label baseline
  python3 evals/skill_routing/prompt_size_probe.py --self-test

The probe is read-only: it builds an inspection agent with dummy credentials
and never makes a network call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# A session whose skills index is a third of its system prompt is index-bound:
# below this, index size is not a first-order cost. Not a target — a
# discriminator that must fire on a real index and stay quiet on a near-empty
# one (see --self-test).
INDEX_RATIO_CONTRACT = 0.30


def index_ratio(skills_index_bytes: int, system_prompt_bytes: int) -> float:
    """Share of the system prompt consumed by the skills index (0.0..1.0)."""
    if system_prompt_bytes <= 0:
        return 0.0
    return skills_index_bytes / system_prompt_bytes


def is_index_bound(skills_index_bytes: int, system_prompt_bytes: int) -> bool:
    """True when the skills index meets or exceeds INDEX_RATIO_CONTRACT."""
    return index_ratio(skills_index_bytes, system_prompt_bytes) >= INDEX_RATIO_CONTRACT


def measure(platform: str = "cli") -> dict:
    """Measure the live index cost for *platform* via the canonical diagnostic."""
    from hermes_cli.prompt_size import compute_prompt_breakdown

    data = compute_prompt_breakdown(platform)
    si = data["skills_index"]["bytes"]
    sp = data["system_prompt"]["bytes"]
    skills = data.get("skills_breakdown") or []

    # Per-category byte shares: the categories whose descriptions cost the
    # most are the candidates a future demotion decision would weigh.
    by_cat: dict[str, int] = {}
    for sk in skills:
        # skill names in the index are flat; attribute each line's bytes to the
        # owning top-level category when the name maps to a known prefix.
        cat = sk["name"].split("/", 1)[0]
        by_cat[cat] = by_cat.get(cat, 0) + sk.get("index_line_bytes", 0)

    return {
        "platform": platform,
        "model": data.get("model", ""),
        "system_prompt_bytes": sp,
        "skills_index_bytes": si,
        "skills_index_ratio": round(index_ratio(si, sp), 4),
        "index_bound": is_index_bound(si, sp),
        "skill_count": len(skills),
        "index_bytes_by_category": dict(
            sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
    }


def _self_test() -> int:
    """Both polarities: contract fires on a real-sized index, quiet on a tiny one."""
    # A 5-skill index is a small fraction of any system prompt.
    assert not is_index_bound(2_000, 48_000), "contract fired on a near-empty index"
    # The measured live index (265 skills / ~26.6 KB of a ~49 KB prompt).
    assert is_index_bound(26_605, 48_948), "contract stayed quiet on a real index"
    assert round(index_ratio(26_605, 48_948), 2) == 0.54
    # Degenerate input must not divide by zero.
    assert index_ratio(0, 0) == 0.0
    print("self-test: OK (contract discriminates both polarities)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Skill-index context-cost probe")
    ap.add_argument("--platform", default="cli")
    ap.add_argument("--label", default="", help="write results/<label>/prompt_size.json")
    ap.add_argument("--json", action="store_true", help="print the record as JSON")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    record = measure(args.platform)

    if args.label:
        out_dir = EVAL_DIR / "results" / args.label
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "prompt_size.json"
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(f"wrote {out_path}")
    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(f"platform={record['platform']} model={record['model']}")
        print(f"  system prompt : {record['system_prompt_bytes']:,} B")
        print(f"  skills index  : {record['skills_index_bytes']:,} B "
              f"({record['skills_index_ratio']:.0%} of prompt, "
              f"{record['skill_count']} skills)")
        print(f"  index-bound   : {record['index_bound']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
