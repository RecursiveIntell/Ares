#!/usr/bin/env bash
# Ares installer.
#
# Ares is a downstream, Hermes-compatible distribution maintained by
# RecursiveIntell. By default this installer provisions the full Ares
# experience: the stable runtime and launcher, the Desktop application, the
# gateway service, the Recursive Agent plugin payload, the five MCP servers,
# and the skills and hooks packs. Every piece has an explicit opt-out flag
# (see --help); provider credentials, API keys, and the Recursive Agent daemon
# remain explicit operator actions.
set -euo pipefail

REPO_URL="https://github.com/RecursiveIntell/Ares.git"
BRANCH="main"
HERMES_HOME="${HERMES_HOME:-$HOME/.ares}"
INSTALL_DIR=""
ARES_BIN_DIR="${ARES_BIN_DIR:-$HOME/.local/bin}"
USE_VENV=true
INSTALL_DESKTOP=true
INSTALL_GATEWAY=true
INSTALL_SEMANTIC_MEMORY=true
INSTALL_AGENT_GRAPH=true
INSTALL_CLAIM_LEDGER=true
INSTALL_CEA_GRAPH=true
INSTALL_PILOT_BRIDGE=true
INSTALL_SKILLS=true
INSTALL_HOOKS=true
INSTALL_RECURSIVE_AGENT=true
RECURSIVE_AGENT_SOURCE=""

RECURSIVE_AGENT_REPO="https://github.com/RecursiveIntell/recursive-agent.git"
RELEASE_BASE="https://github.com/RecursiveIntell/Ares/releases/latest/download"
SKILLS_PACK_ASSET="hermes-skills-20260803.tar.gz"
HOOKS_PACK_ASSET="hermes-hooks-20260803.tar.gz"

_OS=""
_ARCH=""
_ARES_TMP_DIR=""
_AG_UNIT_NAME=""

log() { printf '[ares] %s\n' "$*"; }
warn() { printf '[ares] warning: %s\n' "$*" >&2; }
die() { printf '[ares] error: %s\n' "$*" >&2; exit 1; }

show_help() {
    cat <<'EOF'
Ares Installer

Install the full Ares downstream distribution of Hermes Agent: the stable
runtime and `ares` launcher, the Desktop application, the gateway service,
the Recursive Agent plugin payload, five MCP servers (semantic-memory,
agent-graph, claim-ledger, cea-graph, pilot-bridge), and the skills and hooks
packs.

Everything is installed by default. Each piece has an opt-out flag; pass the
ones you do not want.

Usage:
  bash install.sh [options]

Install selection (default: everything):
  --no-desktop                 Do not build or install the Ares Desktop application
  --no-gateway                 Do not install, enable, or start the Ares gateway service
  --no-mcp                     Do not install any of the five MCP servers below
  --no-semantic-memory         Do not install the semantic-memory MCP server
  --no-agent-graph             Do not install the agent-graph MCP server or daemon
  --no-claim-ledger            Do not install the claim-ledger MCP server
  --no-cea-graph               Do not install the cea-graph MCP server
  --no-pilot-bridge            Do not install the pilot-bridge MCP server
  --no-skills                  Do not install the skills pack
  --no-hooks                   Do not install the agent hooks pack
  --no-recursive-agent         Do not install the Recursive Agent plugin payload
  --with-recursive-agent-source PATH
                               Install the Recursive Agent plugin from an existing
                               RecursiveIntell/recursive-agent checkout instead of the
                               auto-provisioned checkout. Implies the plugin is enabled.
                               The Recursive Agent daemon is not installed or started by this option.

Layout:
  --branch NAME                Git branch to install (default: main)
  --dir PATH                   Source checkout directory (default: <hermes-home>/ares-agent)
  --hermes-home PATH           Ares data directory (default: ~/.ares)
  --ares-bin-dir PATH          Directory for the `ares` launcher and MCP binaries (default: ~/.local/bin)
  --no-venv                    Use the active Python environment instead of a managed .venv
  -h, --help                   Show this help

Prerequisites: git and Python 3.11 through 3.14. uv is installed
automatically when missing (skip with --no-venv and an active environment).

The installer never creates provider credentials and never sets API keys.
Run `ares auth` or the setup flow inside `ares chat` to configure a model
provider. MCP servers and the plugin payload are installed and registered;
their daemons and evidence remain separate operator-verified layers.
EOF
}

