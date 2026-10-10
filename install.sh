#!/usr/bin/env bash
# Ares full installer — the canonical installation entrypoint for the
# RecursiveIntell/Ares distribution. `recursiveintell-web/public/ares/install.sh`
# is kept as a byte-identical mirror of this file; update both together.
# Provider secrets are handled by Ares's local model wizard, never by the
# installer or command-line arguments.
set -euo pipefail
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

RECURSIVE_AGENT_REPO="https://github.com/RecursiveIntell/recursive-agent.git"

log() { printf '[Ares] %s\n' "$*"; }
die() { printf '[Ares] %s\n' "$*" >&2; exit 1; }
restore_configuration() {
  "$BOOTSTRAP_PYTHON" - "$ARES_INSTALL_BACKUP" <<'PY'
import json, shutil, sys
from pathlib import Path
backup = Path(sys.argv[1])
manifest = backup/'files.json'
if manifest.exists():
    record = json.loads(manifest.read_text())
    home = Path(record['home'])
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
    print('[Ares] Restored configuration after the failed activation.')
PY
}
on_error() {
  local status="${1:-$?}"
  trap - ERR INT TERM
  if [[ "$CONFIG_PENDING" == true ]]; then
    restore_configuration || printf '[Ares] Configuration recovery failed; retained backup: %s\n' "$ARES_INSTALL_BACKUP" >&2
  fi
  printf '[Ares] Installation stopped during %s (exit %s). No complete-install claim was made.\n' "$STEP" "$status" >&2
  exit "$status"
}
trap on_error ERR
trap 'on_error 130' INT
trap 'on_error 143' TERM

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
  --skip-setup      Leave provider sign-in for later (unattended install)
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
System package installation may request sudo. Source/native builds and Desktop
downloads can take time. Failed required components stop installation.
No OpenAI API key is required unless you choose an API-key provider that needs it.
OAuth availability and account eligibility depend on the selected provider.
To update the full distribution, rerun this installer. `ares update` alone uses
the upstream runtime recipe and does not rebuild these extra native wheels.
EOF
}

value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
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

if [[ "$PLAN" == true ]]; then
  help
  log "Plan: Ares branch=$BRANCH; home=$ARES_HOME; Desktop=$DESKTOP; enhancements=$EXTRAS; gateway=$GATEWAY; provider wizard=$SETUP; recursive-agent=$RECURSIVE_AGENT"
  exit 0
fi
if [[ "$SETUP" == true ]] && ! ( : </dev/tty ) 2>/dev/null; then
  die "Provider selection needs a terminal. Run interactively, or use --skip-setup and configure locally later."
fi

STEP=prerequisites
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
OS="$(uname -s)"
[[ "$OS" == Linux || "$OS" == Darwin ]] || die "Use Linux, macOS, or WSL2 (see --help)."
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
    curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$TEMP_DIR/homebrew-install.sh"
    NONINTERACTIVE=1 /bin/bash "$TEMP_DIR/homebrew-install.sh"
    for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [[ ! -x "$brew_bin" ]] || export PATH="$(dirname "$brew_bin"):$PATH"
    done
  fi
  packages=(pkgconf openssl@3)
  [[ "$DESKTOP" == false ]] || packages+=(portaudio ffmpeg)
  brew install "${packages[@]}"
  export PKG_CONFIG_PATH="$(brew --prefix openssl@3)/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v git >/dev/null 2>&1 || die "git is required."

