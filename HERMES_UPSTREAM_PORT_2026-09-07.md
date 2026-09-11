# Hermes selective upstream port receipt

- Source: `NousResearch/hermes-agent`
- Target: `RecursiveIntell/Ares`
- Method: literal `git cherry-pick -x`; non-clean commits are aborted, quarantined, and recorded; no conflict is auto-resolved.
- Scope: vetted efficiency, lifecycle reliability, model/context correctness, and platform fixes that do not intentionally replace Ares authority/permit/context owners.
- Result: **16 upstream commits ported cleanly; 20 non-clean candidates quarantined automatically**.
- Bootstrap port head: `dcab80c0a9e05912fdea98f9a274a90f5c078ee6` before this receipt-only summary update.
- Build-certification boundary: bootstrap static validation passed, but repository CI/Nix/Docker must run on the owner-authored final PR head before this PR can be considered merge-ready.

## Ported upstream commits
- `5180601a6ab2aa43b19540a5692914ad7b2630a9` — perf(cli): dispatch serve without the full parser tree
- `4155ea97e881e12d68688fc323abbd6c6bcce0f3` — perf(serve): Desktop backend announces its socket before MCP discovery imports the SDK
- `0534f1033b4547aed86b235ecc1ec18f01492d90` — perf(state): route 39 pure-read SessionDB methods off the writer lock + gate
- `32fe1293244f186c75e82090ed06fe416739f146` — perf(bot-mode): cold DM hops skip the live /models probe; relay replies land within 250ms
- `6064668c8fd2dbbb232ea073b32c9d06d932fa56` — perf(usage): prefer bundled pricing before metadata fetch
- `cb0b66c16170042d22ffb9714ec19f601a3c5cc5` — perf(gateway): offload inbound media cache writes
- `8b681f70ea505959cf7721be40d9dad8f3d33c8f` — perf(process): poll sandbox job logs for new bytes only
- `4a1780228008572445d8a5d98f30340acf6e9695` — perf(constants): remember the resolved Hermes home key
- `8196d409a06eeaf2afaba910f673b80b532c8140` — perf(tools): persist OSV malware-check verdict cache to disk
- `2d783a15eb0b582938e96d4b0ccbe97f6a1a849a` — perf(mcp): one parent-death supervisor per process, not one per stdio server
- `6a2b3f1ebf6eabc09e5dbb14236df39bb64de2ac` — perf(file-ops): collapse read_file's shell probes into one compound command
- `11ed840431aba7ca3fd51508ecf11bf234131762` — perf(mcp): skip npx's resident parent when the package is already cached
- `7caee2898b70d445f0dcc08e198394b18b393b12` — perf(agent): stop rebuilding the streamed reply text on every delta
- `c96568f66ca49d27beec4545bee9740b09d64018` — perf(delegation): finished delegate children no longer pin their transcripts in the parent heap
- `c3ce41645cb08f39e7dd5739dcfd72527096896f` — fix(tui): restore Shift+letter case in the composer for extended-key terminals (#90674)
- `6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef` — fix(tui): match the redo chord case-insensitively so Cmd+Shift+Z works on extended-key terminals (#105493)

## Automatically quarantined non-clean candidates
- `9fbb3b716a5786c5dd2c986b610fdf9ae9bae6ad` — perf(file-ops): merge write_file's pre-write probes into one shell call
- `d3275acf80a05ca79e6ce817d4965283ff7f6f11` — perf(file-ops): native read_file fast path on local POSIX environments
- `8cab422ab09332c1867af81ee4e910878ac1172b` — perf(file-ops): count the native read's tail with memchr; reuse the local-env gate
- `87597d30c8b834ed276bd5361451fd36aad0ffa7` — perf(mcp): apply the -t spawn filter before the mcp SDK import
- `c3b411dfb77fd8a1bad34fb80d4ef1f0e2b6f38e` — perf(agents): share one httpx transport pool across every agent's client
- `2b55ded1ac5f3b41cdc580974e745631dac1bb53` — perf(state): keep delegate-child transcripts out of the trigram FTS index (schema v30)
- `561b053f794a1781868bb032029d589c67708119` — perf(agents): run per-child timers on one shared scheduler thread
- `e245e40f731ce0e723e993803f3286440034c30b` — fix(desktop): warm session switch pegs renderer main thread (#95595)
- `71516214c3d7d59c6d94b6b5a3b325c506dc7cb2` — fix(compressor): thread custom_providers into context-length resolution
- `9160c4e27e32b5984b01cf5514fd4f6ef3471d53` — fix(agent): scan only root mount in is_container() cgroup-v2 fallback
- `869432301b03f29c1384421a97459e8407d9aea1` — fix(tools): detach stdin in the Windows Git Bash probe (#78820)
- `802f0f97adfbf3708888f9d21ba85ca80e0cec87` — fix(gateway): adopt the user D-Bus session when systemd starts the gateway
- `07b161e2419b92970cf198e5c6dbf953653e9193` — refactor(context): fold simplify findings into oversized-file fallback
- `321e8a79b0fa36eb63d9c89fa22d09d993a9866d` — refactor(agent/context_compressor): one newest-first token walk shared by tail-cut and prune boundary
- `116ca1db1e0b900fca37c4aba6b59370a5dff4d4` — fix: sibling Nous 401 recovery adopts a peer's refresh instead of rotating again
- `6a04ea67c0e75ef1218bf4ceaac071c2f16a0701` — fix(gemini): collapse array-typed tool schemas instead of crashing translation
- `bbcf1ee180f170032079996b87f296acf2af7456` — fix: preserve native Gemini union constraints
- `0d6106eab8a5b8afc3357d6e1030723b8be129f2` — test: consolidate Gemini array regressions into two invariants
- `c111ede3e56c62b0399cf932acafc4f0a5757bdf` — fix(picker): resolve key_cmd credentials for model discovery
- `520e63661c8eaa2135ebd60a07192f0d8aa45e6e` — fix: keep command-auth model discovery lazy across config and setup

## Known non-applicable / adaptation-required candidates
- `b363fee510a048b010480ca7941126b8613fa6df` and `bbbd3b100fb353bc72cb5c887d84bca9697e8bc1` — Hermes Yoga rounding-cache optimizations conflict with Ares independently changed/deleted TUI layout surfaces; do not resolve toward upstream blindly.
- Delegation fallback-policy commits — require Ares authority-scope adaptation rather than direct cherry-pick.
- Owner-mailbox / durable Bot Chat ingress commits — require review against Ares managed-admission, receipt, and goal/session ownership semantics.
- Group-chat parallelism changes — alter scheduling semantics and need validation against Ares specialist/authority policy.
- Approval-parser changes — security-relevant but overlap Ares approval/permit ownership, so they need owner-preserving adaptation.
- Subagent process-handoff changes — useful upstream behavior but authority-bearing in Ares; adapt rather than silently import.

## Validation performed by bootstrap
- Every included upstream commit applied as a literal conflict-free `git cherry-pick -x`.
- Any non-clean candidate was aborted and recorded rather than resolved.
- No included upstream commit may modify `ares_runtime/`.
- `git diff --check` over the resulting port.
- Python compileall over agent/tools/hermes_cli/gateway/cron/run_agent.py.
- Repository PR CI remains the build-certified gate; upstream benchmark numbers are not Ares benchmark claims.