while (($#)); do
    case "$1" in
        --branch) BRANCH="${2:?--branch requires a value}"; shift 2 ;;
        --dir) INSTALL_DIR="${2:?--dir requires a value}"; shift 2 ;;
        --hermes-home) HERMES_HOME="${2:?--hermes-home requires a value}"; shift 2 ;;
        --ares-bin-dir) ARES_BIN_DIR="${2:?--ares-bin-dir requires a value}"; shift 2 ;;
        --no-venv) USE_VENV=false; shift ;;
        --no-desktop) INSTALL_DESKTOP=false; shift ;;
        --no-gateway) INSTALL_GATEWAY=false; shift ;;
        --no-mcp)
            INSTALL_SEMANTIC_MEMORY=false
            INSTALL_AGENT_GRAPH=false
            INSTALL_CLAIM_LEDGER=false
            INSTALL_CEA_GRAPH=false
            INSTALL_PILOT_BRIDGE=false
            shift ;;
        --no-semantic-memory) INSTALL_SEMANTIC_MEMORY=false; shift ;;
        --no-agent-graph) INSTALL_AGENT_GRAPH=false; shift ;;
        --no-claim-ledger) INSTALL_CLAIM_LEDGER=false; shift ;;
        --no-cea-graph) INSTALL_CEA_GRAPH=false; shift ;;
        --no-pilot-bridge) INSTALL_PILOT_BRIDGE=false; shift ;;
        --no-skills) INSTALL_SKILLS=false; shift ;;
        --no-hooks) INSTALL_HOOKS=false; shift ;;
        --no-recursive-agent) INSTALL_RECURSIVE_AGENT=false; shift ;;
        --with-recursive-agent-source)
            RECURSIVE_AGENT_SOURCE="${2:?--with-recursive-agent-source requires a value}"; shift 2 ;;
        -h|--help) show_help; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

if [[ "$INSTALL_RECURSIVE_AGENT" != true && -n "$RECURSIVE_AGENT_SOURCE" ]]; then
    die "conflicting options: --no-recursive-agent with --with-recursive-agent-source"
fi

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

cleanup() {
    if [[ -n "$_ARES_TMP_DIR" && -d "$_ARES_TMP_DIR" ]]; then
        rm -rf "$_ARES_TMP_DIR"
    fi
}
trap cleanup EXIT

detect_os() {
    case "$(uname -s)" in
        Linux)  _OS="linux" ;;
        Darwin) _OS="macos" ;;
        *)      _OS="unsupported" ;;
    esac
    _ARCH="$(uname -m)"
    log "detected platform: $_OS / $_ARCH"
}

prebuilt_linux_x64_available() {
    [[ "$_OS" == "linux" ]] || return 1
    [[ "$_ARCH" == "x86_64" || "$_ARCH" == "amd64" ]]
}

resolve_layout() {
    if [[ -z "$INSTALL_DIR" ]]; then
        INSTALL_DIR="$HERMES_HOME/ares-agent"
    fi
    INSTALL_DIR="$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$INSTALL_DIR")"
    HERMES_HOME="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$HERMES_HOME")"
    ARES_BIN_DIR="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$ARES_BIN_DIR")"
    local path
    for path in "$INSTALL_DIR" "$HERMES_HOME" "$ARES_BIN_DIR"; do
        case "$path" in
            *\"*|*\\*|*$'\n'*)
                die "unsupported character (double quote, backslash, or newline) in path: $path" ;;
        esac
    done
}

