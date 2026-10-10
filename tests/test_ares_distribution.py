"""Contract tests for the Ares downstream distribution surface.

Ares intentionally preserves the ``hermes`` package/CLI for compatibility with
Hermes Agent integrations.  These tests protect the public fork identity and
prevent the installer from silently modifying a user's structured config.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_readme_declares_ares_as_a_downstream_distribution() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "# Ares" in readme
    assert "downstream distribution" in readme
    assert "not an official Nous Research product" in readme
    assert "https://github.com/RecursiveIntell/hermes-agent" in readme


def test_website_front_door_carries_ares_identity() -> None:
    config = (REPO_ROOT / "website" / "docusaurus.config.ts").read_text(
        encoding="utf-8"
    )
    index = (REPO_ROOT / "website" / "docs" / "index.mdx").read_text(encoding="utf-8")

    assert "title: 'Ares'" in config
    assert "https://recursiveintell.github.io" in config
    assert "baseUrl: '/hermes-agent/docs/'" in config
    assert "src: 'img/ares-logo.svg'" in config
    assert (REPO_ROOT / "website" / "static" / "img" / "ares-logo.svg").is_file()
    assert "# Ares" in index
    assert "Hermes-compatible" in index


def test_ares_installer_defaults_to_the_full_set_with_opt_outs() -> None:
    installer = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "Ares Installer" in installer

    # Every default-on piece exposes an opt-out flag.
    for flag in (
        "--no-desktop",
        "--no-gateway",
        "--no-mcp",
        "--no-semantic-memory",
        "--no-agent-graph",
        "--no-claim-ledger",
        "--no-cea-graph",
        "--no-pilot-bridge",
        "--no-skills",
        "--no-hooks",
        "--no-recursive-agent",
    ):
        assert flag in installer, flag

    for name in (
        "INSTALL_DESKTOP",
        "INSTALL_GATEWAY",
        "INSTALL_SEMANTIC_MEMORY",
        "INSTALL_AGENT_GRAPH",
        "INSTALL_CLAIM_LEDGER",
        "INSTALL_CEA_GRAPH",
        "INSTALL_PILOT_BRIDGE",
        "INSTALL_SKILLS",
        "INSTALL_HOOKS",
        "INSTALL_RECURSIVE_AGENT",
    ):
        assert f"{name}=true" in installer, name
        assert f"{name}=false" in installer, name

    assert "setup_args+=(--no-gateway)" in installer

    # The Recursive Agent daemon boundary is preserved.
    assert "--with-recursive-agent-source PATH" in installer
    assert (
        "Recursive Agent daemon is not installed or started by this option" in installer
    )

    # Structured configuration stays out of the shell installer; typed edits
    # are owned by ares_runtime.integrations.
    assert "config set" not in installer
    assert "yaml.safe_dump" not in installer
    assert "ares_runtime.integrations" in installer


def test_ares_docs_are_deployed_by_the_downstream_workflow() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy-site.yml").read_text(
        encoding="utf-8"
    )
    llms_generator = (
        REPO_ROOT / "website" / "scripts" / "generate-llms-txt.py"
    ).read_text(encoding="utf-8")

    assert "github.repository == 'RecursiveIntell/hermes-agent'" in workflow
    assert "https://recursiveintell.github.io/hermes-agent/docs" in llms_generator
    assert 'lines.append("# Ares")' in llms_generator
