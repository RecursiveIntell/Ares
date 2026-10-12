#!/usr/bin/env bash
# Ares full installer — the canonical installation entrypoint for the
# RecursiveIntell/Ares distribution. `recursiveintell-web/public/ares/install.sh`
# is kept as a byte-identical mirror of this file; update both together.
# Provider secrets are handled by Ares's local model wizard, never by the
# installer or command-line arguments.
# Do not leave traps, shell options, or an exit in the caller's interactive shell.
if [ -z "${BASH_VERSION:-}" ]; then
  printf '[Ares] Run this installer with bash, not sh.\n' >&2
  exit 2
fi
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '[Ares] Execute this installer with bash; do not source it.\n' >&2
  return 2
fi
set -Eeuo pipefail
umask 077

ARES_HOME="${ARES_HOME:-$HOME/.ares}"
ARES_BIN_DIR="${ARES_BIN_DIR:-$HOME/.local/bin}"
BRANCH=main
DESKTOP=true
GATEWAY=true
EXTRAS=true
SETUP=true
PLAN=false
MODIFY_PATH=true
RECURSIVE_AGENT=true
RECURSIVE_AGENT_SOURCE=""
STEP=arguments
CONFIG_PENDING=false
TEMP_DIR=""
INSTALL_LOCK=""
# Reviewed, aged release; matches the inherited installer's <0.13 policy.
# Update intentionally with the policy/installer qualification, never to latest.
ARES_UV_VERSION=0.12.19


log() { printf '[Ares] %s\n' "$*"; }
die() { printf '[Ares] %s\n' "$*" >&2; on_error 1; }
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$TEMP_DIR" ]]; then rm -rf -- "$TEMP_DIR"; fi
  if [[ -n "$INSTALL_LOCK" && ! -L "$INSTALL_LOCK" && -f "$INSTALL_LOCK/pid" ]]; then
    if [[ "$(cat "$INSTALL_LOCK/pid")" == "$$" ]]; then
      rm -f -- "$INSTALL_LOCK/pid"
      rmdir -- "$INSTALL_LOCK" || printf '[Ares] Installer lock retained: %s\n' "$INSTALL_LOCK" >&2
    fi
  fi
  return "$status"
}
trap cleanup EXIT
restore_configuration() {
  "$BOOTSTRAP_PYTHON" - "$ARES_INSTALL_BACKUP" <<'PY'
import json, shutil, sys
from pathlib import Path
backup = Path(sys.argv[1])
manifest = backup/'files.json'
if manifest.exists():
    record = json.loads(manifest.read_text())
    home = Path(record['home'])
    current = home/'runtime'/'current'
    selected = str(current.readlink()) if current.is_symlink() else ('non-link' if current.exists() else None)
    if 'current_link' in record and selected != record['current_link']:
        raise SystemExit('Runtime selection changed. Configuration backup retained; recover through the Ares runtime owner before restoring it.')
    for name in ('config.yaml', '.env'):
        if name in record['existing']:
            shutil.copy2(backup/name, home/name)
        else:
            (home/name).unlink(missing_ok=True)
    if 'plugin' in record:
        plugin = home/'plugins'/'semantic-memory-mcp'
        if plugin.is_symlink() or plugin.is_file():
            plugin.unlink()
        elif plugin.exists():
            shutil.rmtree(plugin)
        saved = backup/'semantic-memory-mcp'
        if saved.is_symlink():
            plugin.symlink_to(saved.readlink(), target_is_directory=True)
        elif saved.exists():
            shutil.copytree(saved, plugin, symlinks=True)
    for name in record.get('managed_paths', []):
        if name not in ('skills/ares', 'skills/memory-kit', 'skills/.ares-install-manifest.json', 'plugins/ares-full-stack'):
            raise SystemExit('Unexpected managed backup path; recovery refused.')
        target, saved = home/name, backup/'managed'/name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        if saved.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(saved.readlink(), target_is_directory=True)
        elif saved.is_dir():
            shutil.copytree(saved, target, symlinks=True)
        elif saved.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
    print('[Ares] Restored installer-managed configuration after the failed activation.')
PY
}
on_error() {
  local status="${1:-$?}"
  trap - ERR INT TERM HUP
  # ERR is inherited into substitutions/functions. Only the top-level shell
  # owns recovery; a failed child must not restore the same backup twice.
  if (( BASH_SUBSHELL > 0 )); then exit "$status"; fi
  if [[ "$CONFIG_PENDING" == true ]]; then
    restore_configuration || printf '[Ares] Configuration recovery failed; retained backup: %s\n' "$ARES_INSTALL_BACKUP" >&2
  fi
  printf '[Ares] Installation stopped during %s (exit %s). No complete-install claim was made.\n' "$STEP" "$status" >&2
  exit "$status"
}
trap on_error ERR
trap 'on_error 130' INT
trap 'on_error 143' TERM
trap 'on_error 129' HUP

help() {
  cat <<'EOF'
Ares full installer
Usage: bash install.sh [options]

Default: latest RecursiveIntell/Ares main; managed Python; Desktop and voice;
Rust native bindings; Context Governor; semantic-memory, Agent Graph and
ClaimLedger MCP; CEA and Pilot Bridge CLIs; current memory-kit skills/plugin;
Recursive Agent plugin payload; local provider/API-key/OAuth wizard; isolated
Ares gateway on Linux/systemd.

  --home PATH       Ares data home (default ~/.ares)
  --bin-dir PATH    Launcher directory (default ~/.local/bin)
  --branch NAME     Ares source branch (default main)
  --no-desktop      CLI installation without Desktop/voice downloads
  --no-gateway      Do not install or start a background gateway
  --minimal         Skip RecursiveIntell enhancement builds and memory kit
                    Requires a fresh/minimal home; cannot downgrade a full home
  --skip-setup      Defer provider sign-in, not required-component failures
  --no-path         Do not update shell startup files
  --no-recursive-agent
                    Skip the Recursive Agent plugin payload
  --with-recursive-agent-source PATH
                    Install the plugin payload from an existing
                    RecursiveIntell/recursive-agent checkout instead of the
                    auto-provisioned one (default: on, auto-provisioned under
                    <home>/recursive-agent-src; the Recursive Agent daemon is
                    never installed or started by this installer)
  --plan            Print the install plan without changing anything
  -h, --help        Show this help

Linux and macOS. On Windows use WSL2 with --no-desktop --no-gateway.
Run as your normal user; only missing system packages request sudo.
Use a dedicated data directory, not / or your home directory. Paths containing
control characters, ':' (PATH separator), or dot-traversal components are refused.
Concurrent full installations into the same home are refused without stealing locks.
Source/native builds and Desktop downloads can take time.
Failed required components stop installation.
No OpenAI API key is required unless you choose an API-key provider that needs it.
OAuth availability and account eligibility depend on the selected provider.
To update the full distribution, rerun this installer. `ares update` alone uses
the upstream runtime recipe and does not rebuild these extra native wheels.
EOF
}