checkout_source() {
    if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
        die "install path exists but is not a Git checkout: $INSTALL_DIR"
    fi

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]]; then
            die "refusing to update a dirty checkout: $INSTALL_DIR"
        fi
        log "updating Ares checkout at $INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch origin "$BRANCH"
        git -C "$INSTALL_DIR" checkout "$BRANCH"
        git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
    else
        log "cloning Ares from $REPO_URL"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
}

install_runtime() {
    if [[ "$USE_VENV" == true ]]; then
        if ! command -v uv >/dev/null 2>&1; then
            log "uv is missing; installing it from https://astral.sh/uv/install.sh"
            curl -LsSf https://astral.sh/uv/install.sh | sh || die "failed to install uv"
            export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
        fi
        require_command uv
        log "creating managed Python environment"
        (cd "$INSTALL_DIR" && uv sync --locked --extra all)
    else
        log "installing into the active Python environment"
        python3 -m pip install -e "$INSTALL_DIR[all]"
    fi
}

install_stable_runtime() {
    log "building the isolated Ares release runtime"
    local setup_args=(setup --source "$INSTALL_DIR")
    [[ "$INSTALL_DESKTOP" == true ]] || setup_args+=(--no-desktop)
    [[ "$INSTALL_GATEWAY" == true ]] || setup_args+=(--no-gateway)
    if [[ "$USE_VENV" == true ]]; then
        ARES_HOME="$HERMES_HOME" ARES_BIN_DIR="$ARES_BIN_DIR" \
            "$INSTALL_DIR/.venv/bin/python" -m ares_runtime.local_runtime "${setup_args[@]}"
    else
        ARES_HOME="$HERMES_HOME" ARES_BIN_DIR="$ARES_BIN_DIR" \
            python3 -m ares_runtime.local_runtime "${setup_args[@]}"
    fi
}

agent_python() {
    if [[ "$USE_VENV" == true && -x "$INSTALL_DIR/.venv/bin/python" ]]; then
        printf '%s\n' "$INSTALL_DIR/.venv/bin/python"
    else
        printf '%s\n' "python3"
    fi
}

fetch_asset() {
    # fetch_asset URL DEST — best-effort download to a temporary sibling, so a
    # failed or partial transfer can never corrupt an existing executable.
    local url="$1" dest="$2"
    local tmp="$dest.tmp.$$"
    mkdir -p "$(dirname "$dest")"
    if curl -fsSL --retry 2 --max-time 600 -o "$tmp" "$url"; then
        mv -f "$tmp" "$dest"
    else
        rm -f "$tmp"
        return 1
    fi
}

# ── MCP servers ──────────────────────────────────────────────────────────────

