#!/usr/bin/env bash
# Shared probe helpers for the installer/updater E2E surface.
#
# Sourced by tests/install/install-update-e2e.sh (the real E2E harness) and by
# tests/install/test_harness_flag_probe.sh (the fast regression test), so the
# probes the fast test exercises are byte-identical to the harness's — the test
# never parses source text (root AGENTS.md prohibits source-reading tests).
#
# The caller provides:
#   in_sandbox()          run a command inside the (possibly stubbed) sandbox
#   git, UPSTREAM_URL     used by installer_supports' blob fetch (the fast test
#                         stubs `git` in PATH and exports UPSTREAM_URL)

# Does the INSTALLED hermes accept FLAG on `hermes update`?
#
# Asked of the installed binary rather than parsed out of a release's source:
# the update subcommand has lived in main.py, subcommands/update.py, and
# update_cmd.py across the releases we sample, so any static parse is a guess
# that silently rots. `hermes update --help` is the same surface a user meets,
# and argparse prints every option it accepts.
update_supports() {
  local flag="$1"
  local help_text=""
  help_text="$(in_sandbox "hermes update --help 2>&1")"
  # Consume the full output, THEN grep a here-string: `grep -q` exits after
  # the first match, so a producer fed through a pipe (printf/in_sandbox)
  # takes SIGPIPE under `set -o pipefail` on large surfaces (E-47) and the
  # probe reports a present flag as absent.
  grep -qF -- "$flag" <<<"$help_text"
}

# Does the installer at REF accept FLAG? Read it out of that ref's own
# install.sh rather than assuming this checkout's flag set: the point of the
# matrix is to install releases from months back, whose installers predate
# options we take for granted. (Unlike the updater, the installer runs before
# anything is installed, so there is no --help to ask yet.)
#
# The ref may not be local -- the sandbox does its own fetching -- so fall back
# to fetching just that blob. Unresolvable means "flag absent", which costs a
# more conservative invocation, never a wrong one.
installer_supports() {
  local ref="$1"
  local flag="$2"
  local script=""
  script="$(git show "$ref:scripts/install.sh" 2>/dev/null)" || {
    git fetch -q --depth 1 "$UPSTREAM_URL" "$ref" 2>/dev/null || return 1
    script="$(git show FETCH_HEAD:scripts/install.sh 2>/dev/null)" || return 1
  }
  # Consume the full blob, THEN grep a here-string: `grep -q` exits after the
  # first match, so printf-as-producer takes SIGPIPE under pipefail on the
  # ~200 KB install script (E-47) and a present flag reads as absent.
  grep -qF -- "$flag" <<<"$script"
}
