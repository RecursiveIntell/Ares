#!/usr/bin/env bash
# Hermetic contract battery for the canonical Ares installer (install.sh).
# No network, no sudo, no installs, no services: exercises argument
# validation, the --plan preflight (including its no-writes guarantee), and
# the pre-provisioning TTY guard in isolated temporary homes. The full
# behavioral regression suite for the same bytes lives in recursiveintell-web
# (npm run test:installer).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$ROOT/install.sh"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS %s\n' "$*"; }

bash -n "$INSTALLER" || fail "syntax"
pass syntax

help_out="$(bash "$INSTALLER" --help)"
for flag in "--home PATH" "--bin-dir PATH" "--branch NAME" "--no-desktop" \
            "--no-gateway" "--minimal" "--skip-setup" "--no-path" \
            "--no-recursive-agent" "--with-recursive-agent-source PATH" "--plan"; do
  grep -qF -- "$flag" <<<"$help_out" || fail "help missing: $flag"
done
pass help-contract

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
plan_home="$tmp/home"
mkdir -p "$plan_home"

out="$(HOME="$plan_home" ARES_HOME="$plan_home/ares" bash "$INSTALLER" --plan)"
grep -q 'Plan: Ares branch=main; home=.*; Desktop=true; enhancements=true; gateway=true; provider wizard=true; recursive-agent=true' <<<"$out" \
  || fail "default plan line"
[[ -z "$(ls -A "$plan_home")" ]] || fail "--plan wrote files: $(ls -A "$plan_home")"
pass plan-default-no-writes

out="$(HOME="$plan_home" ARES_HOME="$plan_home/ares" bash "$INSTALLER" --plan --minimal --no-desktop --no-gateway --skip-setup --no-recursive-agent --branch candidate)"
grep -q 'branch=candidate; home=.*; Desktop=false; enhancements=false; gateway=false; provider wizard=false; recursive-agent=false' <<<"$out" \
  || fail "opt-out plan line"
pass plan-opt-outs

if HOME="$plan_home" bash "$INSTALLER" --missing >/dev/null 2>"$tmp/err"; then
  fail "unknown option accepted"
fi
grep -q 'Unknown option' "$tmp/err" || fail "unknown option message"
pass unknown-option

for probe in home branch bin-dir source; do
  case "$probe" in
    home) argv=(--home) ;;
    branch) argv=(--branch) ;;
    bin-dir) argv=(--bin-dir "") ;;
    source) argv=(--with-recursive-agent-source) ;;
  esac
  if HOME="$plan_home" bash "$INSTALLER" "${argv[@]}" --plan >/dev/null 2>"$tmp/err"; then
    fail "missing value accepted: $probe"
  fi
  grep -q 'needs a value' "$tmp/err" || fail "missing value message: $probe"
done
pass missing-values

if HOME="$plan_home" bash "$INSTALLER" --no-recursive-agent --with-recursive-agent-source /nonexistent --plan >/dev/null 2>"$tmp/err"; then
  fail "conflicting options accepted"
fi
grep -q 'conflicting options' "$tmp/err" || fail "conflict message"
pass conflict-options

python3 - "$INSTALLER" "$plan_home" <<'PY'
import os
import subprocess
import sys

installer, home = sys.argv[1], sys.argv[2]
# A fresh session has no controlling terminal, so the provider-wizard guard
# must stop before any prerequisite provisioning or system interaction.
result = subprocess.run(
    ["bash", installer],
    stdin=subprocess.DEVNULL,
    capture_output=True,
    text=True,
    timeout=30,
    start_new_session=True,
    env={**os.environ, "HOME": home, "ARES_HOME": home + "/ares"},
)
assert result.returncode != 0, ("tty guard not triggered", result.stdout, result.stderr)
assert "needs a terminal" in result.stderr, result.stderr
print("PASS tty-guard")
PY

pass harness