install_mcp_servers() {
    if [[ "$INSTALL_SEMANTIC_MEMORY" != true && "$INSTALL_AGENT_GRAPH" != true \
        && "$INSTALL_CLAIM_LEDGER" != true && "$INSTALL_CEA_GRAPH" != true \
        && "$INSTALL_PILOT_BRIDGE" != true ]]; then
        log "MCP servers: all opted out; skipping"
        return 0
    fi

    : "${_ARES_TMP_DIR:="$(mktemp -d "${TMPDIR:-/tmp}/ares-install.XXXXXX")"}"
    local plan_file="$_ARES_TMP_DIR/mcp-plan.json"
    local -a plan_entries=()
    local install_memory=false

    if [[ "$INSTALL_SEMANTIC_MEMORY" == true ]]; then
        log "installing semantic-memory MCP server (knowledge base + memory search)"
        local sm_dest="$ARES_BIN_DIR/semantic-memory-mcp"
        if ! prebuilt_linux_x64_available; then
            warn "semantic-memory: prebuilt binary is Linux x86_64-only; build from source (cargo install semantic-memory-mcp)"
        elif fetch_asset "https://github.com/RecursiveIntell/semantic-memory-mcp/releases/latest/download/semantic-memory-mcp-linux-x64" "$sm_dest"; then
            chmod +x "$sm_dest"
            log "  semantic-memory-mcp -> $sm_dest"
            plan_entries+=("\"semantic_memory\": {\"command\": \"$sm_dest\", \"args\": [\"--memory-dir\", \"$HERMES_HOME/semantic-memory.db\"]}")
            install_memory=true
        else
            warn "semantic-memory download failed; build from source (cargo install semantic-memory-mcp)"
        fi
    fi

    local ag_proxy=""
    local ag_daemon=""
    if [[ "$INSTALL_AGENT_GRAPH" == true ]]; then
        log "installing agent-graph MCP server (multi-agent graph orchestration)"
        # Prefer a version-consistent proxy+daemon pair from the published
        # crate (the documented install path). The prebuilt release asset is
        # proxy-only and lags the crate, so it is only the no-cargo fallback.
        local cargo_bin="${CARGO_HOME:-$HOME/.cargo}/bin"
        if [[ -x "$cargo_bin/agent-graph-mcp" && -x "$cargo_bin/agent-graph-mcpd" ]]; then
            ag_proxy="$cargo_bin/agent-graph-mcp"
            ag_daemon="$cargo_bin/agent-graph-mcpd"
            log "  using existing cargo binaries in $cargo_bin"
        elif command -v cargo >/dev/null 2>&1; then
            log "  provisioning agent-graph from crates.io (cargo install --locked agent-graph-mcp)"
            if cargo install --locked agent-graph-mcp >"$_ARES_TMP_DIR/cargo-install.log" 2>&1 \
                && [[ -x "$cargo_bin/agent-graph-mcp" && -x "$cargo_bin/agent-graph-mcpd" ]]; then
                ag_proxy="$cargo_bin/agent-graph-mcp"
                ag_daemon="$cargo_bin/agent-graph-mcpd"
                log "  agent-graph-mcp + agent-graph-mcpd -> $cargo_bin"
            else
                warn "agent-graph cargo provisioning failed (log: $_ARES_TMP_DIR/cargo-install.log)"
            fi
        fi
        if [[ -z "$ag_proxy" ]] && prebuilt_linux_x64_available; then
            local ag_proxy_asset="$ARES_BIN_DIR/agent-graph-mcp"
            if fetch_asset "https://github.com/RecursiveIntell/agent-graph-mcp/releases/latest/download/agent-graph-mcp-linux-x64" "$ag_proxy_asset"; then
                chmod +x "$ag_proxy_asset"
                ag_proxy="$ag_proxy_asset"
                log "  agent-graph-mcp (proxy only) -> $ag_proxy_asset"
            fi
        fi
        if [[ -n "$ag_proxy" ]]; then
            plan_entries+=("\"agent_graph\": {\"command\": \"$ag_proxy\", \"args\": [\"--socket\", \"$HERMES_HOME/agent-graph/run/mcp.sock\"]}")
        else
            warn "agent-graph not installed; provision it manually: cargo install --locked agent-graph-mcp"
        fi
        if [[ -z "$ag_daemon" ]]; then
            warn "agent-graph daemon (agent-graph-mcpd) is not available; graph execution needs it (cargo install --locked agent-graph-mcp)"
        fi
    fi

    if [[ "$INSTALL_CLAIM_LEDGER" == true ]]; then
        log "installing claim-ledger MCP server (evidence/claim verification)"
        local cl_dest="$ARES_BIN_DIR/claim-ledger-mcp"
        if ! prebuilt_linux_x64_available; then
            warn "claim-ledger: prebuilt binary is Linux x86_64-only; see the RecursiveIntell/Ares release assets"
        elif fetch_asset "$RELEASE_BASE/claim-ledger-mcp" "$cl_dest"; then
            chmod +x "$cl_dest"
            log "  claim-ledger-mcp -> $cl_dest"
            plan_entries+=("\"claim_ledger\": {\"command\": \"$cl_dest\", \"args\": [\"--ledger-dir\", \"$HERMES_HOME/claim-ledger\"]}")
        else
            warn "claim-ledger download failed; retry, or fetch the release asset from RecursiveIntell/Ares"
        fi
    fi

    if [[ "$INSTALL_CEA_GRAPH" == true ]]; then
        log "installing cea-graph MCP server (causal edit attribution)"
        local cea_dir="$HOME/.local/lib/cea-graph-mcp"
        local cea_tarball="$_ARES_TMP_DIR/cea-graph.tar.gz"
        if ! prebuilt_linux_x64_available; then
            warn "cea-graph: prebuilt package is Linux x86_64-only; build from source"
        elif fetch_asset "$RELEASE_BASE/cea-graph-mcp-linux-x64.tar.gz" "$cea_tarball"; then
            mkdir -p "$cea_dir"
            if tar -xzf "$cea_tarball" -C "$cea_dir"; then
                chmod +x "$cea_dir/cea-graph" "$cea_dir/cea-graph-mcp.py" 2>/dev/null || true
                log "  cea-graph -> $cea_dir"
                plan_entries+=("\"cea_graph\": {\"command\": \"$cea_dir/cea-graph-mcp.py\"}")
            else
                warn "cea-graph archive extraction failed"
            fi
        else
            warn "cea-graph download failed"
        fi
    fi

    if [[ "$INSTALL_PILOT_BRIDGE" == true ]]; then
        log "installing pilot-bridge MCP server (forge-pilot OODA loops)"
        local pb_dir="$HOME/.local/lib/pilot-bridge-mcp"
        local pb_tarball="$_ARES_TMP_DIR/pilot-bridge.tar.gz"
        if ! prebuilt_linux_x64_available; then
            warn "pilot-bridge: prebuilt package is Linux x86_64-only; build from source"
        elif fetch_asset "$RELEASE_BASE/pilot-bridge-mcp-linux-x64.tar.gz" "$pb_tarball"; then
            mkdir -p "$pb_dir"
            if tar -xzf "$pb_tarball" -C "$pb_dir"; then
                chmod +x "$pb_dir/pilot-bridge" "$pb_dir/pilot-bridge-mcp.py" 2>/dev/null || true
                log "  pilot-bridge -> $pb_dir"
                plan_entries+=("\"pilot_bridge\": {\"command\": \"$pb_dir/pilot-bridge-mcp.py\"}")
            else
                warn "pilot-bridge archive extraction failed"
            fi
        else
            warn "pilot-bridge download failed"
        fi
    fi

    if [[ "${#plan_entries[@]}" -gt 0 ]]; then
        local joined="" entry
        for entry in "${plan_entries[@]}"; do
            joined+="${joined:+,}$entry"
        done
        printf '{"mcp_servers": {%s}, "disable_builtin_memory": %s}\n' \
            "$joined" "$install_memory" > "$plan_file"
        log "registering MCP servers in $HERMES_HOME/config.yaml (typed merge)"
        if (cd "$INSTALL_DIR" && "$(agent_python)" -m ares_runtime.integrations \
                register-mcp --home "$HERMES_HOME" --plan "$plan_file"); then
            log "MCP registration complete; servers appear in a fresh Ares session"
        else
            warn "MCP registration failed; rerun later from $INSTALL_DIR:"
            warn "  .venv/bin/python -m ares_runtime.integrations register-mcp --home \"$HERMES_HOME\" --plan <plan>"
        fi
    else
        warn "no MCP servers were installed; skipping registration"
    fi

    install_agent_graph_unit "$ag_daemon"
}