value() { [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "$1 needs a value"; }
while (($#)); do
  case "$1" in
    --home) value "$@"; ARES_HOME="$2"; shift 2 ;;
    --bin-dir) value "$@"; ARES_BIN_DIR="$2"; shift 2 ;;
    --branch) value "$@"; BRANCH="$2"; shift 2 ;;
    --no-desktop) DESKTOP=false; shift ;;
    --no-gateway) GATEWAY=false; shift ;;
    --minimal) EXTRAS=false; shift ;;
    --skip-setup) SETUP=false; shift ;;
    --plan) PLAN=true; shift ;;
    --no-path) MODIFY_PATH=false; shift ;;
    --no-recursive-agent) RECURSIVE_AGENT=false; shift ;;
    --with-recursive-agent-source) value "$@"; RECURSIVE_AGENT_SOURCE="$2"; shift 2 ;;
    -h|--help) help; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

if [[ "$RECURSIVE_AGENT" != true && -n "$RECURSIVE_AGENT_SOURCE" ]]; then
  die "conflicting options: --no-recursive-agent with --with-recursive-agent-source"
fi

normalize_directory() {
  local requested="$1" parent suffix="" component
  case "$requested" in '~') requested="$HOME" ;; '~/'*) requested="$HOME/${requested:2}" ;; esac
  [[ "$requested" != *:* && ! "$requested" =~ [[:cntrl:]] ]] || die "Directory contains an unsupported control character or PATH separator."
  [[ "$requested" == /* ]] || requested="$PWD/$requested"
  case "/$requested/" in */../*|*/./*) die "Use a directory without '.' or '..' path components." ;; esac
  parent="${requested%/}"
  while [[ ! -d "$parent" ]]; do
    [[ ! -e "$parent" && ! -L "$parent" ]] || die "Installation directory is not a usable directory."
    component="${parent##*/}"
    suffix="/$component$suffix"
    parent="${parent%/*}"
    [[ -n "$parent" ]] || parent=/
  done
  parent="$(cd -- "$parent" && pwd -P)"
  [[ "$parent$suffix" != *:* && ! "$parent$suffix" =~ [[:cntrl:]] ]] || die "Physical directory contains an unsupported control character or PATH separator."
  printf '%s%s\n' "${parent%/}" "$suffix"
}
ARES_HOME="$(normalize_directory "$ARES_HOME")"
ARES_BIN_DIR="$(normalize_directory "$ARES_BIN_DIR")"
PHYSICAL_HOME="$(cd -- "$HOME" && pwd -P)"
[[ -n "$ARES_HOME" && "$ARES_HOME" != / && "$ARES_HOME" != "$PHYSICAL_HOME" ]] || die "Choose a dedicated Ares data directory, not / or your home directory."
[[ -n "$ARES_BIN_DIR" && "$ARES_BIN_DIR" != / ]] || die "Choose a dedicated launcher directory, not /."
[[ ! "$BRANCH" =~ [[:cntrl:]] && "$BRANCH" != -* ]] || die "Invalid Ares source branch."
if command -v git >/dev/null 2>&1; then
  git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || die "Invalid Ares source branch."
fi

if [[ "$PLAN" == true ]]; then
  help
  log "Plan: Ares branch=$BRANCH; home=$ARES_HOME; Desktop=$DESKTOP; enhancements=$EXTRAS; gateway=$GATEWAY; provider wizard=$SETUP; recursive-agent=$RECURSIVE_AGENT"
  log "Toolchain: managed uv $ARES_UV_VERSION; Python 3.13. No changes made."
  exit 0
fi
if [[ "$SETUP" == true ]] && ! ( : </dev/tty ) 2>/dev/null; then
  die "Provider selection needs a terminal. Run interactively, or use --skip-setup and configure locally later."
fi

STEP=preflight
OS="$(uname -s)"
[[ "$OS" == Linux || "$OS" == Darwin ]] || die "Use Linux, macOS, or WSL2 (see --help)."
# This lock serializes this installer's mutable build cache/configuration only;
# AresLocalRuntime retains exclusive ownership of activation and its own lock.
mkdir -p -- "$ARES_HOME"
if ! mkdir -- "$ARES_HOME/.full-install.lock" 2>/dev/null; then
  die "Another full installer owns $ARES_HOME/.full-install.lock. After confirming no installer remains, remove only that stale lock directory and retry."
