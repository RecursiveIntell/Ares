#!/usr/bin/env bash
# Task 1.2 (PLAN-FINAL): the E2E harness flag probes must detect installer and
# updater flags correctly for LARGE scripts (E-47).
#
# RED: installer_supports() on a 200 KB scripts/install.sh whose first line
# contains --skip-browser fails under `set -euo pipefail`: grep -qF exits on
# the first match, printf gets SIGPIPE, pipefail reports 141, and the harness
# wrongly concludes "flag absent" → passes wrong flags to older installers.
# GREEN: the probe consumes the script without a SIGPIPE-prone producer.
#
# This harness extracts the probe functions VERBATIM from
# tests/install/install-update-e2e.sh, so any fix there is exercised here.
# External effects (git show, in_sandbox) are stubbed with fakes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HARNESS="$REPO_ROOT/tests/install/install-update-e2e.sh"
[ -f "$HARNESS" ] || { echo "harness not found: $HARNESS" >&2; exit 2; }

extract_fn() {
    awk -v fn="$1" '
        $0 ~ "^"fn"\\(\\) \\{" {p = 1}
        p {print}
        p && $0 == "}" {exit}
    ' "$HARNESS"
}

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

# 200 KB fake installer whose FIRST line contains the flag (worst case for
# SIGPIPE: the match is found while most of the payload is still unwritten).
{
    printf '%s\n' '#!/bin/sh --skip-browser'
    head -c 204800 /dev/zero | tr '\0' 'x'
    printf '\n'
} > "$work/fake-install.sh"

# 200 KB fake `hermes update --help` output containing --yes once.
{
    printf '%s\n' 'usage: hermes update [--yes] [options]'
    head -c 204800 /dev/zero | tr '\0' 'h'
    printf '\n'
} > "$work/fake-help.txt"

# Fake update-support probe input: stub in_sandbox to emit the fake help.
in_sandbox() { cat "$FAKE_HELP"; }

# Fake git: installer_supports fetches scripts/install.sh from a ref.
mkdir -p "$work/bin"
cat > "$work/bin/git" <<GIT
#!/bin/sh
case "\$1 \$2" in
  "show "*) cat "\$FAKE_INSTALL_SCRIPT"; exit 0 ;;
esac
exit 1
GIT
chmod +x "$work/bin/git"
export FAKE_INSTALL_SCRIPT="$work/fake-install.sh"
export FAKE_HELP="$work/fake-help.txt"
export PATH="$work/bin:$PATH"

update_supports_fn="$(extract_fn update_supports)"
installer_supports_fn="$(extract_fn installer_supports)"
eval "$update_supports_fn"
eval "$installer_supports_fn"

expect_ok() {  # probe must succeed
    local rc=0
    ( set -euo pipefail; "$@" ) 2>"$work/err.txt" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "FAIL: expected success: $* (rc=$rc)" >&2
        sed 's/^/    /' "$work/err.txt" >&2
        return 1
    fi
    return 0
}

expect_absent() {  # absent flag must be reported absent (clean rc=1, not SIGPIPE 141)
    local rc=0
    ( set -euo pipefail; "$@" ) 2>"$work/err.txt" || rc=$?
    if [ "$rc" -ne 1 ]; then
        echo "FAIL: expected clean rc=1 for absent flag: $* (rc=$rc)" >&2
        sed 's/^/    /' "$work/err.txt" >&2
        return 1
    fi
    return 0
}

failures=0

# installer probe over a 200 KB script (RED: SIGPIPE kills printf)
expect_ok installer_supports refs/heads/main --skip-browser || failures=$((failures + 1))
expect_absent installer_supports refs/heads/main --definitely-not-a-flag || failures=$((failures + 1))

# updater probe over a 200 KB help surface
expect_ok update_supports --yes || failures=$((failures + 1))
expect_absent update_supports --definitely-not-a-flag || failures=$((failures + 1))

if [ "$failures" -ne 0 ]; then
    echo "harness flag probe: $failures case(s) failed" >&2
    exit 1
fi
echo "harness flag probe: all cases pass"