install_agent_graph_unit() {
    local daemon="$1"
    # Scope graph state and service identity to the selected Ares home, so
    # independent homes cannot read or mutate each other's graph daemon/store.
    local data_dir="$HERMES_HOME/agent-graph"
    local socket="$data_dir/run/mcp.sock"

    if [[ -z "$daemon" ]]; then
        log "agent-graph daemon not provisioned; unit not installed (graph tools need it running)"
        return 0
    fi

    mkdir -p "$data_dir/run"
    local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    local default_home
    default_home="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$HOME/.ares")"
    local unit_name="agent-graph-mcpd.service"
    if [[ "$HERMES_HOME" != "$default_home" ]]; then
        local home_key
        home_key="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:10])' "$HERMES_HOME")"
        unit_name="agent-graph-mcpd-$home_key.service"
    fi
    local unit_path="$unit_dir/$unit_name"
    local env_file="$HERMES_HOME/agent-graph.env"
    if [[ ! -f "$env_file" ]]; then
        printf '%s\n' \
            "# API key for the agent-graph daemon (OpenAI-compatible providers)." \
            "# OPENAI_API_KEY=sk-..." > "$env_file"
        chmod 600 "$env_file"
    fi

    _AG_UNIT_NAME="$unit_name"
    if [[ -f "$unit_path" ]]; then
        log "keeping the existing $unit_name (edit it for your provider/model settings)"
    else
        cat > "$unit_path" << UNITEOF
