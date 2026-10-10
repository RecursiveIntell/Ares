<p align="center">
  <img src="docs/ares-workbench.svg" width="100%" alt="Ares architecture: an isolated Hermes-compatible runtime feeds explicit plugins, MCP services, and an evidence boundary with optional governed integrations.">
</p>

<!-- README source review: 2026-09-30; installed/runtime proof remains separately scoped -->

# Ares

<p align="center"><strong>An evidence-native, Hermes-compatible AI workbench for bounded execution, inspectable state, and explicit operator control.</strong></p>

<p align="center">
  <a href="https://github.com/RecursiveIntell/Ares">Repository</a> ·
  <a href="https://recursiveintell.github.io/hermes-agent/docs/">Documentation</a> ·
  <a href="https://github.com/NousResearch/hermes-agent">Upstream Hermes</a> ·
  <a href="SECURITY.md">Security</a>
</p>

> [!IMPORTANT]
> **Ares is not regular Hermes and is not an official Nous Research product.** Ares is a RecursiveIntell downstream distribution of [Hermes Agent](https://github.com/NousResearch/hermes-agent). It preserves the Hermes Python package and `hermes` CLI for compatibility while adding an isolated `ares` launcher, managed runtime releases, and explicit evidence-oriented integration boundaries.

## What Ares is

Ares is for operators who want the familiar Hermes agent experience without treating a chat transcript, a registered tool, or a successful-looking model response as proof that work completed correctly.

Ares keeps Hermes’s normal conversation, model routing, tools, plugins, skills, MCP, gateway, TUI, and desktop surfaces. Its fork-owned layer adds a separate runtime control plane around that compatible base:

| Surface | Hermes compatibility | What Ares adds or changes |
|---|---|---|
| Agent process | Existing Python package and `hermes` CLI remain available | An `ares` launcher selects a stable Ares runtime and defaults to the independent `~/.ares` home. |
| Runtime lifecycle | Hermes can be installed and updated through its normal flows | Ares materializes releases, switches them atomically, keeps current/previous pointers, and supports `doctor`, `status`, and rollback. |
| Release custody | Hermes update behavior remains available inside the selected runtime | Ares-owned candidate custody binds release artifacts, identities, inventories, lifecycle events, authorization, and rollback state. See [`docs/ares-candidate-custody.md`](docs/ares-candidate-custody.md). |
| Governed execution | Hermes approvals and toolsets remain the normal agent boundary | The optional Recursive Agent plugin submits one bounded operation through local authenticated IPC and returns daemon-derived verification facts. |
| RecursiveIntell integrations | Normal Hermes providers, MCP, plugins, and skills remain opt-in | Optional transports and external services can be admitted independently: `llm-pipeline`, `context-governor`, `agent-graph`, `poly-kv`, Semantic Memory, Claim Ledger, CEA Graph, and Pilot Bridge. Source presence is not activation. |
| Documentation | Hermes-compatible reference material remains useful | Ares documents which surfaces are fork-owned, inherited, optional, verified, or still unverified. |

### The core distinction

Ares separates four states that are easy to confuse:

```text
selected  →  registered  →  exposed  →  exercised
   │            │             │            │
 config       tool/MCP      current       real run,
 choice       discovery     session       result/receipt
```

A component is not proven merely because it appears in source, configuration, a tool listing, or a successful registration step.

## Capability map

This is the compact Ares-versus-Hermes map. “Inherited” means the surface comes from the Hermes-compatible runtime; “gated” means an operator must install, configure, or verify another component first.

| Capability | Ares state | Hermes relationship | Where to go next |
|---|---|---|---|
| Interactive CLI / TUI | Inherited and launched through `ares chat` / `ares tui` | Compatible Hermes surface | [Hermes CLI documentation](https://hermes-agent.nousresearch.com/docs/user-guide/cli) |
| Desktop | Managed by `ares desktop` after a desktop-capable runtime build | Compatible Hermes desktop surface with Ares branding/runtime selection | [`apps/desktop/README.md`](apps/desktop/README.md) |
| Gateway | Managed by `ares gateway ...` and `ares-gateway.service` | Compatible Hermes gateway and platform adapters | [Messaging documentation](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) |
| Providers and model routing | Inherited | Hermes configuration and provider system | [Provider documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Tools, toolsets, plugins, and skills | Inherited, with Ares home isolation | Hermes extension model | [Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) · [Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) |
| MCP | Inherited | Hermes MCP client and configured servers | [MCP documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) |
| Cron and scheduled work | Inherited | Hermes scheduler and delivery model | [Cron documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) |
| Stable runtime lifecycle | Ares-owned | Not a replacement for Hermes’s normal update path | [`ares update`](#runtime-operations) · [`ares rollback`](#runtime-operations) |
| Candidate custody | Ares-owned and separately persisted | No claim that upstream Hermes provides this Ares custody layer | [`docs/ares-candidate-custody.md`](docs/ares-candidate-custody.md) |
| Recursive Agent execution | Gated | Separate plugin and daemon | [`docs/ares-recursive-agent.md`](docs/ares-recursive-agent.md) |
| Rust-backed RecursiveIntell transports | Gated and environment-dependent | Not part of the normal Hermes compatibility guarantee | [Transport boundaries](#recursiveintell-integrations) |
| Semantic Memory, Agent Graph, Claim Ledger, CEA Graph, Pilot Bridge | External and opt-in | Separate services/projects | [Integration boundaries](#integration-boundaries) |

Ares does **not** claim that every optional service is installed, that every native extension is active, or that every provider/platform combination has been tested on every host.

## Quick start

### Prerequisites

- Git
- [uv](https://docs.astral.sh/uv/)
- Python **3.11–3.14** is admitted by the current project metadata (`>=3.11,<3.15`). The inherited POSIX installer provisions 3.11 by default; the committed Desktop resolver explicitly probes 3.11–3.14. Resolver support is a source-level admission decision, not proof that every native extension or installation path works on each Python minor and operating system.
- A model provider configured through the normal Hermes setup flow

- The Ares runtime controller is currently exercised through the Python module entry point on POSIX systems. `scripts/install.sh` and `scripts/install.ps1` are inherited Hermes installers; they do not create the Ares `ares` launcher or the Ares stable-runtime layout.

### Install from the Ares fork

Review the source and installer behavior before executing it, then run:

```bash
git clone https://github.com/RecursiveIntell/Ares.git Ares
cd Ares
uv sync --locked --extra all --no-dev
.venv/bin/python -m ares_runtime.local_runtime setup \
  --source "$PWD" --no-desktop --no-gateway
```

Before running setup, review its migration boundary: `--seed-from` defaults to `~/.hermes` and may copy configuration, credentials, profiles, skills and plugins into the Ares home. Use an explicitly chosen empty directory as `--seed-from PATH` if you want an unseeded first setup. When the strict `ri-context-governor` engine is already selected, setup also provisions its governed key state if needed; it is not a passive filesystem inspection.

A successful setup creates or selects:

- independent Ares configuration under `~/.ares/`;
- stable runtime releases under `~/.ares/runtime/releases/<commit>/source/`;
- atomically replaced `current` and `previous` runtime pointers;
- Ares control state under `~/.ares/runtime-state/`;
- a launcher at `~/.local/bin/ares`;
- the `ares-gateway.service` user unit only when gateway installation is enabled and a compatible systemd user bus is available.

If `~/.local/bin` is not on `PATH`, add it through your shell profile. Then check the selected runtime:

```bash
ares --version
ares status
ares doctor
```

The expected first-success signal is a selected Ares revision followed by `PASS` checks from `ares doctor`. Doctor checks selected-runtime imports and SQLite, the configured strict governor, managed-process coherence, and enabled MCP readiness. Its MCP probe can start configured subprocess servers or contact configured endpoints, so review those server commands and destinations before running it. Provider credentials are still your responsibility; setup does not create credentials or silently authorize external services. The first setup command above intentionally omits Desktop and the gateway so the CLI path can be validated without a desktop build or systemd user service.

### Choose the Ares runtime surface

```bash
ares chat                 # Hermes-compatible interactive CLI
ares tui                  # Hermes-compatible TUI
ares desktop              # Launch the selected Desktop build, if installed
ares gateway status       # Inspect the Ares gateway service
```

To build the optional Desktop and install the gateway on a host that supports
them, repeat setup without the two opt-outs:

```bash
.venv/bin/python -m ares_runtime.local_runtime setup --source "$PWD"
```

## The `ares` command reference

The launcher is defined in [`ares_runtime/local_runtime.py`](ares_runtime/local_runtime.py). Run `ares --help` on an installed runtime for the live parser output.

| Command | Purpose | Important options |
|---|---|---|
| `ares setup` | Build and select a stable runtime from a Git checkout | `--source PATH`, `--seed-from PATH`, `--no-desktop`, `--no-gateway`, `--upstream-remote URL`, `--upstream-branch NAME`, `--transition-from-legacy EXACT_CURRENT_SHA` |
| `ares update` | Build and atomically select the configured remote candidate | `--no-desktop` |
| `ares rollback` | Return to the previous stable runtime | None |
| `ares doctor` | Check runtime pointers, imports, configuration, native integrations, and gateway state | None |
| `ares status` | Show selected runtime, remote, and gateway information | None |
| `ares desktop` | Launch the selected Ares Desktop application | `--rebuild` |
| `ares tui` | Launch the selected Hermes-compatible TUI | Pass-through TUI arguments are accepted |
| `ares chat` | Launch the selected Hermes-compatible CLI | Pass-through CLI arguments are accepted |
| `ares gateway` | Manage the Ares gateway service | `start`, `stop`, `restart`, `status`, or `foreground` |
| `ares specialist` | Submit an explicit bounded specialist run to the running Ares Desktop | Specialist dispatch arguments are passed through |
| `ares auth` | Delegate Hermes credential-pool operations inside the Ares home | `--type`, `--label`, `--api-key`, OAuth options, `--target`, `--no-browser` |
| `ares --version` | Print the selected stable runtime revision | None |

`ares chat` and `ares tui` pass remaining arguments to the selected Hermes
runtime. `ares auth` also passes through the underlying Hermes auth behavior;
prefer its secure prompt or OAuth flow over putting a credential in a shell
command, and never commit or paste credential values into issues or logs.

`ares desktop --rebuild` is retained as a deprecated compatibility option for
the immutable-release controller; use `ares update` to produce a new selected
release instead of rebuilding an installed release in place.

### Runtime controller paths and isolation

The controller reads these optional environment variables before setup:

| Variable | Default | Scope |
|---|---|---|
| `ARES_HOME` | `~/.ares` | Ares controller state, releases, launcher environment, and Ares agent home |
| `ARES_BIN_DIR` | `~/.local/bin` | Directory for the generated `ares` launcher |

After setup, the generated launcher resolves through `current`; it does not
fall back to the checkout that was used to create the release. Each selected
release has its own `.venv`, is cloned at an immutable Git revision, and is
built in a staging directory before the release becomes visible. The launch
environment removes `PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`, `VIRTUAL_ENV`,
and `UV_PROJECT_ENVIRONMENT`, sets `PYTHONNOUSERSITE=1`, and marks the process
with `ARES_RUNTIME_MODE=stable` plus the selected release identity.

`ares setup` may copy `config.yaml`, `.env`, `auth.json`, `active_profile`,
`profiles/`, `skills/`, and `plugins/` once from `--seed-from` (default
`~/.hermes`). This is an explicit migration, not a live fallback: later
changes in the Hermes and Ares homes are independent.

### Runtime operations

```bash
ares status
ares update
ares doctor
ares rollback
```

`ares update` stages a configured Hermes upstream revision, applies the Ares downstream state, builds the candidate, and switches only after the candidate succeeds. If the build or activation path fails, the active release is intended to remain selected. `ares rollback` returns to the previous stable release when one exists.

Ordinary `ares setup` and `ares update` refuse an unqualified selected release.
For a verified legacy release without `runtime_binding`, follow the
[source-bound transition runbook](docs/operations/legacy-controller-transition.md):
use the new controller's `setup --transition-from-legacy EXACT_CURRENT_SHA`
with the full 40-character selected revision. The installed old `ares` launcher
cannot emit the new binding contract. Preserve existing release bytes; this
option does not repair a contract-1 release with missing or invalid bindings.
For a CLI-only legacy release, also pass `--no-desktop` during this transition;
the legacy probe otherwise requires a Desktop executable in that release.
Build Desktop only in a later qualified release.

The source-backed custody details are deliberately kept out of this quick-start block. Read [`docs/ares-candidate-custody.md`](docs/ares-candidate-custody.md) before treating candidate certification, audit state, or rollback state as an authority decision: certification and candidate-bundled activation input are explicitly non-authorizing until the CandidateStore-owned activation transition occurs.

The local controller and CandidateStore are separate lifecycle lanes:

```text
development checkout ──setup/update──> Ares local runtime current/previous

identified artifacts ──CandidateStore──> sealed candidate ──explicit grant──>
                                       certified activation path
```

`ares setup`/`ares update` do not turn a source checkout into a CandidateStore
sealed candidate. Conversely, CandidateStore custody does not by itself select
or start a local runtime. Keep the two claims separate.

## Profile collaboration and specialist routing

Ares ships an optional `profile-collaboration` skill for work that genuinely
benefits from an independent specialist. This is orchestration and evidence
plumbing, not a new authority layer: specialist reports remain advisory until
the controller checks their receipt and the current source/runtime state.

### Relevance-gated panel runner

The repository copy lives at
[`optional-skills/productivity/profile-collaboration/`](optional-skills/productivity/profile-collaboration/).
The canonical runner requires either an explicit `--profiles` list or an
explicit `--full-panel`; it refuses an implicit fan-out. The current runner
profile order is:

```text
public, explorer, job-scout, longmemeval-bench, statistician,
ml-evaluation-researcher, cognitive-scientist, psychometrician, inbox-manager
```

The lanes are intentionally narrow:

| Profile | Decision lane |
|---|---|
| `public` | README/docs, release wording, external claims, and publication boundaries |
| `explorer` | Competing designs, novel combinations, falsification, and kill criteria |
| `job-scout` | Current job research and verified application-target shortlists; read-only and never applies for the operator |
| `longmemeval-bench` | Source lineage, schemas, receipts, and reproducibility |
| `statistician` | Estimands, uncertainty, quantitative comparisons, and evidence strength |
| `ml-evaluation-researcher` | Benchmark design, controls, rubrics, and model/evaluation claims |
| `cognitive-scientist` | Coordination, dissent, reconciliation, authority, and recovery constructs |
| `psychometrician` | Measurement validity and discriminant validity |
| `inbox-manager` | Correspondence, follow-up, commitments, and communication evidence |

Example dry run and execution:

```bash
RUNNER=optional-skills/productivity/profile-collaboration/scripts/run_panel.py
python3 "$RUNNER" --workspace "$PWD" --runtime "$HOME/.ares/runtime/current" \
  --profiles public --brief 'Read-only claim-boundary review of the current README.' \
  --dry-run
python3 "$RUNNER" --workspace "$PWD" --runtime "$HOME/.ares/runtime/current" \
  --profiles public --brief 'Read-only claim-boundary review of the current README.' \
  --max-workers 1
```

The runner defaults to a 180-second per-profile timeout and a 600-second
panel deadline, captures at most 512 KiB per stdout/stderr stream, redacts
secret-like values, records per-profile hashes, and terminates process groups
on timeout. It requests archival of automation-owned one-shot sessions so
those sessions do not pollute the normal Desktop Sessions projection. These
receipts prove orchestration mechanics, not the truth of a specialist report.

Verify an executed panel with the directory containing `panel.json`:

```bash
python3 optional-skills/productivity/profile-collaboration/scripts/verify_receipt.py \
  --receipt "$HOME/.ares/profile-collaboration/receipts/<run-id>" \
  --runtime "$HOME/.ares/runtime/current"
```

The verifier checks profile order, runtime identity, artifact existence,
byte counts, and SHA-256 hashes. A nonzero profile exit, timeout, empty report,
failed archival, or failed verification is blocked/failed evidence—not
approval. The controller must still review uncertainty, dissent, relevance,
and next gates.

### Explicit specialist dispatch

The committed Ares runtime also exposes `ares specialist`, which submits an
explicit bounded request to a running Ares Desktop. The request must identify
the profiles, requested capability, workspace, brief, and a content digest;
the request and candidate descriptor set are validated before dispatch.
Electron remains the capacity and admission owner: the runtime-side decision
is non-activating, does not automatically select a profile, does not reserve a
slot, and cannot grant dispatch authority. Invalid, disabled, mismatched, or
over-capacity requests remain blocked with a typed receipt.

This is separate from the optional profile-panel skill above. The panel runner
owns profile consultation orchestration and redacted report artifacts; the
Desktop specialist command owns an explicit, Electron-admitted run path.

## Integration boundaries

Ares does not bundle authority into a product slogan. The layers remain separate:

```text
operator
   │
   ▼
Ares launcher ──> stable Hermes-compatible runtime ──> tools / plugins / MCP
                                      │
                                      ├── optional Recursive Agent plugin
                                      │       └── local authenticated IPC
                                      │             └── bounded daemon run + receipt chain
                                      │
                                      └── optional external services
                                              ├── Semantic Memory
                                              ├── Agent Graph
                                              ├── Claim Ledger
                                              ├── CEA Graph
                                              └── Pilot Bridge
```

- **Hermes-compatible runtime** owns conversation, provider routing, tool selection, approvals, plugins, and normal persistence.
- **Ares** owns downstream identity, installer behavior, isolated home selection, runtime lifecycle, documentation boundaries, and integration policy in this repository.
- **Recursive Agent** owns its run contract, state machine, receipt chain, and verification result. The plugin does not manufacture evidence or bypass the daemon.
- **MCP services** remain separate processes or services. A registered tool is not proof that its backend is reachable or that a real operation succeeded.

### RecursiveIntell integrations

The repository includes optional transport modules for `llm-pipeline`, `context-governor`, `agent-graph`, and `poly-kv`. The code also exposes the Hermes/Ares `/llm-pipeline` control surface for inspecting or changing the transport state. These paths are **gated**, not unconditional promises:

1. select the relevant provider or engine;
2. install or materialize the required native/runtime component;
3. run `ares doctor` or the integration-specific checks;
4. exercise a real request in the target environment;
5. retain the returned evidence before making a capability claim.

The presence of source modules, a config key, or a registered MCP server does not establish any of those steps.

The current transport adapters are optional and capability-gated. Some become
active by default when their native extension is present, and each exposes an
explicit disable or restriction gate:

| Adapter | What it provides | Gate / limitation |
|---|---|---|
| `ri_llm` | Rust-backed `llm-pipeline` path for representable, unauthenticated `ollama-launch` text requests | Native extension required; default selection is limited to qualifying Ollama requests; `HERMES_RI_PIPELINE=0` disables it; unsupported ordinary requests keep their same-provider SDK route; explicit native selection raises `RI_PIPELINE_REQUEST_UNSUPPORTED` for unsupported requests |
| `ri_context_compressor` | Legacy PyO3-backed context compressor with an LLM summarizer fallback and receipt preservation | Native extension required for this legacy lane; it is distinct from the configured CLI-backed engine below; the CEA graph lane is advisory, read-only, and fails open |
| `ri_agent_graph` | Rust-backed in-process state plus read-only direct SQLite queries for runs, graphs, state, and receipts | Native extension required for the accelerator; active by default when available; writes remain MCP-mediated; `HERMES_RI_AGENT_GRAPH=0` disables the read accelerator; `HERMES_RI_AGENT_GRAPH_DB` selects the DB |
| `ri_poly_kv` | Shape validation, synthetic-pool receipts, local cosine/top-k scoring, and compressed-domain integration points | Native extension required; `HERMES_RI_POLY_KV=0` disables; the adapter returns `None` on errors so callers can use the MCP path; the scorer is alpha |

`ri_autoload` imports these components once and logs availability; it does not
install them, activate their external services, or certify their native
artifacts. The CEA graph integration reads a separately configured graph
binary/database and writes nothing from the compressor path.

### Configured Context Governor engine

The discoverable [`ri-context-governor` plugin](plugins/context_engine/ri-context-governor/__init__.py) is the selection owner for `context.engine: ri-context-governor`. It delegates to the [shared CLI adapter](plugins/context_engine/_context_governor/__init__.py), which invokes the Rust `context-governor` executable and checks governed key/protocol/receipt state. This path does not depend on the unrelated PyO3 compressor being installed.

Choose the engine through the existing configuration workflow, then validate the installed executable and adapter together with the runtime's strict probe. A source-present plugin, a passing Python import, and an old closure document do not establish current activation or successful compaction on an existing long-running session. The legacy transport and configured engine have different owners and proof boundaries.

### Optional Recursive Agent plugin

The Recursive Agent integration is a standalone plugin, not a bundled core tool. It requires a separately built and running local Recursive Agent daemon.

From the Ares checkout, pointing at an existing `RecursiveIntell/recursive-agent` checkout:

```bash
bash install.sh --with-recursive-agent-source /path/to/recursive-agent
```

This is the source-defined integration path but is not end-to-end verified by
the README checks. It does **not** build, configure, start, or grant authority
to the Recursive Agent daemon. The bootstrap invokes the runtime module through
the selected Python environment; the plugin checkout owns plugin rollback.

This installs the plugin package into `~/.ares/plugins/recursive-agent-native`. It does **not** build, configure, start, or grant authority to the daemon. Start a fresh Ares/Hermes session after plugin installation so discovery can occur.

Read [`docs/ares-recursive-agent.md`](docs/ares-recursive-agent.md) for the socket contract, operation envelope, receipts, and verification semantics.

## Configuration and data ownership

Ares preserves the Hermes-compatible configuration format but uses `~/.ares` as its independent agent home. Ares and an existing Hermes installation can therefore have different providers, skills, plugins, sessions, and gateway lifecycles on the same machine.

Keep these boundaries explicit:

- provider secrets belong in the supported local secret mechanism, never in this repository or shell history;
- MCP server mappings and argument lists are typed YAML, not ad-hoc strings;
- plugins and hooks run with agent-process authority and must be reviewed before installation;
- restart or start a fresh session after changing plugin, toolset, MCP, or credential configuration because tool schemas are session-scoped;
- prove a capability at the correct layer: selected, registered, exposed, then exercised.

The bootstrap installer accepts:

| Installer option | Effect |
|---|---|
| `--branch NAME` | Clone or update a specific branch. |
| `--dir PATH` | Select the source checkout directory. |
| `--hermes-home PATH` | Select the Ares data directory. |
| `--ares-bin-dir PATH` | Select where the `ares` launcher is written. |
| `--no-venv` | Use the active Python environment instead of a managed virtual environment. |
| `--no-desktop` | Skip the Desktop build for a CLI-only or disposable installation. |
| `--no-gateway` | Do not install, enable, or start the user-level Ares gateway service. |
| `--with-recursive-agent-source PATH` | Install only the standalone Recursive Agent plugin from an existing checkout. The daemon remains operator-managed. |

Run `bash install.sh --help` for the authoritative installer contract. The bootstrap refuses to update a dirty existing checkout and refuses to overwrite a non-Ares launcher.

### Inherited installer and runtime remediation behavior

The inherited Hermes installers now own their `uv` binary under
`$HERMES_HOME/bin/uv` (or `bin\\uv.exe` on Windows) instead of trusting an
ambient PATH copy. Runtime repair provisions a sibling Python generation,
probes its SQLite behavior and imports, and cuts over only after the candidate
passes; it does not replace a live vulnerable interpreter in place. The
project's `requires-python` constraint is the source of truth for admissible
future minor versions. A failed repair leaves the existing runtime in place
when possible and reports that the next update should retry.

The current project admission range is `>=3.11,<3.15` (Python 3.11 through
3.14). This is a packaging/source contract, not proof that every provider,
native extension, Desktop build, or optional service works on every minor and
every operating system.

**Installer proof boundary:** the managed bootstrap invokes
`.venv/bin/python -m ares_runtime.local_runtime`; it does not depend on an
undeclared `ares` console script. A hermetic subprocess regression exercises
managed and active-environment setup, option/environment forwarding, and
fail-closed dependency-install errors. Git, dependency installation and the
setup implementation are fixture boundaries in that test, so it is not a full
installation or installed-runtime acceptance test. Shell syntax and help are
checked separately. The manual module-based [Quick start](#quick-start) remains
available.

## Security and trust boundaries

Ares inherits Hermes’s fundamental security posture: **the operating system or an explicit whole-process sandbox is the real boundary against adversarial model output.** Approval prompts, tool allowlists, plugin review, redaction, and receipts are useful controls; they are not containment.

Terminal-backend isolation is narrower than whole-process wrapping: it can
confine shell and file operations routed through that backend, but it does not
contain in-process plugins, hooks, skills, MCP subprocesses, or the
code-execution path. Use whole-process wrapping when those paths must share one
filesystem, network, process, and credential policy.

Important consequences:

- a plugin runs with the authority of the agent process;
- a local IPC socket or verified receipt does not contain a compromised process;
- do not give an agent access to files, credentials, network destinations, or destructive tools you would not delegate to it;
- use a whole-process wrapper or deliberately constrained account for untrusted content or higher-risk workloads.

Read [`SECURITY.md`](SECURITY.md) before exposing Ares to untrusted inputs or shared environments.

### Authorization matrix and failure remediation

Authorization is a separate question from routing, session identity, or
successful process startup:

| Surface | Required boundary | What does not count |
|---|---|---|
| Messaging and network HTTP adapters | Operator-configured caller allowlist before dispatch, approval resolution, or output relay | Knowing a session ID; an open listening port; a successful HTTP response |
| Dashboard, API, and plugin HTTP servers | Loopback/OS access by default, or an explicit network auth layer plus an allowlist when exposed | `--host 0.0.0.0` by itself; an unreviewed plugin |
| TUI gateway and ACP/local IPC | Host-user access control, restrictive permissions, and loopback/local binding unless separately protected | Treating local IPC as safe against every same-user process |
| Profile routing | Multiplexing plus an existing target profile; routing chooses a profile but does not grant new caller authority | A route entry or profile name alone |
| Recursive Agent permit bridge | Private same-user Unix socket, exact daemon binding, and daemon-issued permit/receipt facts | A local receipt, a copied binding, or a session identifier |

For a failed or ambiguous trust transition, preserve the exact error and
receipt, do not promote the candidate or widen permissions, then re-run the
owner's check:

| State | Operator action | Next gate |
|---|---|---|
| `AUDIT_BLOCKED` | Keep the candidate blocked; retain the audit lease/handoff and repair the missing audit capability | A fresh hostile audit may resume; it cannot auto-pass |
| `AUDIT_FAILED` or `CUSTODY_CORRUPT` | Quarantine/hold the candidate and preserve custody evidence; do not activate it | Repair or rebuild, then publish and audit a new exact candidate |
| `AWAITING_ACTIVATION` | Confirm the explicit CandidateStore-owned authorization transition; do not confuse it with activation | Exact grant, materialization, runtime identity, and live certification |
| Post-commit failure / `ROLLBACK_REQUIRED` | Use the previous verified runtime when available; retain the transaction journal and failed candidate | Health and identity verification of the restored runtime |
| `INCIDENT_HELD` or failed GC | Stop deletion/activation and retain the candidate/tombstone state | Manual incident review and a new explicit lifecycle decision |

Rollback restores a prior runtime; it does not repair authorization, prove a
new candidate, or erase the incident record. The full custody state machine and
garbage-collection rules are in [`docs/ares-candidate-custody.md`](docs/ares-candidate-custody.md).

### Optional effect and permit boundary

When enabled explicitly, `ARES_STRICT_EFFECT_TOOL_ARGS_V1=1` rejects unknown
fields, missing required fields, and type coercion for effectful tool payloads.
`ARES_RUNTIME_PERMITS_V1=1` requires the configured daemon permit bridge for
effectful tools. The bridge checks an exact tool/argument binding, uses the
daemon-owned canonical digest helper, requires a private same-user Unix socket
and peer credentials, and returns daemon-derived evidence/preflight/receipt
facts. It never mints permits or persists a local substitute receipt. Missing,
malformed, stale, or denied bridge state fails closed. These flags are
experimental/runtime-gated controls; they are not a security certification.

## Repository map

| Path | Role |
|---|---|
| `install.sh` | Ares bootstrap installer for the Ares checkout, runtime-module setup, stable launcher, and optional Recursive Agent plugin. |
| `scripts/install.sh`, `scripts/install.ps1` | Inherited Hermes installers and dependency/bootstrap surfaces; they are not the Ares stable-runtime launcher. |
| `ares_runtime/` | Stable runtime selection, materialization, activation, rollback, gateway handoff, and launcher implementation. |
| `agent/transports/ri_*.py` | Optional RecursiveIntell transport integrations. |
| `docs/ares-candidate-custody.md` | Candidate custody, lifecycle, audit, authorization, and garbage-collection contract. |
| `docs/ares-recursive-agent.md` | Recursive Agent boundary and operator guide. |
| `website/` | Ares documentation front door plus Hermes-compatible reference material. |
| `optional-skills/productivity/profile-collaboration/` | Relevance-gated specialist runner and receipt verifier. |
| `tests/test_ares_distribution.py` | Fork identity and installer-scope contract tests. |
| `tests/ares_runtime/`, `tests/test_ares_collaboration.py` | Runtime, custody-boundary, effect, permit, witness, and replay contract tests. |

## Development and validation

Ares is a large Python, TypeScript, and desktop codebase. Start with [`AGENTS.md`](AGENTS.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md). For repository tests, sync the development extra in addition to the runtime extras:

```bash
uv sync --locked --extra all --extra dev
```

Useful bounded checks from this checkout:

```bash
bash -n install.sh
bash install.sh --help
bash -n scripts/install.sh
bash scripts/install.sh --help
scripts/run_tests.sh tests/test_ares_distribution.py -q
scripts/run_tests.sh tests/test_ares_collaboration.py -q
```

For broader validation, use the repository-owned test entry point:

```bash
scripts/run_tests.sh
```

The commands above validate Ares and inherited installer syntax/help, the Ares
distribution contract, and selected runtime/collaboration behavior. They do
not prove that a model provider, optional daemon, native extension, Desktop
package, or production deployment works on every host. Per repository policy,
use `scripts/run_tests.sh` rather than invoking `pytest` directly; it applies
CI-parity environment isolation and subprocess-per-file test isolation.

## Deeper Hermes-compatible documentation

Ares intentionally does not duplicate the entire Hermes manual. Use these references for inherited capabilities:

- [Hermes quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)
- [CLI and configuration](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- [Providers and models](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- [Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Messaging gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)

Where a page names upstream URLs or support channels, treat those as Hermes reference material—not as an Ares release, support, or universal compatibility guarantee.

## Status and claim boundary

**README source review: 2026-09-30, committed `main` snapshot `56d296c70d9fe7f1cf1e978c730b3122a57de44e`, followed by the bootstrap invocation repair.** The installation/help, Python admission, configured engine and runtime-probe descriptions were reconciled against that source. Root and inherited POSIX installer syntax/help and the module CLI help were checked. The bootstrap follow-up replaces its undeclared console-script invocation with the runtime module and adds the bounded regression described above.

This documentation review did not run an installation, select an installed release, contact configured MCP servers or execute live compaction. The canonical distribution-test command was attempted but could not start because this review environment lacked a pytest-enabled project virtualenv. Do not treat documentation publication or unmerged PR descriptions as passing runtime evidence.

That source review does **not** establish cross-platform support, public packaging of the Recursive Agent daemon, a managed service installer for every optional service, production readiness, security certification, performance superiority, or universal provider/platform support. Treat those as separate verification projects.

## Upstream provenance, contributions, and license

Ares is derived from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). The canonical downstream repository is [RecursiveIntell/Ares](https://github.com/RecursiveIntell/Ares); the historical [`RecursiveIntell/hermes-agent`](https://github.com/RecursiveIntell/hermes-agent) repository path remains a compatibility reference for existing documentation and issue links. Preserve upstream attribution and license notices when redistributing or contributing changes.

- Security reporting: [`SECURITY.md`](SECURITY.md)
- Contribution process: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- License: [`MIT`](LICENSE)