fi
INSTALL_LOCK="$ARES_HOME/.full-install.lock"
printf '%s\n' "$$" > "$INSTALL_LOCK/pid"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ares-install.XXXXXXXX")"
STEP=prerequisites
log "Checking system prerequisites for the selected components."
elevated() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; return; fi
  command -v sudo >/dev/null 2>&1 || die "sudo is required to install missing system dependencies."
  sudo "$@"
}
if [[ "$OS" == Linux ]]; then
  # Compiler and headers are needed for current Rust/Python/native builds.
  if command -v apt-get >/dev/null 2>&1; then
    packages=(git curl build-essential pkg-config libssl-dev unzip)
    [[ "$DESKTOP" == false ]] || packages+=(libgtk-3-dev libnss3 libgbm-dev libasound2-dev libx11-xcb1 libportaudio2 ffmpeg)
    missing=()
    for package in "${packages[@]}"; do
      # pkgconf can supply the tool without the transitional pkg-config package.
      if [[ "$package" == pkg-config ]] && command -v pkg-config >/dev/null 2>&1; then continue; fi
      [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" == installed ]] || missing+=("$package")
    done
    if ((${#missing[@]})); then
      elevated apt-get update
      elevated apt-get install -y "${missing[@]}"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    packages=(git curl gcc gcc-c++ make pkgconf-pkg-config openssl-devel unzip)
    [[ "$DESKTOP" == false ]] || packages+=(gtk3-devel nss mesa-libgbm alsa-lib-devel portaudio ffmpeg-free)
    elevated dnf install -y "${packages[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    packages=(git curl base-devel pkgconf openssl unzip)
    [[ "$DESKTOP" == false ]] || packages+=(gtk3 nss mesa alsa-lib portaudio ffmpeg)
    elevated pacman -S --needed --noconfirm "${packages[@]}"
  else
    for tool in git curl cc c++ make pkg-config; do
      command -v "$tool" >/dev/null 2>&1 || die "Missing $tool. Install your distribution's build prerequisites, then rerun."
    done
  fi
else
  if ! xcode-select -p >/dev/null 2>&1; then
    xcode-select --install
    die "Complete the macOS Command Line Tools dialog, then rerun this command."
  fi
  for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ ! -x "$brew_bin" ]] || export PATH="$(dirname "$brew_bin"):$PATH"
  done
  if ! command -v brew >/dev/null 2>&1; then
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 20 --max-time 180 https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$TEMP_DIR/homebrew-install.sh"
    NONINTERACTIVE=1 /bin/bash "$TEMP_DIR/homebrew-install.sh"
    for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [[ ! -x "$brew_bin" ]] || export PATH="$(dirname "$brew_bin"):$PATH"
    done
  fi
  packages=(pkgconf openssl@3)
  [[ "$DESKTOP" == false ]] || packages+=(portaudio ffmpeg)
  brew install "${packages[@]}"
  openssl_prefix="$(brew --prefix openssl@3)"
  export PKG_CONFIG_PATH="$openssl_prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v git >/dev/null 2>&1 || die "git is required."

# Download each bootstrap completely before executing it; retain normal stdin.
# Ares's locked dependencies require its own [tool.uv] resolver settings.
log "Using installer-owned uv settings; only an absolute UV_CACHE_DIR override is retained."
for uv_option in ${!UV_@}; do
  case "$uv_option" in
    UV_CACHE_DIR) [[ "${!uv_option}" == /* ]] || die "UV_CACHE_DIR must be an absolute path." ;;
    *) unset "$uv_option" ;;
  esac
done
# Provision owned, current tools even when the shell has an older installation.
case "$ARES_HOME" in '~') ARES_HOME="$HOME" ;; '~/'*) ARES_HOME="$HOME/${ARES_HOME:2}" ;; esac
[[ "$ARES_HOME" == /* ]] || ARES_HOME="$PWD/$ARES_HOME"
export UV_INSTALL_DIR="$ARES_HOME/bin" UV_UNMANAGED_INSTALL="$ARES_HOME/bin"
export CARGO_HOME="$ARES_HOME/toolchains/cargo"
export RUSTUP_HOME="$ARES_HOME/toolchains/rustup"
mkdir -p "$CARGO_HOME" "$RUSTUP_HOME"
CARGO_HOME="$(cd "$CARGO_HOME" && pwd -P)"
export CARGO_HOME
RUSTUP_HOME="$(cd "$RUSTUP_HOME" && pwd -P)"
export RUSTUP_HOME
unset PYTHONPATH PYTHONHOME PYTHONINSPECT PYTHONSTARTUP PYTHONOPTIMIZE VIRTUAL_ENV UV_PROJECT_ENVIRONMENT
export PYTHONNOUSERSITE=1
export UV_PYTHON_INSTALL_DIR="$ARES_HOME/toolchains/python"
export UV_PYTHON_INSTALL_BIN=0 UV_MANAGED_PYTHON=1
export PATH="$UV_INSTALL_DIR:$ARES_BIN_DIR:$HOME/.local/bin:$CARGO_HOME/bin:$HOME/.cargo/bin:$PATH"
STEP=toolchains
log "Provisioning managed uv $ARES_UV_VERSION and Python 3.13."
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 20 --max-time 180 "https://astral.sh/uv/$ARES_UV_VERSION/install.sh" -o "$TEMP_DIR/uv-install.sh"
UV_NO_MODIFY_PATH=1 sh "$TEMP_DIR/uv-install.sh"
[[ -x "$UV_INSTALL_DIR/uv" ]] || die "Managed uv provisioning failed."
uv_report="$("$UV_INSTALL_DIR/uv" --version)"
[[ "$uv_report" == "uv $ARES_UV_VERSION" || "$uv_report" == "uv $ARES_UV_VERSION "* ]] || die "Managed uv version does not match the reviewed installer selection."
hash -r
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
if [[ "$EXTRAS" == true ]]; then
  if [[ ! -x "$CARGO_HOME/bin/rustup" ]]; then
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 20 --max-time 180 https://sh.rustup.rs -o "$TEMP_DIR/rustup.sh"
    sh "$TEMP_DIR/rustup.sh" -y --profile minimal --default-toolchain stable --no-modify-path
  else
    "$CARGO_HOME/bin/rustup" update stable --no-self-update
  fi
  export RUSTUP_TOOLCHAIN=stable
fi
uv python install 3.13
PYTHON="$(uv python find 3.13)"
export UV_PYTHON="$PYTHON"
ARES_HOME="$("$PYTHON" -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$ARES_HOME")"
ARES_BIN_DIR="$("$PYTHON" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$ARES_BIN_DIR")"
export ARES_HOME ARES_BIN_DIR HERMES_HOME="$ARES_HOME"
mkdir -p "$ARES_HOME" "$ARES_BIN_DIR"
for managed_path in "$ARES_HOME/installer-sources" "$ARES_HOME/enhancements" "$ARES_HOME/install-receipts" "$ARES_HOME/installer-backups"; do
  [[ ! -L "$managed_path" ]] || die "Installer-managed storage is symlinked; preserved: $managed_path"
done
SOURCE_ROOT="$ARES_HOME/installer-sources"
ENHANCEMENTS="$ARES_HOME/enhancements"
WHEELS="$ENHANCEMENTS/wheels"
export PATH="$UV_INSTALL_DIR:$CARGO_HOME/bin:$ENHANCEMENTS/bin:$ARES_BIN_DIR:$PATH"
mkdir -p "$SOURCE_ROOT" "$WHEELS"
SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd -P)"

checkout() {
  local repo="$1" directory="$2" branch="${3:-main}" origin
  [[ ! -L "$directory" ]] || die "Refusing a symlinked installer checkout: $directory"
  if [[ -e "$directory" ]]; then
    [[ -d "$directory/.git" && ! -L "$directory/.git" ]] || die "Existing path is not an installer-owned checkout: $directory"
    origin="$(git -C "$directory" remote get-url origin)"
    [[ "$origin" == "https://github.com/RecursiveIntell/$repo.git" ]] || die "Unexpected repository origin at $directory"
    [[ -z "$(git -C "$directory" status --porcelain)" ]] || die "Refusing to overwrite local changes at $directory"
    git -C "$directory" fetch origin "$branch"
    if git -C "$directory" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$directory" merge-base --is-ancestor "refs/heads/$branch" FETCH_HEAD || die "Refusing unpublished or diverged local commits at $directory; local history was preserved."
      git -C "$directory" checkout "$branch"
      git -C "$directory" merge --ff-only FETCH_HEAD
    else
      git -C "$directory" checkout -b "$branch" FETCH_HEAD
    fi
    [[ "$(git -C "$directory" rev-parse HEAD)" == "$(git -C "$directory" rev-parse FETCH_HEAD)" ]] || die "Checkout does not match the requested published revision."
  else
    git clone --depth 1 --branch "$branch" "https://github.com/RecursiveIntell/$repo.git" "$directory"
  fi
}

STEP=source
log "Preparing clean published source checkouts."
ARES_SOURCE="$SOURCE_ROOT/Ares"
checkout Ares "$ARES_SOURCE" "$BRANCH"
if [[ -e "$ARES_BIN_DIR/ares" || -L "$ARES_BIN_DIR/ares" ]]; then
  "$PYTHON" - <<'PY'
import os, re, shlex
from pathlib import Path
launcher = Path(os.environ['ARES_BIN_DIR'])/'ares'
text = launcher.read_text()
match = re.search(r'^if \[\[ -z "\$\{ARES_HOME:-\}" \]\]; then export ARES_HOME=(.+); fi$', text, re.MULTILINE)
owner = shlex.split(match[1]) if match else []
if 'ares_runtime.local_runtime' not in text or len(owner) != 1:
    raise SystemExit(f'Refusing to replace an unrelated or unrecognized launcher: {launcher}')
if Path(owner[0]).resolve() != Path(os.environ['ARES_HOME']).resolve():
    raise SystemExit(f'Launcher {launcher} belongs to another Ares home; preserved. Choose a separate --bin-dir for this --home.')
PY
fi
STEP=python
log "Installing the locked Ares Python environment."
(cd "$ARES_SOURCE" && uv sync --locked --extra all --no-dev --python 3.13)
BOOTSTRAP_PYTHON="$ARES_SOURCE/.venv/bin/python"

if [[ "$EXTRAS" == true ]]; then
  STEP=enhancement-sources
  LIBRARIES="$SOURCE_ROOT/Libraries"
  KITS="$SOURCE_ROOT/agent-memory-kits"
  GRAPH="$SOURCE_ROOT/agent-graph-mcp"
  checkout Libraries "$LIBRARIES"
  checkout agent-memory-kits "$KITS"
  checkout agent-graph-mcp "$GRAPH"
  STEP=rust-tools
  # Install the current published MCP package with its packaged lockfile.
  # The Libraries submodule is not a buildable member of its parent workspace.
  cargo install --locked semantic-memory-mcp --root "$ENHANCEMENTS"
  for package in context-governor claim-ledger-mcp cea-graph pilot-bridge; do
    log "Building $package"
    cargo install --locked --path "$LIBRARIES/$package" --root "$ENHANCEMENTS"
  done
  cargo install --locked --path "$GRAPH" --root "$ENHANCEMENTS"
  STEP=native-wheels
  # Ares's configured strict engine uses the current Rust CLI. The separate
  # legacy PyO3 compressor is not this engine and is not part of this recipe.
  # Build once, then install into INACTIVE release environments through Ares's
  # own lifecycle builder. Never run pip against runtime/current.
  rm -f "$WHEELS"/*.whl
  for package in llm-pipeline-python agent-graph-python poly-kv; do
    build_source="$LIBRARIES/$package"
    # Multiple crates export `_native`; a shared target directory can package
    # another crate's cached library on a repeat build. Isolate their outputs.
    (cd "$build_source" && uv tool run --from 'maturin>=1.8,<2' maturin build --locked --release --target-dir "$ENHANCEMENTS/build-targets/$package" --interpreter "$BOOTSTRAP_PYTHON" --out "$WHEELS")
  done
fi

STEP=configuration
export ARES_INSTALL_SOURCE="$ARES_SOURCE" ARES_INSTALL_EXTRAS="$EXTRAS" ARES_INSTALL_DESKTOP="$DESKTOP"
export ARES_INSTALL_WHEELS="$WHEELS" ARES_INSTALL_GATEWAY="$GATEWAY"
export ARES_INSTALL_SETUP="$SETUP"
export ARES_INSTALL_MODIFY_PATH="$MODIFY_PATH"
export ARES_INSTALL_RECIPE_VERSION=4
install_epoch="$(date +%s)"
export ARES_INSTALL_RECEIPT="$ARES_HOME/install-receipts/attempt-$install_epoch-$$.json"
# Derive identity from the physical data home, not a shared per-user name.
# Retain the old unit only for its proven owner, avoiding a duplicate gateway
# on upgrades. Other homes always receive their own distinct unit.
ARES_GATEWAY_UNIT_PATH="$("$BOOTSTRAP_PYTHON" - <<'PY'
import hashlib, os
from pathlib import Path
home = Path(os.environ['ARES_HOME']).resolve()
units = Path.home()/'.config'/'systemd'/'user'
legacy = units/'ares-full-gateway.service'
owners = [line.removeprefix('Environment=HERMES_HOME=') for line in legacy.read_text().splitlines() if line.startswith('Environment=HERMES_HOME=')] if legacy.is_file() else []
owned = len(owners) == 1 and Path(owners[0]).resolve() == home
name = 'ares-full-gateway.service' if owned else f'ares-full-gateway-{hashlib.sha256(os.fsencode(home)).hexdigest()}.service'
print(units/name)
PY
)"
export ARES_GATEWAY_UNIT_PATH

STEP=managed-runtime
log "Building and verifying an inactive runtime; the selected runtime remains owned by Ares."
"$BOOTSTRAP_PYTHON" - <<'PY'
import hashlib, json, os, shutil, subprocess, time
from pathlib import Path
from ares_runtime.local_runtime import AresLocalRuntime
from hermes_cli.managed_uv import ensure_uv

extra = os.environ['ARES_INSTALL_EXTRAS'] == 'true'
desktop = os.environ['ARES_INSTALL_DESKTOP'] == 'true'
home = Path(os.environ['ARES_HOME'])
wheels = sorted(Path(os.environ['ARES_INSTALL_WHEELS']).glob('*.whl'))
if extra and len(wheels) != 3:
    raise SystemExit('Expected three verified native wheels; refusing a partial native installation.')
tools = ['context-governor', 'semantic-memory-mcp', 'claim-ledger-mcp', 'agent-graph-mcpd', 'agent-graph-mcp', 'agent-graph-operator', 'cea-graph', 'pilot-bridge']
sdk = ['mcp==2.2.0', 'mcp-types==2.2.0']
probe = 'from llm_pipeline._native import LlmConfig, Pipeline; from agent_graph._native import AgentState; from poly_kv._native import validate_shape_json'
probe += f'; import os, sys; from pathlib import Path; missing = [name for name in {tools!r} if not (Path(sys.executable).parent/name).is_file() or not os.access(Path(sys.executable).parent/name, os.X_OK)]; sys.exit("missing executable bundled Rust tools" if missing else 0)'
sdk_probe = '''from mcp_types.methods import validate_server_result
validate_server_result('tools/list', '2025-11-25', {'tools':[{'name':'schema_probe','inputSchema':{'type':'object'},'outputSchema':{'type':'object','properties':{'data':True,'blocked':False}}}]})
'''

runtime = AresLocalRuntime()
# A minimal release lacks the tools referenced by a full home's configuration.
# Reject before materialization, configuration writes or activation; do not
# guess which user-edited entries can safely be removed.
if not extra:
    active_record = home/'runtime'/'current'/'.venv'/'share'/'ares-full-install.json'
    if active_record.exists() and json.loads(active_record.read_text()).get('inputs', {}).get('enhancements'):
        raise SystemExit('Cannot switch a full Ares home to --minimal. Rerun with full defaults, or use --home and --bin-dir with new directories for a minimal installation.')
source = Path(os.environ['ARES_INSTALL_SOURCE'])
revision = runtime._git_output(source, 'rev-parse', 'HEAD')
inputs = {'recipe_version':os.environ['ARES_INSTALL_RECIPE_VERSION'], 'ares_revision':revision, 'desktop':desktop, 'enhancements':extra, 'sdk_dependencies':sdk}
if extra:
    inputs['source_revisions'] = {name:runtime._git_output(home/'installer-sources'/name,'rev-parse','HEAD') for name in ['Libraries','agent-memory-kits','agent-graph-mcp']}
    inputs['rust_tools'] = runtime._run(['cargo', 'install', '--list', '--root', home/'enhancements'], capture=True).stdout.strip()
manifest = {'inputs':inputs, 'native_wheels':[{'name':p.name, 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in wheels] if extra else []}

class FullInstallRuntime(AresLocalRuntime):
    # This installation adapter adds build inputs before Ares seals/activates
    # the release. Ares owns source identity, activation, rollback and launch.
    def _sync_python_runtime(self, source):
        super()._sync_python_runtime(source)
        if desktop:
            self._run([str(ensure_uv()), 'sync', '--locked', '--extra', 'all', '--extra', 'voice', '--extra', 'wake', '--no-dev'], cwd=source, env=self._build_environment(source))
        # Published SDK fix for boolean tool sub-schemas in pre-2026 sessions
        # (python-sdk #3353/#3354). This explicit recipe override is recorded;
        # dependency versions already provided by Ares's lock remain intact.
        self._run([str(ensure_uv()), 'pip', 'install', '--no-deps', '--python', self._python_for(source), *sdk], cwd=home, env=self._build_environment(source))
        self._run([self._python_for(source), '-c', sdk_probe], cwd=home, env=self._agent_environment())
        if extra:
            self._run([str(ensure_uv()), 'pip', 'install', '--no-deps', '--python', self._python_for(source), *wheels], cwd=source, env=self._build_environment(source))
            # Couple Rust tools and kit helpers to the same immutable release
            # as Python. Active Ares processes never use the mutable build cache.
            bins = source/'.venv'/'bin'
            for name in tools:
                shutil.copy2(home/'enhancements'/'bin'/name, bins/name)
            kit = source/'.venv'/'share'/'agent-memory-kits'
            shutil.copytree(home/'installer-sources'/'agent-memory-kits', kit, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', 'target', '.venv'))
            self._run([self._python_for(source), '-c', probe], cwd=home, env=self._agent_environment())
        self._run([str(ensure_uv()), 'pip', 'check', '--python', self._python_for(source)], cwd=source, env=self._build_environment(source))
        record = source/'.venv'/'share'/'ares-full-install.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(manifest, indent=2)+'\n')

runtime = FullInstallRuntime()
source = Path(os.environ['ARES_INSTALL_SOURCE'])
final = runtime.paths.releases_dir / revision / 'source'
if final.exists():
    record = final/'.venv'/'share'/'ares-full-install.json'
    previous = json.loads(record.read_text()) if record.exists() else {}
    if previous.get('inputs') != inputs:
        raise SystemExit('This Ares revision exists with different installation inputs. Its immutable release was preserved. Use --home and --bin-dir with new directories, or install a newer Ares revision.')
    result = subprocess.run([runtime._python_for(final), '-c', sdk_probe + ('\n'+probe if extra else '')], cwd=home, env=runtime._agent_environment(), capture_output=True)
    if result.returncode:
        raise SystemExit('Existing immutable release failed its enhancement probe; refusing to repair it in place.')
    manifest = previous
# The release that will become `previous` after activation: if it was not
# produced by this installer it carries none of the bundled tooling, so a
# `ares rollback` to it would leave the enhanced configuration without its
# tools. Record that in the receipt instead of presenting a silent breakage.
previous_has_bundled_tools = True
try:
    current = runtime._release_from_link(runtime.paths.current_link, 'current')
    if current is not None:
        previous_has_bundled_tools = (current[1]/'.venv'/'share'/'ares-full-install.json').exists()
        if not previous_has_bundled_tools:
            print('[Ares] Note: the preserved previous release was not produced by this installer and lacks the bundled native tools; after this install, `ares rollback` to it leaves the enhanced configuration without its tooling. Roll forward again or rerun this installer to recover.')
except Exception:
    # Minimal runtimes or fixtures without pointer introspection: advisory only.
    pass
# Build and seal an INACTIVE release before changing an existing configuration.
with runtime.locked():
    runtime._materialize(str(source), revision, desktop=desktop)
receipt = {'schema':'ares-full-install/v1', 'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), **manifest['inputs'], 'provider_setup':'pending', 'phase':'built_pending_activation', 'native_wheels':manifest['native_wheels'], 'rollback_previous_has_bundled_tools': previous_has_bundled_tools}
directory = home/'install-receipts'
directory.mkdir(exist_ok=True, mode=0o700)
Path(os.environ['ARES_INSTALL_RECEIPT']).write_text(json.dumps(receipt, indent=2)+'\n')
PY

STEP=configuration
export ARES_INSTALL_BACKUP="$ARES_HOME/installer-backups/config-$install_epoch-$$"
CONFIG_PENDING=true
"$BOOTSTRAP_PYTHON" - <<'PY'
import hashlib, json, os, shutil
from pathlib import Path
from hermes_cli.config import load_config, save_config

home = Path(os.environ['ARES_HOME'])
source = Path(os.environ['ARES_INSTALL_SOURCE'])
if os.environ['ARES_INSTALL_EXTRAS'] == 'true':
    plugin = home/'plugins'/'semantic-memory-mcp'
    stack_plugin = home/'plugins'/'ares-full-stack'
    stack_files = {
        'plugin.yaml': 'name: ares-full-stack\nversion: "1.0.0"\ndescription: Scope installed tool paths to the current Ares runtime\n',
        '__init__.py': '''import os
from pathlib import Path
from hermes_constants import get_hermes_home

def register(ctx):
    home = Path(get_hermes_home())
    paths = [str(home/'runtime'/'current'/'.venv'/'bin')]
    os.environ['PATH'] = os.pathsep.join(paths + [os.environ.get('PATH', '')])
''',
    }
    # Follow no operator-owned file/link while rewriting managed configuration.
    for name in ('config.yaml', '.env', 'plugins', 'skills', 'skills/ares', 'skills/memory-kit', 'skills/.ares-install-manifest.json', 'installer-backups'):
        path = home/name
        if path.is_symlink():
            raise SystemExit(f'Installer-managed path is a symlink; preserved: {name}')
    if stack_plugin.is_symlink() or (stack_plugin.exists() and not stack_plugin.is_dir()):
        raise SystemExit('Existing ares-full-stack plugin is customized; preserved.')
    if stack_plugin.exists():
        actual = {str(path.relative_to(stack_plugin)): path for path in stack_plugin.rglob('*') if '__pycache__' not in path.parts and path.suffix != '.pyc'}
        if set(actual) != set(stack_files) or any(actual[name].is_symlink() or not actual[name].is_file() or actual[name].read_text() != text for name, text in stack_files.items()):
            raise SystemExit('Existing ares-full-stack plugin is customized; preserved.')
    kits = home/'runtime'/'current'/'.venv'/'share'/'agent-memory-kits'
    plugin_target = kits/'hermes'
    def snapshot(root):
        # Imported Python bytecode is generated, not a plugin customization.
        result = {}
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if '__pycache__' in relative.parts or path.suffix == '.pyc':
                continue
            result[str(relative)] = ('link', str(path.readlink())) if path.is_symlink() else ('dir',) if path.is_dir() else ('file', path.read_bytes())
        return result
    def skill_snapshot(root):
        result = {}
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if '__pycache__' in relative.parts or path.suffix == '.pyc':
                continue
            result[str(relative)] = ['link', str(path.readlink())] if path.is_symlink() else ['dir'] if path.is_dir() else ['file', hashlib.sha256(path.read_bytes()).hexdigest()]
        return result
    def save_env_path(name, value):
        """Persist a behavioral path in .env without the credential sanitizer.

        ``hermes_cli.config.save_env_value`` strips every non-ASCII character
        (it is written for API keys), which would corrupt the persisted paths
        on a home whose directory name contains non-ASCII text. These are
        filesystem paths, not credentials, so upsert them directly with the
        same quoting semantics the shared writer uses.
        """
        quoted = value
        if value == "" or any(ch in value for ch in "#\"'") or value != value.strip() or any(c.isspace() for c in value):
            quoted = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        env_path = home/'.env'
        existing_lines = env_path.read_text(encoding='utf-8-sig').splitlines() if env_path.exists() else []
        line = f'{name}={quoted}'
        out, replaced = [], False
        for raw in existing_lines:
            probe = raw.strip()
            if probe.startswith(f'{name}=') or probe.startswith(f'export {name}='):
                out.append(line)
                replaced = True
            else:
                out.append(raw)
        if not replaced:
            out.append(line)
        env_path.write_text('\n'.join(out).rstrip('\n') + '\n', encoding='utf-8')
        env_path.chmod(0o600)
    if plugin.is_symlink():
        if plugin.readlink() != plugin_target:
            raise SystemExit('Existing semantic-memory-mcp plugin link is customized; preserved. Reconcile it before rerunning.')
    elif plugin.exists():
        if not plugin.is_dir() or not plugin_target.is_dir() or snapshot(plugin) != snapshot(plugin_target):
            raise SystemExit('Existing semantic-memory-mcp plugin is customized or untracked; preserved. Reconcile it before rerunning.')
    backup = Path(os.environ['ARES_INSTALL_BACKUP'])
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = []
    for name in ('config.yaml', '.env'):
        path = home/name
        if path.exists():
            shutil.copy2(path, backup/name)
            (backup/name).chmod(0o600)
            existing.append(name)
    if plugin.is_symlink():
        (backup/'semantic-memory-mcp').symlink_to(plugin.readlink(), target_is_directory=True)
    elif plugin.exists():
        shutil.copytree(plugin, backup/'semantic-memory-mcp', symlinks=True)
    managed_paths = ['skills/ares', 'skills/memory-kit', 'skills/.ares-install-manifest.json', 'plugins/ares-full-stack']
    for name in managed_paths:
        path, saved = home/name, backup/'managed'/name
        if path.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, saved, symlinks=True)
            else:
                shutil.copy2(path, saved)
    current = home/'runtime'/'current'
    selected = str(current.readlink()) if current.is_symlink() else ('non-link' if current.exists() else None)
    (backup/'files.json').write_text(json.dumps({'home':str(home), 'existing':existing, 'plugin':True, 'managed_paths':managed_paths, 'current_link':selected}))
    config = load_config()
    config.setdefault('context', {})['engine'] = 'ri-context-governor'
    bins = home/'runtime'/'current'/'.venv'/'bin'
    kits_source = home/'installer-sources'/'agent-memory-kits'
    servers = {
        'semantic_memory': {'command': str(bins/'semantic-memory-mcp'), 'args': ['--memory-dir', str(home/'memory'), '--tool-profile', 'agent']},
        'claim_ledger': {'command': str(bins/'claim-ledger-mcp'), 'args': ['--ledger-dir', str(home/'claim-ledger')]},
        'agent_graph': {'command': str(bins/'agent-graph-mcp'), 'args': ['--socket', str(home/'agent-graph'/'run'/'mcp.sock')], 'enabled': False},
        'context_governor': {'command': str(home/'runtime'/'current'/'.venv'/'bin'/'python'), 'args': [str(kits/'hermes'/'scripts'/'context-governor-mcp.py')]},
    }
    existing = config.setdefault('mcp_servers', {})

    def legacy_installer_shape(name, candidate):
        # Exact shapes written by the superseded bootstrap installer, which a
        # home installed before this one legitimately carries; migrate those
        # while still rejecting genuinely customized entries.
        if not isinstance(candidate, dict) or set(candidate) != {'command', 'enabled', 'args'} or candidate.get('enabled') is not True:
            return False
        stem = os.path.basename(str(candidate.get('command', '')))
        if name == 'semantic_memory':
            return stem == 'semantic-memory-mcp' and candidate.get('args') == ['--memory-dir', str(home/'semantic-memory.db')]
        if name == 'claim_ledger':
            return stem == 'claim-ledger-mcp' and candidate.get('args') == ['--ledger-dir', str(home/'claim-ledger')]
        if name == 'agent_graph':
            return stem == 'agent-graph-mcp' and candidate.get('args') in (
                ['--socket', str(home/'agent-graph'/'run'/'mcp.sock')],
                ['--socket', str(Path.home()/'.local'/'share'/'agent-graph'/'run'/'mcp.sock')],
            )
        return False

    for name, entry in servers.items():
        if name in existing and existing[name] != entry:
            if legacy_installer_shape(name, existing[name]):
                print(f'[Ares] Migrated configuration from the superseded installer: {name}')
                existing[name] = entry
                continue
            raise SystemExit(f'Existing MCP configuration {name!r} differs. Backup retained; reconcile it before rerunning.')
        existing[name] = entry
    # The semantic-memory replacement is installer-enabled, so retire the
    # overlapping built-in memory toolset: never keep both memory surfaces
    # enabled at once (they pay for both schemas on every model request).
    agent = config.setdefault('agent', {})
    disabled = agent.setdefault('disabled_toolsets', [])
    if 'memory' not in disabled:
        print('[Ares] Disabled the built-in memory toolset for the semantic-memory replacement.')
        disabled.append('memory')
    if save_config(config) is False:
        raise SystemExit('Ares configuration save was rejected; installation is incomplete.')
    save_env_path('CONTEXT_GOVERNOR_BIN', str(bins/'context-governor'))
    save_env_path('CEA_GRAPH_BIN', str(bins/'cea-graph'))
    save_env_path('SEMANTIC_MEMORY_KIT_ROOT', str(kits))
    save_env_path('CONTEXT_GOVERNOR_STORE', str(home/'context-governor'/'receipts'))
    save_env_path('HERMES_RI_AGENT_GRAPH_DB', str(home/'agent-graph'/'agent-graph.db'))
    skills_manifest_path = home/'skills'/'.ares-install-manifest.json'
    skills_manifest = json.loads(skills_manifest_path.read_text()) if skills_manifest_path.exists() else {}
    refreshed_skills = []
    preserved_skills = []
    for root, prefix in [(source/'optional-skills', 'ares'), (kits_source/'hermes'/'skills', 'memory-kit')]:
        for skill in root.rglob('SKILL.md'):
            key = str(Path(prefix)/skill.parent.relative_to(root))
            dest = home/'skills'/key
            if dest.is_symlink() or any(parent.is_symlink() for parent in dest.parents if parent != home and home in parent.parents):
                preserved_skills.append(key)
                continue
            if not dest.exists():
                shutil.copytree(skill.parent, dest, symlinks=True)
                skills_manifest[key] = skill_snapshot(skill.parent)
                continue
            current = skill_snapshot(dest)
            if current == skill_snapshot(skill.parent):
                continue
            if skills_manifest.get(key) == current:
                # Unmodified since this installer wrote it: refresh to the new
                # source revision so updates deliver skill fixes.
                shutil.rmtree(dest)
                shutil.copytree(skill.parent, dest, symlinks=True)
                skills_manifest[key] = skill_snapshot(skill.parent)
                refreshed_skills.append(key)
            else:
                # Operator-modified (or pre-manifest) copy: preserved, and
                # reported so the divergence is visible.
                preserved_skills.append(key)
    if refreshed_skills:
        print('[Ares] Refreshed installer-owned skills: ' + ', '.join(sorted(refreshed_skills)))
    if preserved_skills:
        print('[Ares] Preserved locally-modified skills: ' + ', '.join(sorted(preserved_skills)))
    skills_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    skills_manifest_path.write_text(json.dumps(skills_manifest, indent=2, sort_keys=True))
    if not plugin.is_symlink():
        plugin.parent.mkdir(parents=True, exist_ok=True)
        if plugin.exists():
            shutil.rmtree(plugin)
        # current remains the runtime owner's activation/rollback pointer.
        plugin.symlink_to(plugin_target, target_is_directory=True)
    # Kit management helpers execute `hermes`. Scope their PATH to the
    # selected Ares runtime instead of selecting an ambient Hermes install.
    stack_plugin.mkdir(parents=True, exist_ok=True)
    for name, text in stack_files.items():
        (stack_plugin/name).write_text(text)

PY

STEP=activation
log "Handing activation to the Ares runtime owner."
# Ares owns activation, signed compaction-key provisioning and rollback.
args=(setup --source "$ARES_SOURCE" --seed-from "$ARES_HOME/no-import" --no-gateway)
[[ "$DESKTOP" == true ]] || args+=(--no-desktop)
"$BOOTSTRAP_PYTHON" -m ares_runtime.local_runtime "${args[@]}"
CONFIG_PENDING=false

RUNTIME_PYTHON="$ARES_HOME/runtime/current/.venv/bin/python"
agent_cli() { "$RUNTIME_PYTHON" -m hermes_cli.main "$@"; }
if [[ "$EXTRAS" == true ]]; then
  export PATH="$ARES_HOME/runtime/current/.venv/bin:$PATH"
  export CONTEXT_GOVERNOR_BIN="$ARES_HOME/runtime/current/.venv/bin/context-governor"
  export CONTEXT_GOVERNOR_STORE="$ARES_HOME/context-governor/receipts"
  export CEA_GRAPH_BIN="$ARES_HOME/runtime/current/.venv/bin/cea-graph"
  export SEMANTIC_MEMORY_KIT_ROOT="$ARES_HOME/runtime/current/.venv/share/agent-memory-kits"
  export HERMES_RI_AGENT_GRAPH_DB="$ARES_HOME/agent-graph/agent-graph.db"
  STEP=plugin
  agent_cli plugins enable semantic-memory-mcp --no-allow-tool-override
  agent_cli plugins enable ares-full-stack --no-allow-tool-override
  # Agent Graph's daemon has separate API-compatible/Codex-worker transports.
  # Do not borrow OAuth tokens or invent a provider endpoint/model/key for it.
  log "Agent Graph binaries are installed; remote graph execution needs its own compatible provider/daemon configuration."
fi

STEP=recursive-agent
if [[ "$RECURSIVE_AGENT" == true ]]; then
  plugin_dir="$ARES_HOME/plugins/recursive-agent-native"
  [[ ! -L "$ARES_HOME/plugins/recursive-agent-native.manifest" ]] || die "Recursive Agent manifest is a symlink; preserved."
  payload_files=(__init__.py client.py schema.py plugin.yaml pyproject.toml)
  [[ ! -L "$ARES_HOME/plugins" && ! -L "$plugin_dir" ]] || die "Recursive Agent payload path is a symlink; preserved."
  if [[ -e "$plugin_dir" ]]; then
    [[ -d "$plugin_dir" ]] || die "Recursive Agent payload path is not a directory; preserved."
    for file in "${payload_files[@]}"; do
      [[ -f "$plugin_dir/$file" && -s "$plugin_dir/$file" && ! -L "$plugin_dir/$file" ]] || die "Existing Recursive Agent payload is incomplete or symlinked; preserved for review: $plugin_dir"
    done
    export ARES_INSTALL_RECURSIVE_AGENT_STATUS=existing_complete
    log "Existing Recursive Agent payload is complete; preserved without overwriting operator changes."
  else
    if [[ -n "$RECURSIVE_AGENT_SOURCE" ]]; then
      src="$("$PYTHON" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$RECURSIVE_AGENT_SOURCE")"
    else
      src="$ARES_HOME/recursive-agent-src"
      checkout recursive-agent "$src"
    fi
    [[ -f "$src/scripts/install-hermes-plugin.sh" && ! -L "$src/scripts/install-hermes-plugin.sh" ]] || die "Recursive Agent plugin installer is missing or symlinked."
    # The upstream payload copier is not atomic. Stage its output in a private
    # same-filesystem home, then publish only a complete declared payload.
    payload_stage="$(mktemp -d "$ARES_HOME/.recursive-agent-stage.XXXXXXXX")"
    if HERMES_HOME="$payload_stage" bash "$src/scripts/install-hermes-plugin.sh"; then
      for file in "${payload_files[@]}"; do
        [[ -f "$payload_stage/plugins/recursive-agent-native/$file" && -s "$payload_stage/plugins/recursive-agent-native/$file" && ! -L "$payload_stage/plugins/recursive-agent-native/$file" ]] || die "Recursive Agent staged payload is incomplete; retained: $payload_stage"
      done
      mkdir -p -- "$ARES_HOME/plugins"
      [[ ! -e "$plugin_dir" && ! -L "$plugin_dir" ]] || die "Recursive Agent payload appeared during installation; both copies preserved."
      "$PYTHON" -c 'import os,sys; os.rename(sys.argv[1], sys.argv[2])' "$payload_stage/plugins/recursive-agent-native" "$plugin_dir"
      # Canonical filenames, not a sed expression derived from a user path.
      printf '%s\n' "${payload_files[@]}" | LC_ALL=C sort > "$payload_stage/manifest"
      mv -- "$payload_stage/manifest" "$ARES_HOME/plugins/recursive-agent-native.manifest"
      rm -rf -- "$payload_stage"
      export ARES_INSTALL_RECURSIVE_AGENT_STATUS=installed
      log "Recursive Agent plugin payload installed; its daemon remains operator-managed."
    else
      die "Recursive Agent plugin payload installation failed; staged files retained: $payload_stage"
    fi
  fi
else
  export ARES_INSTALL_RECURSIVE_AGENT_STATUS=disabled
  log "Recursive Agent plugin payload skipped (--no-recursive-agent)."
fi

STEP=provider
if [[ "$SETUP" == true ]]; then
  log "Choose your AI provider and model. Ares will prompt locally for its API key or supported OAuth sign-in."
  # Provider discovery must not borrow ambient EC2/ECS instance credentials.
  # Explicit API keys, profiles and OAuth remain owned by the local wizard.
  env -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI -u AWS_CONTAINER_CREDENTIALS_FULL_URI \
      -u AWS_CONTAINER_AUTHORIZATION_TOKEN -u AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE \
      AWS_EC2_METADATA_DISABLED=true "$RUNTIME_PYTHON" -m hermes_cli.main model </dev/tty
else
  log "Provider sign-in deferred (--skip-setup)."
fi

STEP=gateway
export ARES_INSTALL_GATEWAY_STARTED=false
if [[ "$GATEWAY" == true && "$OS" == Linux ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  args=(setup --source "$ARES_SOURCE" --seed-from "$ARES_HOME/no-import")
  [[ "$DESKTOP" == true ]] || args+=(--no-desktop)
  # --skip-setup defers sign-in, not a failed selected runtime/service action.
  "$RUNTIME_PYTHON" -m ares_runtime.local_runtime "${args[@]}"
  export ARES_INSTALL_GATEWAY_STARTED=true
else
  log "Gateway not installed or started (disabled or no Linux user-systemd session). Use ares gateway foreground, or rerun this installer when systemd is available."
fi

STEP=verification
log "Verifying the selected runtime and required components."
export ARES_INSTALL_DOCTOR_LOG="$TEMP_DIR/doctor.log"
if "$ARES_BIN_DIR/ares" doctor >"$ARES_INSTALL_DOCTOR_LOG" 2>&1; then
  export ARES_INSTALL_DOCTOR_STATUS=0
else
  export ARES_INSTALL_DOCTOR_STATUS=$?
fi
"$RUNTIME_PYTHON" - <<'PY'
import json, os, re, shlex
from pathlib import Path

# Upstream doctor treats an inactive gateway as a failure even on hosts with
# no systemd or an explicit --no-gateway. Keep that omission visible and require
# every runtime/MCP check; only require gateway health when we started it.
output = Path(os.environ['ARES_INSTALL_DOCTOR_LOG']).read_text()
checks = re.findall(r'^(PASS|FAIL) ([^:\n]+): (.*)$', output, re.MULTILINE)
required = {'active runtime','stable Python','selected release tree','Ares runtime imports','SQLite runtime','Context Governor strict probe','MCP readiness','runtime process coherence','Ares gateway'}
labels = [label for _, label, _ in checks]
if not required.issubset(labels) or len(labels) != len(set(labels)):
    print(output)
    raise SystemExit('Ares health checks returned incomplete evidence; installation is incomplete.')
health = []
for result, label, detail in checks:
    passed = result == 'PASS'
    deferred = label == 'Ares gateway' and os.environ['ARES_INSTALL_GATEWAY_STARTED'] != 'true'
    state = 'deferred' if deferred else ('passed' if passed else 'failed')
    health.append({'check':label, 'state':state, 'detail':detail})
    print(f"[Ares] {state.upper()} {label}: {detail}")
if any(item['state'] == 'failed' for item in health) or int(os.environ['ARES_INSTALL_DOCTOR_STATUS']) not in (0,1):
    print(output)
    raise SystemExit('Required Ares runtime health checks failed; installation is incomplete.')
if int(os.environ['ARES_INSTALL_DOCTOR_STATUS']) != 0 and not any(result == 'FAIL' and label == 'Ares gateway' for result,label,_ in checks):
    print(output)
    raise SystemExit('Ares doctor failed without an explicitly deferred gateway check.')
home = Path(os.environ['ARES_HOME'])
receipt_path = Path(os.environ['ARES_INSTALL_RECEIPT'])
receipt = json.loads(receipt_path.read_text())
revision = receipt.get('ares_revision')
if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
    raise SystemExit('Install attempt has no valid source revision; completion refused.')
current = home/'runtime'/'current'
expected = home/'runtime'/'releases'/revision/'source'
if not current.is_symlink() or current.resolve(strict=True) != expected.resolve(strict=True):
    raise SystemExit('Selected runtime changed after this install attempt; completion refused.')
bins = [str(Path(os.environ['ARES_BIN_DIR'])), str(home/'runtime'/'current'/'.venv'/'bin')]
line = 'export PATH='+shlex.quote(':'.join(bins))+':"$PATH" # Ares full installer\n'
for name in (['.profile', '.bashrc', '.zshrc'] if os.environ['ARES_INSTALL_MODIFY_PATH'] == 'true' else []):
    file = Path.home()/name
    content = file.read_text() if file.exists() else ''
    if line.strip() not in content:
        with file.open('a') as handle:
            handle.write('\n'+line)
receipt['observed_selected_revision'] = revision
receipt['phase'] = 'complete'
receipt['runtime_checks'] = 'passed'
receipt['health_checks'] = health
receipt['gateway_started'] = os.environ['ARES_INSTALL_GATEWAY_STARTED'] == 'true'
receipt['recursive_agent_payload'] = os.environ['ARES_INSTALL_RECURSIVE_AGENT_STATUS']
receipt['provider_setup'] = 'wizard_returned' if os.environ.get('ARES_INSTALL_SETUP') == 'true' else 'deferred_or_not_recorded'
# Preserve the completed attempt before atomically publishing the latest index.
# No success receipt is replaced while an earlier installation phase can fail.
import tempfile
payload = json.dumps(receipt, indent=2)+'\n'
def publish(path):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.ares-receipt-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
publish(receipt_path)
publish(receipt_path.parent/'latest.json')
PY
log "Ares installed and runtime checks passed. Open a new terminal, then run: ares"
[[ "$DESKTOP" == false ]] || log "Desktop: ares desktop"
log "Change provider later: HERMES_HOME=\"$ARES_HOME\" \"$RUNTIME_PYTHON\" -m hermes_cli.main model"
log "Installation record: $ARES_HOME/install-receipts/latest.json"
log "Update the full distribution by rerunning this installer. Review the recorded previous-release tooling boundary before using: ares rollback"