# Written by the Ares installer (install.sh). Adjust the provider base URL
# and model below to match your environment, then:
#   systemctl --user daemon-reload && systemctl --user restart $unit_name
[Unit]
Description=Agent Graph MCP daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$daemon --data-dir $data_dir --socket $socket --base-url https://api.deepseek.com/v1 --model deepseek-v4-pro --max-graphs 256
Restart=on-failure
RestartSec=3s
MemoryHigh=2G
MemoryMax=4G
NoNewPrivileges=true
UMask=0077
Environment=RUST_LOG=info
EnvironmentFile=-$env_file

[Install]
WantedBy=default.target
UNITEOF
    fi

    if systemctl --user daemon-reload 2>/dev/null \
        && systemctl --user enable "$unit_name" 2>/dev/null; then
        log "agent-graph daemon unit enabled (starts on next login)"
    else
        warn "could not enable $unit_name (no systemd user bus?)"
    fi
    log "set an API key in $env_file, then: systemctl --user start $unit_name"
}

# ── Skills and hooks packs ───────────────────────────────────────────────────

install_packs() {
    if [[ "$INSTALL_SKILLS" != true && "$INSTALL_HOOKS" != true ]]; then
        log "skills/hooks packs: opted out; skipping"
        return 0
    fi

    if [[ "$INSTALL_SKILLS" == true ]]; then
        log "installing the skills pack into $HERMES_HOME/skills"
        mkdir -p "$HERMES_HOME/skills"
        if curl -fsSL --retry 2 --max-time 600 "$RELEASE_BASE/$SKILLS_PACK_ASSET" \
            | tar -xz -C "$HERMES_HOME/skills" --strip-components=1; then
            log "  skills pack installed (existing same-named skills refreshed)"
        else
            warn "skills pack download or extraction failed (the agent still works without it)"
        fi
    fi

    if [[ "$INSTALL_HOOKS" == true ]]; then
        log "installing the agent hooks pack into $HERMES_HOME/agent-hooks"
        mkdir -p "$HERMES_HOME/agent-hooks"
        if curl -fsSL --retry 2 --max-time 600 "$RELEASE_BASE/$HOOKS_PACK_ASSET" \
            | tar -xz -C "$HERMES_HOME/agent-hooks"; then
            log "  hooks pack extracted to $HERMES_HOME/agent-hooks (hook registration and allowlisting remain explicit operator steps; review the pack's INTEGRATIONS.md before use)"
        else
            warn "hooks pack download or extraction failed (the agent still works without it)"
        fi
    fi
}

# ── Recursive Agent plugin payload ───────────────────────────────────────────

