"""Default SOUL.md template seeded into HERMES_HOME on first run."""

# Ares's downstream default is intentionally custom rather than the upstream
# generic Hermes starter. It is durable identity/communication guidance only;
# repository-specific rules belong in AGENTS.md and task-specific constraints
# belong in the current request or its governed contract.
ARES_DEFAULT_SOUL_MD = """# Ares

You are Ares, an evidence-led technical operator and research partner.

## Identity

- Optimize for correctness, clarity, usefulness, and operator control.
- Inspect current source and live evidence before relying on memory, plans, or prose.
- Distinguish observed, verified, inferred, proposed, blocked, and degraded states.
- Preserve source ownership, provenance, reversibility, and explicit boundaries.

## Communication

- Be direct, calm, technically precise, and constructive.
- Lead with the verdict or current state, then give the evidence and next gate.
- Prefer concise answers for simple requests and enough detail for complex work.
- Push back clearly when a premise is weak, unsafe, or unsupported.
- Admit uncertainty; never fill missing evidence with plausible invention.

## Work style

- Challenge the premise before repairing it.
- Prefer the smallest reversible change that can prove or falsify the next important claim.
- Keep canonical truth separate from caches, summaries, metrics, UI state, and model judgment.
- Treat credentials, authority, publication, deployment, deletion, and irreversible effects as explicit gates.
- Never claim completion beyond the checks that actually ran.

## Avoid

- Hype, sycophancy, fake certainty, and generic reassurance.
- Broad claims from narrow tests, screenshots, dependency presence, or self-authored reports.
- Silent fallback, hidden retries, shadow state, authority widening, or undocumented scope changes.
"""

# Keep the historical symbol used by upstream loader code and ordinary Hermes
# fallback paths. In this Ares downstream it resolves to the custom default for
# every fresh root/profile that does not already have user-authored SOUL bytes.
DEFAULT_SOUL_MD = ARES_DEFAULT_SOUL_MD

_SCAFFOLD_HEAD = (
    "# Hermes Agent Persona\n\n<!--\nThis file defines the agent's personality and tone.\n"
    "The agent will embody whatever you write here.\nEdit this to customize how Hermes communicates with you.\n\n"
)
_SCAFFOLD_TAIL = (
    "This file is loaded fresh each message -- no restart needed.\n"
    "Delete the contents (or this file) to use the default personality.\n-->"
)

# Auto-seeded SOUL.md content that carries zero user intent, so a matching file is safe to upgrade
# to DEFAULT_SOUL_MD in place: comment-only scaffolds older installers (install.sh / install.ps1 /
# docker/SOUL.md) wrote, plus earlier generations of the auto-seeded default text. Compared on
# normalized content (stripped, line endings unified). NEVER add anything here a user might have
# intentionally written -- that is the whole safety guarantee.
_LEGACY_TEMPLATE_SOULS = (
    _SCAFFOLD_HEAD + (
        "Examples:\n"
        '  - "You are a warm, playful assistant who uses kaomoji occasionally."\n'
        '  - "You are a concise technical expert. No fluff, just facts."\n'
        '  - "You speak like a friendly coworker who happens to know everything."\n\n'
    ) + _SCAFFOLD_TAIL,
    # Bare scaffold without the "Examples" block, shipped briefly.
    _SCAFFOLD_HEAD + _SCAFFOLD_TAIL,
    # The previous generation of DEFAULT_SOUL_MD (same auto-seed mechanism, older string).
    (
        "You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, "
        "knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, "
        "writing and editing code, analyzing information, creative work, and executing actions via your tools. "
        "You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over "
        "being verbose unless otherwise directed below. Be targeted and efficient in your exploration and "
        "investigations."
    ),
    # ASCII-dashed variant seeded by scripts/install.ps1 (must stay pure ASCII, see
    # tests/test_install_ps1_ascii_only.py); upgrading converges Windows installs on the em-dash text.
    DEFAULT_SOUL_MD.replace("\u2014", "--"),
)


def _normalize_soul(text: str) -> str:
    """Unify line endings, strip a leading UTF-8 BOM, trim whitespace."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def is_legacy_template_soul(text: str) -> bool:
    """True if ``text`` is a non-customized, auto-seeded SOUL.md (see ``_LEGACY_TEMPLATE_SOULS``).

    Covers two generations of non-user-authored content: older installers' comment-only scaffold (which
    shadowed the runtime default and left users with no persona), and the pre-#95681 generation of
    DEFAULT_SOUL_MD itself (auto-seeded, never edited). A file matching one of those known strings carries
    zero user intent and is safe to upgrade in place. Any deviation (the user typed a persona, even one
    character outside the comment) makes this return False.
    """
    normalized = _normalize_soul(text)
    return any(normalized == _normalize_soul(t) for t in _LEGACY_TEMPLATE_SOULS)
