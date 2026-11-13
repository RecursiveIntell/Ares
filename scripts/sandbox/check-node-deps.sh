#!/usr/bin/env bash
# Read-only health gate for the workspaces required by the CLI/TUI/web update.
# npm ls checks missing and invalid dependency trees without installing,
# running lifecycle scripts, or contacting the registry. Desktop is optional.
# Raw npm output can contain registry URLs and resolved specs, so failures are
# reported as named lines only:  <workspace>:<package>:<state>  (Task 1.3).
set -euo pipefail

[ "$#" -eq 1 ] || { echo 'error: expected the installation directory' >&2; exit 2; }
cd -- "$1"
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo 'error: Node.js or npm is unavailable for dependency health check' >&2
  exit 1
fi

summary="$(mktemp)"
trap 'rm -f -- "$summary"' EXIT
status=0
npm ls --offline --all --include=dev --workspace ui-tui --workspace web \
  --include-workspace-root --json >"$summary" 2>/dev/null || status=$?

# Emitter: read the npm ls JSON summary and the root manifest, print
# "<workspace>:<package>:<state>" lines for every missing/invalid dependency.
# Never prints versions, specs, or registry URLs.
emit_problem_lines() {
  node - "$summary" "$PWD/package.json" <<'NODE'
const fs = require('node:fs');
try {
  const summary = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const problems = [];
  // Nested walk below a workspace entry; label stays the workspace name.
  const walk = (label, deps) => {
    for (const [name, info] of Object.entries(deps || {})) {
      if (!record(info)) continue;
      if (info.missing) problems.push(`${label}:${name}:missing`);
      else if (info.invalid || info.invalidVersion) problems.push(`${label}:${name}:invalid`);
      walk(label, info.dependencies);
    }
  };
  if (record(summary) && record(summary.dependencies)) {
    // Top-level entries are the workspace trees AND the root project. Each can
    // itself be missing/invalid (npm ls reports the root's installed-version
    // drift as an invalidVersion flag on its own entry) — the child walk never
    // sees that flag, so check it here before descending.
    for (const [wsName, tree] of Object.entries(summary.dependencies)) {
      if (!record(tree)) continue;
      if (tree.missing) problems.push(`${wsName}:${wsName}:missing`);
      else if (tree.invalid || tree.invalidVersion) problems.push(`${wsName}:${wsName}:invalid`);
      walk(wsName, tree.dependencies);
    }
  }
  // Root-manifest required devDependencies absent from the summary tree
  // (npm can omit them from workspace-filtered output without failing).
  const groups = ['dependencies', 'devDependencies', 'optionalDependencies']
    .map(key => (manifest[key] === undefined ? {} : manifest[key]));
  for (const group of groups) {
    if (!record(group) || Object.values(group).some(value => typeof value !== 'string')) {
      // Invalid manifest: fail closed with no named lines (nothing to leak).
      process.exit(2);
    }
  }
  const optional = groups[2];
  const actual = record(summary) && record(summary.dependencies) ? summary.dependencies : {};
  for (const name of Object.keys({ ...groups[0], ...groups[1] })) {
    if (Object.hasOwn(optional, name)) continue;
    if (!Object.hasOwn(actual, name) || !record(actual[name])) problems.push(`root:${name}:missing`);
  }
  for (const line of [...new Set(problems)].sort()) console.log(line);
} catch (_) {
  // Unparseable summary: no named lines; the caller falls back to the fixed message.
  process.exit(1);
}
NODE
}

problems="$(mktemp)"
trap 'rm -f -- "$summary" "$problems"' EXIT
emitter_status=0
emit_problem_lines >"$problems" 2>/dev/null || emitter_status=$?
if [ "$emitter_status" -ne 0 ]; then
  # Fail closed: unparseable summary or invalid manifest — fixed message only.
  : >"$problems"
  status=1
fi

if [ "$status" -ne 0 ] || [ -s "$problems" ]; then
  {
    printf 'error: required Node dependencies are missing or invalid (npm ls exit %s)\n' "$status"
    if [ -s "$problems" ]; then
      sed 's/^/error:   /' "$problems"
    fi
  } >&2
  if [ "$status" -eq 0 ]; then
    exit 1
  fi
  exit "$status"
fi

# npm's workspace-filtered list can omit missing root devDependencies without
# failing. (Their named lines are emitted above from the summary + manifest.)
echo 'Required Node dependency health check passed (root, ui-tui, web)'