install_recursive_agent_plugin() {
    [[ "$INSTALL_RECURSIVE_AGENT" == true ]] || { log "recursive-agent plugin: opted out; skipping"; return 0; }

    local plugin_dir="$HERMES_HOME/plugins/recursive-agent-native"
    if [[ -e "$plugin_dir" ]]; then
        log "recursive-agent plugin payload already present at $plugin_dir; skipping (uninstall first to reinstall)"
        return 0
    fi

    local src=""
    if [[ -n "$RECURSIVE_AGENT_SOURCE" ]]; then
        src="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$RECURSIVE_AGENT_SOURCE")"
        local explicit_installer="$src/scripts/install-hermes-plugin.sh"
        [[ -x "$explicit_installer" ]] || die "Recursive Agent plugin installer not found or not executable: $explicit_installer"
    else
        src="$HERMES_HOME/recursive-agent-src"
        if [[ -d "$src/.git" ]]; then
            if [[ -n "$(git -C "$src" status --porcelain)" ]]; then
                warn "recursive-agent checkout at $src is dirty; skipping plugin install"
                return 0
            fi
            log "updating recursive-agent checkout at $src"
            git -C "$src" pull --ff-only >/dev/null 2>&1 || warn "could not fast-forward $src; using the current revision"
        elif [[ -e "$src" ]]; then
            warn "$src exists but is not a Git checkout; skipping plugin install"
            return 0
        else
            log "cloning recursive-agent from $RECURSIVE_AGENT_REPO"
            mkdir -p "$(dirname "$src")"
            if ! git clone --depth 1 "$RECURSIVE_AGENT_REPO" "$src" >/dev/null 2>&1; then
                warn "recursive-agent clone failed; skipping plugin install"
                return 0
            fi
        fi
    fi

    local installer="$src/scripts/install-hermes-plugin.sh"
    [[ -f "$installer" ]] || { warn "plugin installer missing in $src; skipping"; return 0; }
    log "installing the Recursive Agent plugin payload from $src"
    if HERMES_HOME="$HERMES_HOME" bash "$installer"; then
        log "plugin payload installed; the Recursive Agent daemon remains an explicit operator-managed prerequisite"
        log "start a fresh Ares session so plugin discovery can occur"
    else
        warn "plugin installation failed; rerun later: HERMES_HOME=\"$HERMES_HOME\" bash \"$installer\""
    fi
}

refresh_gateway() {
    # The gateway discovers MCP servers and plugins at process startup; one
    # that was started during `ares setup` would otherwise miss every
    # integration provisioned afterwards. Restart it when it is running.
    [[ "$INSTALL_GATEWAY" == true ]] || return 0
    if systemctl --user is-active --quiet ares-gateway.service 2>/dev/null; then
        log "restarting ares-gateway.service so it discovers the newly installed MCP servers and plugin"
        systemctl --user restart ares-gateway.service 2>/dev/null \
            || warn "could not restart ares-gateway.service; restart it manually"
    fi
}

print_summary() {
    echo
    log "Ares installed"
    log "launcher: $ARES_BIN_DIR/ares"
    log "data home: $HERMES_HOME"
    echo "[ares] next steps:"
    echo "  ares chat | ares tui | ares desktop   # start the agent (fresh session for new tools)"
    echo "  ares doctor                           # verify the selected runtime"
    echo "  ares auth                             # configure model provider credentials"
    if [[ -n "$_AG_UNIT_NAME" ]]; then
        echo "  edit $HERMES_HOME/agent-graph.env and 'systemctl --user start $_AG_UNIT_NAME' for multi-agent graphs"
    fi
}

main() {
    require_command git
    require_command python3
    detect_os
    resolve_layout
    checkout_source
    install_runtime
    install_stable_runtime
    install_mcp_servers
    install_packs
    install_recursive_agent_plugin
    refresh_gateway
    print_summary
}

# Execute when run as a file or piped (e.g. `curl ... | bash`); stay inert
# when sourced so test harnesses can load the functions without running main.
# Under `bash < install.sh` and `curl | bash`, BASH_SOURCE[0] is empty.
if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