# Download each bootstrap completely before executing it; retain normal stdin.
# Ares's locked dependencies require its own [tool.uv] resolver settings.
unset UV_NO_CONFIG
# Provision owned, current tools even when the shell has an older installation.
case "$ARES_HOME" in '~') ARES_HOME="$HOME" ;; '~/'*) ARES_HOME="$HOME/${ARES_HOME:2}" ;; esac
[[ "$ARES_HOME" == /* ]] || ARES_HOME="$PWD/$ARES_HOME"
export UV_INSTALL_DIR="$ARES_HOME/bin" UV_UNMANAGED_INSTALL="$ARES_HOME/bin"
export CARGO_HOME="$ARES_HOME/toolchains/cargo"
export RUSTUP_HOME="$ARES_HOME/toolchains/rustup"
mkdir -p "$CARGO_HOME" "$RUSTUP_HOME"
export CARGO_HOME="$(cd "$CARGO_HOME" && pwd -P)"
export RUSTUP_HOME="$(cd "$RUSTUP_HOME" && pwd -P)"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT_ENVIRONMENT
export PATH="$UV_INSTALL_DIR:$ARES_BIN_DIR:$HOME/.local/bin:$CARGO_HOME/bin:$HOME/.cargo/bin:$PATH"
curl -fsSL https://astral.sh/uv/install.sh -o "$TEMP_DIR/uv-install.sh"
UV_NO_MODIFY_PATH=1 sh "$TEMP_DIR/uv-install.sh"
command -v uv >/dev/null 2>&1 || die "uv provisioning failed."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
if [[ "$EXTRAS" == true ]]; then
  if [[ ! -x "$CARGO_HOME/bin/rustup" ]]; then
    curl -fsSL https://sh.rustup.rs -o "$TEMP_DIR/rustup.sh"
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
SOURCE_ROOT="$ARES_HOME/installer-sources"
ENHANCEMENTS="$ARES_HOME/enhancements"
WHEELS="$ENHANCEMENTS/wheels"
export PATH="$ENHANCEMENTS/bin:$ARES_BIN_DIR:$HOME/.cargo/bin:$PATH"
mkdir -p "$SOURCE_ROOT" "$WHEELS"
SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd -P)"

checkout() {
  local repo="$1" directory="$2" branch="${3:-main}" origin
  if [[ -e "$directory" ]]; then
    [[ -d "$directory/.git" ]] || die "Existing path is not an installer-owned checkout: $directory"
    origin="$(git -C "$directory" remote get-url origin)"
    [[ "$origin" == "https://github.com/RecursiveIntell/$repo.git" ]] || die "Unexpected repository origin at $directory"
    [[ -z "$(git -C "$directory" status --porcelain)" ]] || die "Refusing to overwrite local changes at $directory"
    git -C "$directory" fetch origin "$branch"
    if git -C "$directory" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$directory" checkout "$branch"
      git -C "$directory" merge --ff-only FETCH_HEAD
    else
      git -C "$directory" checkout -b "$branch" FETCH_HEAD
    fi
  else
    git clone --depth 1 --branch "$branch" "https://github.com/RecursiveIntell/$repo.git" "$directory"
  fi
}

STEP=source
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
export ARES_INSTALL_RECIPE_VERSION=3
# Derive identity from the physical data home, not a shared per-user name.
# Retain the old unit only for its proven owner, avoiding a duplicate gateway
# on upgrades. Other homes always receive their own distinct unit.
export ARES_GATEWAY_UNIT_PATH="$("$BOOTSTRAP_PYTHON" - <<'PY'
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

STEP=managed-runtime
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
probe += f'; import sys; from pathlib import Path; assert all((Path(sys.executable).parent/name).is_file() for name in {tools!r}), "missing bundled Rust tools"'
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
            self._run([str(ensure_uv()), 'pip', 'install', '--python', self._python_for(source), *wheels], cwd=source, env=self._build_environment(source))
            # Couple Rust tools and kit helpers to the same immutable release
            # as Python. Active Ares processes never use the mutable build cache.
            bins = source/'.venv'/'bin'
            for name in tools:
                shutil.copy2(home/'enhancements'/'bin'/name, bins/name)
            kit = source/'.venv'/'share'/'agent-memory-kits'
            shutil.copytree(home/'installer-sources'/'agent-memory-kits', kit, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', 'target', '.venv'))
            self._run([self._python_for(source), '-c', probe], cwd=home, env=self._agent_environment())
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
# Build and seal an INACTIVE release before changing an existing configuration.
with runtime.locked():
    runtime._materialize(str(source), revision, desktop=desktop)
receipt = {'schema':'ares-full-install/v1', 'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), **manifest['inputs'], 'provider_setup':'pending', 'phase':'built_pending_activation', 'native_wheels':manifest['native_wheels']}
directory = home/'install-receipts'
directory.mkdir(exist_ok=True, mode=0o700)
(directory/'latest.json').write_text(json.dumps(receipt, indent=2)+'\n')
PY

STEP=configuration
export ARES_INSTALL_BACKUP="$ARES_HOME/installer-backups/config-$(date +%s)-$$"
CONFIG_PENDING=true
"$BOOTSTRAP_PYTHON" - <<'PY'
import json, os, shutil
from pathlib import Path
from hermes_cli.config import load_config, save_config, save_env_value

home = Path(os.environ['ARES_HOME'])
source = Path(os.environ['ARES_INSTALL_SOURCE'])
if os.environ['ARES_INSTALL_EXTRAS'] == 'true':
    plugin = home/'plugins'/'semantic-memory-mcp'
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
    (backup/'files.json').write_text(json.dumps({'home':str(home), 'existing':existing, 'plugin':True}))
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
    for name, entry in servers.items():
        if name in existing and existing[name] != entry:
            raise SystemExit(f'Existing MCP configuration {name!r} differs. Backup retained; reconcile it before rerunning.')
        existing[name] = entry
    save_config(config)
    save_env_value('CONTEXT_GOVERNOR_BIN', str(bins/'context-governor'))
    save_env_value('CEA_GRAPH_BIN', str(bins/'cea-graph'))
    save_env_value('SEMANTIC_MEMORY_KIT_ROOT', str(kits))
    save_env_value('CONTEXT_GOVERNOR_STORE', str(home/'context-governor'/'receipts'))
    save_env_value('HERMES_RI_AGENT_GRAPH_DB', str(home/'agent-graph'/'agent-graph.db'))
    for root, prefix in [(source/'optional-skills', 'ares'), (kits_source/'hermes'/'skills', 'memory-kit')]:
        for skill in root.rglob('SKILL.md'):
            dest = home/'skills'/prefix/skill.parent.relative_to(root)
            if not dest.exists():
                shutil.copytree(skill.parent, dest)
    if not plugin.is_symlink():
        plugin.parent.mkdir(parents=True, exist_ok=True)
        if plugin.exists():
            shutil.rmtree(plugin)
        # current remains the runtime owner's activation/rollback pointer.
        plugin.symlink_to(plugin_target, target_is_directory=True)
    # Kit management helpers execute `hermes`. Scope their PATH to the
    # selected Ares runtime instead of selecting an ambient Hermes install.
    stack_plugin = home/'plugins'/'ares-full-stack'
    stack_plugin.mkdir(parents=True, exist_ok=True)
    (stack_plugin/'plugin.yaml').write_text('name: ares-full-stack\nversion: "1.0.0"\ndescription: Scope installed tool paths to the current Ares runtime\n')
    (stack_plugin/'__init__.py').write_text('''import os
from pathlib import Path
from hermes_constants import get_hermes_home

def register(ctx):
    home = Path(get_hermes_home())
    paths = [str(home/'runtime'/'current'/'.venv'/'bin')]
    os.environ['PATH'] = os.pathsep.join(paths + [os.environ.get('PATH', '')])
''')
PY

STEP=activation
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
  if [[ -e "$plugin_dir" ]]; then
    log "Recursive Agent plugin payload already present; keeping it (remove $plugin_dir to reinstall)."
  else
    src=""
    if [[ -n "$RECURSIVE_AGENT_SOURCE" ]]; then
      src="$("$PYTHON" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$RECURSIVE_AGENT_SOURCE")"
      [[ -x "$src/scripts/install-hermes-plugin.sh" ]] || die "Recursive Agent plugin installer not found or not executable: $src/scripts/install-hermes-plugin.sh"
    else
      src="$ARES_HOME/recursive-agent-src"
      if [[ -d "$src/.git" ]]; then
        if [[ -n "$(git -C "$src" status --porcelain)" ]]; then
          log "recursive-agent checkout at $src has local changes; skipping the plugin payload install."
          src=""
        else
          git -C "$src" pull --ff-only >/dev/null 2>&1 || log "Could not fast-forward $src; using the current revision."
        fi
      elif [[ -e "$src" ]]; then
        log "$src exists but is not a Git checkout; skipping the plugin payload install."
        src=""
      else
        log "Cloning RecursiveIntell/recursive-agent for the plugin payload..."
        mkdir -p "$(dirname "$src")"
        if ! git clone --depth 1 "$RECURSIVE_AGENT_REPO" "$src" >/dev/null 2>&1; then
          log "recursive-agent clone failed; skipping the plugin payload install."
          src=""
        fi
      fi
    fi
    if [[ -n "$src" ]]; then
      if HERMES_HOME="$ARES_HOME" bash "$src/scripts/install-hermes-plugin.sh"; then
        log "Recursive Agent plugin payload installed from $src; the daemon remains an explicit operator-managed prerequisite."
      else
        log "Recursive Agent plugin payload install failed; rerun later: HERMES_HOME=\"$ARES_HOME\" bash \"$src/scripts/install-hermes-plugin.sh\""
      fi
    fi
  fi
else
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
if [[ "$GATEWAY" == true && "$SETUP" == true && "$OS" == Linux ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  args=(setup --source "$ARES_SOURCE" --seed-from "$ARES_HOME/no-import")
  [[ "$DESKTOP" == true ]] || args+=(--no-desktop)
  "$RUNTIME_PYTHON" -m ares_runtime.local_runtime "${args[@]}"
  export ARES_INSTALL_GATEWAY_STARTED=true
else
  log "Gateway not started (disabled, sign-in deferred, or no Linux user-systemd session). Use ares gateway foreground when needed."
fi

STEP=verification
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
bins = [str(Path(os.environ['ARES_BIN_DIR'])), str(home/'runtime'/'current'/'.venv'/'bin')]
line = 'export PATH='+shlex.quote(':'.join(bins))+':"$PATH" # Ares full installer\n'
for name in (['.profile', '.bashrc', '.zshrc'] if os.environ['ARES_INSTALL_MODIFY_PATH'] == 'true' else []):
    file = Path.home()/name
    content = file.read_text() if file.exists() else ''
    if line.strip() not in content:
        with file.open('a') as handle:
            handle.write('\n'+line)
receipt_path = home/'install-receipts'/'latest.json'
receipt = json.loads(receipt_path.read_text())
receipt['phase'] = 'complete'
receipt['runtime_checks'] = 'passed'
receipt['health_checks'] = health
receipt['gateway_started'] = os.environ['ARES_INSTALL_GATEWAY_STARTED'] == 'true'
receipt['provider_setup'] = 'wizard_returned' if os.environ.get('ARES_INSTALL_SETUP') == 'true' else 'deferred_or_not_recorded'
receipt_path.write_text(json.dumps(receipt, indent=2)+'\n')
(receipt_path.parent/f'install-{__import__("time").time_ns()}.json').write_text(json.dumps(receipt, indent=2)+'\n')
PY
log "Ares installed and runtime checks passed. Open a new terminal, then run: ares"
[[ "$DESKTOP" == false ]] || log "Desktop: ares desktop"
log "Change provider later: HERMES_HOME=\"$ARES_HOME\" \"$RUNTIME_PYTHON\" -m hermes_cli.main model"
log "Installation record: $ARES_HOME/install-receipts/latest.json"
log "Update the full distribution by rerunning this installer; rollback an existing runtime with: ares rollback"
