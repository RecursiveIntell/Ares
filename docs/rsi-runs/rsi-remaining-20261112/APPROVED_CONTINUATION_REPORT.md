# Approved isolated-helper continuation — verified engineering, RSI inactive

## Result
- Ares helper/file-tool developer regression: **308 passed, 0 failed, 4 skipped** across 7 files.
- The exact same final new contract tests against the pinned baseline: **35 failed, 122 passed**. Candidate passes all 157 cases. This demonstrates test sensitivity, not independent hidden evaluation.
- Seven-package Rust substrate gate: **348 passed, 0 failed, 1 ignored** across 60 suites.
- Seven-package Clippy with `-D warnings`: PASS on matching stable final bytes. Changed/new Rust formatting and both Git whitespace checks pass.
- Offline Ares authority/transport owner regressions: **58 passed, 0 failed, 3 skipped**. The skipped cases require the unrelated Bedrock extra (`boto3`); that provider is not qualified by this run.
- **14 selected execution receipts verified**, including intentional RED and failed setup/fixture attempts.

## Changes actually made
1. `approved-helper/Ares/tools/file_operations.py`: changed only file pagination inside `_try_multi_path_search`; fetch per-root prefixes from zero with one lookahead, merge in requested root order, apply combined offset/limit once and preserve known truncation. All other module AST is unchanged. Single-root, path parsing and content-search logic are preserved.
2. `approved-helper/Ares/tests/tools/test_multi_path_pagination_contract.py`: real Bash/rg/find matrix and public tool-handler regression tests. A fixture initially assumed JSON-only output; current public API appends a pagination hint, so the fixture now verifies both without changing production serialization.
3. Isolated Libraries `verification-policy/src/lib.rs`: validate approval metadata, parse query/approval/expiry as instants, refuse future/invalid/reversed windows and use an exclusive expiry boundary. Three tests reproduced the failures before repair.
4. `verification-policy/tests/approval_time_tests.rs`: the three new behavioral witnesses. First build caught a missing `reusable` fixture field; it was repaired before the real RED execution. Failed receipts are preserved.
5. Private harness, source snapshots, approval addendum and evidence outputs only. No other production code changed in this continuation. The first owner-regression run failed because the isolated venv lacked the project's declared Anthropic extra; `uv sync --locked --offline --extra dev --extra anthropic --no-install-project --python /usr/bin/python3` installed only the locked run-owned dependencies before the explicitly linked retry. No source or global environment was weakened.

Ares base: `2ad5151f36a9b4aac55f2b01a5f5f192646ac6a5`. Libraries base: `d7cf668b35f18fbc3dd8ce9e2627521e63a82583`. Both candidates remain uncommitted and isolated. The installed helper baseline hash still matches; nothing was installed, committed, pushed, deployed or activated. No migrations.

## Approval and budget
Your approval settles the isolated helper target and **$5.00-equivalent** ceiling with `gpt-6-luna`. Exact observed configured route is `openai-codex/gpt-6-luna`, effort `xhigh`; no shared defaults changed. The full literal approval is preserved in `APPROVAL_ADDENDUM.md`.

**No optional candidate, worker or reviewer model calls were dispatched after this approval by this continuation.** Main-session cost is not measured here and is not claimed to be zero. Official equivalent tariff is distinct from a subscription invoice. Model-backed campaign admission stays closed until the existing native family/permit and Ares physical-send owners enforce durable cumulative reservations. A declared mission budget and per-send transport lease are not that integration. Generic delegation also has no configured own timeout and no exposed per-call window selector. Direct deterministic work avoids spending on an inadmissible route; no model was silently substituted.

## Not complete / remaining delta
This is **not** an agent-generated improvement round, independently sealed confirmation, or recursive cycle. The full 18-row R00–R16 plus closeout denominator is in `approved-evidence/STATUS.json`.
- R03 remains partial: reviewed artifact/source binding and expiry/revocation at actual consumption need qualification beyond timestamp matching.
- R05/R06: production containment/resources and independently custodied evaluator/holdout accounting remain missing or unqualified. The trusted offline test namespace is not certification for hostile candidates.
- R08/R09: cumulative monetary reservation/settlement and qualified producer remain missing integration, not unanswered operator decisions.
- R10–R12/R15: canonical Python campaign adapter, model-generated acceptance, exact-patch disposable adoption/backout and genuine successor consumption remain unperformed.
- R13/R14: no matched-budget recursive-advantage study, cross-family final verdict or unattended soak qualification. R16 remains deferred and optional.

Rollback is continuation-only inverse changes in isolated trees. The helper inverse patch was checked without applying it. Do not reset canonical/user trees or erase failed evidence. Current review state is controller verification only; no final independent/cross-family semantic verdict is claimed.

## Rerun commands
```bash
python3 /home/sikmindz/.ares/runs/rsi-remaining-20261112/run_gate.py auditor-approved-tests-01 cargo test --locked --offline -p forge-engine -p forge-pilot -p verification-policy -p check-runner -p sandbox-workspace -p typed-patch -p forge-policy --lib --tests -- --test-threads=1
python3 /home/sikmindz/.ares/runs/rsi-remaining-20261112/run_gate.py auditor-approved-clippy-01 cargo clippy --locked --offline -p forge-engine -p forge-pilot -p verification-policy -p check-runner -p sandbox-workspace -p typed-patch -p forge-policy --lib --tests -- -D warnings
python3 /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/test_namespace.py tests/tools/test_multi_path_pagination_contract.py --file-retries 0 -j 1 -v -p no:cacheprovider
python3 /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/test_namespace.py --baseline tests/tools/test_multi_path_pagination_contract.py --file-retries 0 -j 1 -v -p no:cacheprovider
```
Use ri_run.py around auditor helper commands for fresh receipts. The baseline command intentionally exits 1. Fresh run-gate labels are mandatory. Exact commands, stdout/stderr and integrity checks are retained; do not replace a failure with a later pass.

## Verbatim approved-helper-pagination-regression-02

```text
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 7 test files (~156 tests) under ['tests/tools/test_multi_path_pagination_contract.py', 'tests/tools/test_file_operations.py', 'tests/tools/test_file_operations_edge_cases.py', 'tests/tools/test_file_ops_cwd_tracking.py', 'tests/tools/test_file_tools.py', 'tests/tools/test_file_tools_cwd_resolution.py', 'tests/tools/test_file_tools_tilde_profile.py']; running with -j 1
[ 41.0% |    64/~156 | ✓66 | ✗ 0] ✓ tests/tools/test_file_operations.py (66✓ 2s, 3.1s)
[ 54.5% |    85/~156 | ✓87 | ✗ 0] ✓ tests/tools/test_file_operations_edge_cases.py (21✓, 2.7s)
[ 56.4% |    88/~156 | ✓90 | ✗ 0] ✓ tests/tools/test_file_ops_cwd_tracking.py (3✓, 2.6s)
[ 87.2% |   136/~156 | ✓136 | ✗  0] ✓ tests/tools/test_file_tools.py (46✓ 2s, 4.6s)
[ 94.2% |   147/~156 | ✓147 | ✗  0] ✓ tests/tools/test_file_tools_cwd_resolution.py (11✓, 3.6s)
[ 96.8% |   151/~156 | ✓151 | ✗  0] ✓ tests/tools/test_file_tools_tilde_profile.py (4✓, 3.4s)
[100.0% |   156/~156 | ✓308 | ✗  0] ✓ tests/tools/test_multi_path_pagination_contract.py (157✓, 8.4s)

=== Summary: 7 files, 308 tests passed, 0 failed, 4 skipped (100% complete) in 28.4s (1 workers) ===

  note: windows_only tests (in 2 files) were SKIPPED on this host (linux); they run on the tests-os CI lane (windows-latest).
  Durations cached to test_durations.json (7 files)

=== Per-file subprocess time distribution ===
  Files:   7
  Total subprocess CPU-wall: 28.4s  (runner wall: 28.4s, parallelism: 1x)
  P50: 3.36s  P90: 8.42s  P95: 8.42s  P99: 8.42s  Max: 8.42s
  <1s: 0 files (0%)  <2s: 0 files (0%)
  Top 10 slowest:
      8.42s  tests/tools/test_multi_path_pagination_contract.py
      4.59s  tests/tools/test_file_tools.py
      3.56s  tests/tools/test_file_tools_cwd_resolution.py
      3.36s  tests/tools/test_file_tools_tilde_profile.py
      3.14s  tests/tools/test_file_operations.py
      2.74s  tests/tools/test_file_operations_edge_cases.py
      2.64s  tests/tools/test_file_ops_cwd_tracking.py

```

## Verbatim approved-route-owner-regressions

```text
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 5 test files (~28 tests) under ['tests/ares_runtime/test_authority_review_regressions.py', 'tests/ares_runtime/test_permit_readback.py', 'tests/ares_runtime/test_transport_lease.py', 'tests/ares_runtime/test_transport_wire_bounds.py', 'tests/ares_runtime/test_transport_deadline.py']; running with -j 1
[ 21.4% |     6/~28 | ✓6 | ✗0] ✓ tests/ares_runtime/test_authority_review_regressions.py (6✓, 2.2s)
[ 42.9% |    12/~28 | ✓18 | ✗ 0] ✓ tests/ares_runtime/test_permit_readback.py (12✓, 3.8s)
[ 75.0% |    21/~28 | ✓30 | ✗ 2] ✗ tests/ares_runtime/test_transport_deadline.py (12✓ 2✗ 1s, 10.8s)

  ╔╍ Failed: tests/ares_runtime/test_transport_deadline.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍
  ║ >           run()
  ║ 
  ║ tests/ares_runtime/test_transport_deadline.py:178: 
  ║ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
  ║ 
  ║     def run():
  ║         if provider=='anthropic':
  ║ >           import anthropic
  ║ E           ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_deadline.py:157: ModuleNotFoundError
  ║ __________ test_other_async_providers_share_total_deadline[anthropic] __________
  ║ 
  ║ dripping = ('http://127.0.0.1:37511/v1', [], ['body'])
  ║ monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5eca8bcec0>
  ║ provider = 'anthropic'
  ║ 
  ║     @pytest.mark.asyncio
  ║     @pytest.mark.parametrize('provider',['anthropic','gemini'])
  ║     async def test_other_async_providers_share_total_deadline(dripping,monkeypatch,provider):
  ║         url,bodies,_=dripping;outcomes=govern(monkeypatch,send_timeout_ms=180)
  ║         if provider=='anthropic':
  ║ >           import anthropic
  ║ E           ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_deadline.py:188: ModuleNotFoundError
  ║ =========================== short test summary info ============================
  ║ FAILED tests/ares_runtime/test_transport_deadline.py::test_other_sync_providers_share_total_deadline[anthropic]
  ║ FAILED tests/ares_runtime/test_transport_deadline.py::test_other_async_providers_share_total_deadline[anthropic]
  ║ =================== 2 failed, 12 passed, 1 skipped in 9.77s ====================
  ║
  ║  Repro: python -m pytest tests/ares_runtime/test_transport_deadline.py -v -p no:cacheprovider
  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍

[ 92.9% |    26/~28 | ✓40 | ✗ 5] ✗ tests/ares_runtime/test_transport_lease.py (10✓ 3✗ 1s, 10.8s)

  ╔╍ Failed: tests/ares_runtime/test_transport_lease.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍
  ║ 
  ║ endpoint = ('http://127.0.0.1:33751/v1', [], [])
  ║ monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f18684dc530>
  ║ provider = 'openai'
  ║ 
  ║     @pytest.mark.asyncio
  ║     @pytest.mark.parametrize('provider',['openai','anthropic'])
  ║     async def test_async_sdk_leases(endpoint,monkeypatch,provider):
  ║ >       import httpx,openai,anthropic
  ║ E       ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_lease.py:92: ModuleNotFoundError
  ║ _______________________ test_async_sdk_leases[anthropic] _______________________
  ║ 
  ║ endpoint = ('http://127.0.0.1:37143/v1', [], [])
  ║ monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f1868449190>
  ║ provider = 'anthropic'
  ║ 
  ║     @pytest.mark.asyncio
  ║     @pytest.mark.parametrize('provider',['openai','anthropic'])
  ║     async def test_async_sdk_leases(endpoint,monkeypatch,provider):
  ║ >       import httpx,openai,anthropic
  ║ E       ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_lease.py:92: ModuleNotFoundError
  ║ =========================== short test summary info ============================
  ║ FAILED tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[anthropic]
  ║ FAILED tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[openai]
  ║ FAILED tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[anthropic]
  ║ =================== 3 failed, 10 passed, 1 skipped in 9.68s ====================
  ║
  ║  Repro: python -m pytest tests/ares_runtime/test_transport_lease.py -v -p no:cacheprovider
  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍

[100.0% |    28/~28 | ✓49 | ✗10] ✗ tests/ares_runtime/test_transport_wire_bounds.py (9✓ 5✗, 9.5s)

  ╔╍ Failed: tests/ares_runtime/test_transport_wire_bounds.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍
  ║ 
  ║ endpoint = ('http://127.0.0.1:37539/v1', [], [])
  ║ monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f501a4dba10>
  ║ provider = 'responses'
  ║ 
  ║     @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
  ║     def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
  ║ >       import openai,anthropic
  ║ E       ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
  ║ ______________ test_off_enforce_exact_sdk_body_and_hash[bedrock] _______________
  ║ 
  ║ endpoint = ('http://127.0.0.1:46303/v1', [], [])
  ║ monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5019c60b90>
  ║ provider = 'bedrock'
  ║ 
  ║     @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
  ║     def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
  ║ >       import openai,anthropic
  ║ E       ModuleNotFoundError: No module named 'anthropic'
  ║ 
  ║ tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
  ║ =========================== short test summary info ============================
  ║ FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[openai]
  ║ FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[anthropic]
  ║ FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[gemini]
  ║ FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[responses]
  ║ FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[bedrock]
  ║ ========================= 5 failed, 9 passed in 8.52s ==========================
  ║
  ║  Repro: python -m pytest tests/ares_runtime/test_transport_wire_bounds.py -v -p no:cacheprovider
  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍


=== Summary: 5 files, 49 tests passed, 10 failed, 2 skipped (100% complete) in 37.3s (1 workers) ===
  Durations cached to test_durations.json (5 files)

=== Per-file subprocess time distribution ===
  Files:   5
  Total subprocess CPU-wall: 37.2s  (runner wall: 37.3s, parallelism: 1x)
  P50: 9.50s  P90: 10.84s  P95: 10.84s  P99: 10.84s  Max: 10.84s
  <1s: 0 files (0%)  <2s: 0 files (0%)
  Top 10 slowest:
     10.84s  tests/ares_runtime/test_transport_lease.py
     10.84s  tests/ares_runtime/test_transport_deadline.py
      9.50s  tests/ares_runtime/test_transport_wire_bounds.py
      3.84s  tests/ares_runtime/test_permit_readback.py
      2.22s  tests/ares_runtime/test_authority_review_regressions.py

=== Failure output ===

--- tests/ares_runtime/test_transport_deadline.py ---
============================= test session starts ==============================
platform linux -- Python 3.14.8, pytest-9.1.1, pluggy-1.6.0 -- /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares/.venv/bin/python
rootdir: /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares
configfile: pyproject.toml
plugins: asyncio-1.3.0, anyio-4.12.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 15 items

tests/ares_runtime/test_transport_deadline.py::test_sync_total_deadline_stops_actual_sdk_without_retry[body] PASSED [  6%]
tests/ares_runtime/test_transport_deadline.py::test_sync_total_deadline_stops_actual_sdk_without_retry[headers] PASSED [ 13%]
tests/ares_runtime/test_transport_deadline.py::test_async_total_deadline_stops_actual_sdk_without_retry[body] PASSED [ 20%]
tests/ares_runtime/test_transport_deadline.py::test_async_total_deadline_stops_actual_sdk_without_retry[headers] PASSED [ 26%]
tests/ares_runtime/test_transport_deadline.py::test_async_cancel_settles_once_and_no_retry PASSED [ 33%]
tests/ares_runtime/test_transport_deadline.py::test_dns_owner_is_killed_and_reaped_under_total_deadline[False] PASSED [ 40%]
tests/ares_runtime/test_transport_deadline.py::test_dns_owner_is_killed_and_reaped_under_total_deadline[True] PASSED [ 46%]
tests/ares_runtime/test_transport_deadline.py::test_gemini_async_cancel_closes_send_before_return PASSED [ 53%]
tests/ares_runtime/test_transport_deadline.py::test_other_sync_providers_share_total_deadline[anthropic] FAILED [ 60%]
tests/ares_runtime/test_transport_deadline.py::test_other_sync_providers_share_total_deadline[gemini] PASSED [ 66%]
tests/ares_runtime/test_transport_deadline.py::test_other_sync_providers_share_total_deadline[bedrock] SKIPPED [ 73%]
tests/ares_runtime/test_transport_deadline.py::test_other_async_providers_share_total_deadline[anthropic] FAILED [ 80%]
tests/ares_runtime/test_transport_deadline.py::test_other_async_providers_share_total_deadline[gemini] PASSED [ 86%]
tests/ares_runtime/test_transport_deadline.py::test_shorter_idle_timeout_is_unknown_not_relabelled_total PASSED [ 93%]
tests/ares_runtime/test_transport_deadline.py::test_partial_response_is_unknown_and_never_retried PASSED [100%]

=================================== FAILURES ===================================
__________ test_other_sync_providers_share_total_deadline[anthropic] ___________

dripping = ('http://127.0.0.1:38693/v1', [], ['body'])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5eca380590>
provider = 'anthropic'

    @pytest.mark.parametrize('provider',['anthropic','gemini','bedrock'])
    def test_other_sync_providers_share_total_deadline(dripping,monkeypatch,provider):
        url,bodies,_=dripping;outcomes=govern(monkeypatch,send_timeout_ms=180)
        def run():
            if provider=='anthropic':
                import anthropic
                with bind_inference_client(anthropic.Anthropic(api_key=<REDACTED>,base_url=url,max_retries=2,
                        http_client=httpx.Client(trust_env=False))) as sdk:
                    sdk.messages.create(model='local',messages=[],max_tokens=<REDACTED>
            elif provider=='gemini':
                from agent.gemini_native_adapter import GeminiNativeClient
                with GeminiNativeClient(api_key=<REDACTED>,base_url=url,http_client=httpx.Client(trust_env=False)) as sdk:
                    sdk.chat.completions.create(model='local',messages=[],max_tokens=<REDACTED>
            else:
                boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
                from botocore import UNSIGNED
                from botocore.config import Config
                from ares_runtime.physical_dispatch import bind_bedrock_client
                sdk=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
                    aws_access_key_id='local',aws_secret_access_key=<REDACTED>,
                    config=Config(signature_version=UNSIGNED,retries={'max_attempts':2},proxies={})))
                try:
                    sdk.converse(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
                finally:
                    sdk.close()
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
>           run()

tests/ares_runtime/test_transport_deadline.py:178: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

    def run():
        if provider=='anthropic':
>           import anthropic
E           ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_deadline.py:157: ModuleNotFoundError
__________ test_other_async_providers_share_total_deadline[anthropic] __________

dripping = ('http://127.0.0.1:37511/v1', [], ['body'])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5eca8bcec0>
provider = 'anthropic'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('provider',['anthropic','gemini'])
    async def test_other_async_providers_share_total_deadline(dripping,monkeypatch,provider):
        url,bodies,_=dripping;outcomes=govern(monkeypatch,send_timeout_ms=180)
        if provider=='anthropic':
>           import anthropic
E           ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_deadline.py:188: ModuleNotFoundError
=========================== short test summary info ============================
FAILED tests/ares_runtime/test_transport_deadline.py::test_other_sync_providers_share_total_deadline[anthropic]
FAILED tests/ares_runtime/test_transport_deadline.py::test_other_async_providers_share_total_deadline[anthropic]
=================== 2 failed, 12 passed, 1 skipped in 9.77s ====================

--- tests/ares_runtime/test_transport_lease.py ---
============================= test session starts ==============================
platform linux -- Python 3.14.8, pytest-9.1.1, pluggy-1.6.0 -- /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares/.venv/bin/python
rootdir: /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares
configfile: pyproject.toml
plugins: asyncio-1.3.0, anyio-4.12.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 14 items

tests/ares_runtime/test_transport_lease.py::test_useful_bounded_sync_request_is_admitted_without_body_rewrite PASSED [  7%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides0-INVALID_TRANSPORT_LEASE] PASSED [ 14%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides1-INVALID_TRANSPORT_LEASE] PASSED [ 21%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides2-INVALID_TRANSPORT_LEASE] PASSED [ 28%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides3-OUTPUT_TOKENS_EXCEEDED] PASSED [ 35%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides4-REQUEST_BYTES_EXCEEDED] PASSED [ 42%]
tests/ares_runtime/test_transport_lease.py::test_precise_presend_denial_has_zero_network[overrides5-TRANSPORT_LEASE_EXPIRED] PASSED [ 50%]
tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[anthropic] FAILED [ 57%]
tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[gemini] PASSED [ 64%]
tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[bedrock] SKIPPED [ 71%]
tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[responses] PASSED [ 78%]
tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[openai] FAILED [ 85%]
tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[anthropic] FAILED [ 92%]
tests/ares_runtime/test_transport_lease.py::test_private_network_contract_version_drift_refuses_before_send PASSED [100%]

=================================== FAILURES ===================================
_____________________ test_real_sdk_lease_paths[anthropic] _____________________

endpoint = ('http://127.0.0.1:46621/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f18684feb70>
provider = 'anthropic'

    @pytest.mark.parametrize('provider',['anthropic','gemini','bedrock','responses'])
    def test_real_sdk_lease_paths(endpoint,monkeypatch,provider):
        import httpx
        from ares_runtime.physical_dispatch import bind_bedrock_client,bind_inference_client
        url,bodies,_=endpoint;settles=[]
        monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
            SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
        if provider=='anthropic':
            from agent.anthropic_adapter import build_anthropic_client
>           with build_anthropic_client('local',base_url=url) as sdk:
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests/ares_runtime/test_transport_lease.py:65: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

api_key=<REDACTED>, base_url = 'http://127.0.0.1:46621/v1', timeout = None
drop_context_1m_beta = False

    def build_anthropic_client(
        api_key,
        base_url: str = None,
        timeout: float = None,
        *,
        drop_context_1m_beta: bool = False,
    ):
        """Create an Anthropic client, auto-detecting setup-tokens vs API keys.
    
        ``api_key`` accepts either:
    
        * a static ``str`` — the historical contract for all key-based and
          OAuth flows.
        * a ``Callable[[], str]`` — an Entra ID bearer token provider from
          :mod:`agent.azure_identity_adapter`. The Anthropic SDK itself
          requires a static string, so when given a callable we construct
          a custom ``httpx.Client`` with a request event hook that mints a
          fresh JWT per outbound request and rewrites the ``Authorization``
          header. The SDK never sees the callable directly.
    
        If *timeout* is provided it overrides the default 900s read timeout.  The
        connect timeout stays at 10s.  Callers pass this from the per-provider /
        per-model ``request_timeout_seconds`` config so Anthropic-native and
        Anthropic-compatible providers respect the same knob as OpenAI-wire
        providers.
    
        ``drop_context_1m_beta=True`` strips ``context-1m-2025-08-07`` from the
        client-level ``anthropic-beta`` header. Used by the reactive OAuth retry
        path in ``run_agent.py`` when a subscription rejects the beta; leave at
        its default on fresh clients so 1M-capable subscriptions keep the
        capability.
    
        Returns an anthropic.Anthropic instance.
        """
        _anthropic_sdk = _get_anthropic_sdk()
        if _anthropic_sdk is None:
>           raise ImportError(
                "The 'anthropic' package is required for the Anthropic provider. "
                "Install it with: pip install 'anthropic>=0.39.0'"
            )
E           ImportError: The 'anthropic' package is required for the Anthropic provider. Install it with: pip install 'anthropic>=0.39.0'

agent/anthropic_adapter.py:867: ImportError
________________________ test_async_sdk_leases[openai] _________________________

endpoint = ('http://127.0.0.1:33751/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f18684dc530>
provider = 'openai'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('provider',['openai','anthropic'])
    async def test_async_sdk_leases(endpoint,monkeypatch,provider):
>       import httpx,openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_lease.py:92: ModuleNotFoundError
_______________________ test_async_sdk_leases[anthropic] _______________________

endpoint = ('http://127.0.0.1:37143/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f1868449190>
provider = 'anthropic'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('provider',['openai','anthropic'])
    async def test_async_sdk_leases(endpoint,monkeypatch,provider):
>       import httpx,openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_lease.py:92: ModuleNotFoundError
=========================== short test summary info ============================
FAILED tests/ares_runtime/test_transport_lease.py::test_real_sdk_lease_paths[anthropic]
FAILED tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[openai]
FAILED tests/ares_runtime/test_transport_lease.py::test_async_sdk_leases[anthropic]
=================== 3 failed, 10 passed, 1 skipped in 9.68s ====================

--- tests/ares_runtime/test_transport_wire_bounds.py ---
============================= test session starts ==============================
platform linux -- Python 3.14.8, pytest-9.1.1, pluggy-1.6.0 -- /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares/.venv/bin/python
rootdir: /home/sikmindz/.ares/runs/rsi-remaining-20261112/approved-helper/Ares
configfile: pyproject.toml
plugins: asyncio-1.3.0, anyio-4.12.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 14 items

tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":8}] PASSED [  7%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":7,"max_completion_tokens":7}] PASSED [ 14%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":true}] PASSED [ 21%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":7,"max_tokens":1}] PASSED [ 28%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":7,"n":true}] PASSED [ 35%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":7,"n":2}] PASSED [ 42%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":7.0}] PASSED [ 50%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{}] PASSED [ 57%]
tests/ares_runtime/test_transport_wire_bounds.py::test_invalid_actual_output_field_cannot_be_repaired[{"max_tokens":null}] PASSED [ 64%]
tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[openai] FAILED [ 71%]
tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[anthropic] FAILED [ 78%]
tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[gemini] FAILED [ 85%]
tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[responses] FAILED [ 92%]
tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[bedrock] FAILED [100%]

=================================== FAILURES ===================================
_______________ test_off_enforce_exact_sdk_body_and_hash[openai] _______________

endpoint = ('http://127.0.0.1:44715/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5019be97f0>
provider = 'openai'

    @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
    def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
>       import openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
_____________ test_off_enforce_exact_sdk_body_and_hash[anthropic] ______________

endpoint = ('http://127.0.0.1:41873/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f501a22d7f0>
provider = 'anthropic'

    @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
    def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
>       import openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
_______________ test_off_enforce_exact_sdk_body_and_hash[gemini] _______________

endpoint = ('http://127.0.0.1:42367/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5019b631e0>
provider = 'gemini'

    @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
    def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
>       import openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
_____________ test_off_enforce_exact_sdk_body_and_hash[responses] ______________

endpoint = ('http://127.0.0.1:37539/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f501a4dba10>
provider = 'responses'

    @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
    def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
>       import openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
______________ test_off_enforce_exact_sdk_body_and_hash[bedrock] _______________

endpoint = ('http://127.0.0.1:46303/v1', [], [])
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f5019c60b90>
provider = 'bedrock'

    @pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
    def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
>       import openai,anthropic
E       ModuleNotFoundError: No module named 'anthropic'

tests/ares_runtime/test_transport_wire_bounds.py:34: ModuleNotFoundError
=========================== short test summary info ============================
FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[openai]
FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[anthropic]
FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[gemini]
FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[responses]
FAILED tests/ares_runtime/test_transport_wire_bounds.py::test_off_enforce_exact_sdk_body_and_hash[bedrock]
========================= 5 failed, 9 passed in 8.52s ==========================

=== 3 files with test failures (10 tests failed) ===
  tests/ares_runtime/test_transport_deadline.py  (2 tests failed)
  tests/ares_runtime/test_transport_lease.py  (3 tests failed)
  tests/ares_runtime/test_transport_wire_bounds.py  (5 tests failed)

```

## Verbatim approved-substrate-final

```text

running 12 tests
test tests::check_result_requires_all_checks_to_pass ... ok
test tests::collect_logs_preserves_outputs_and_timings ... ok
test tests::env_allowlist_accepts_safe_prefixes_and_rejects_unknowns ... ok
test tests::host_backend_clears_disallowed_environment_variables ... ok
test tests::host_backend_run_command_times_out ... ok
test tests::host_backend_timeout_kills_spawned_child_process_group ... ok
test tests::select_backend_rejects_host_in_sealed_mode_without_opt_in ... ok
test tests::select_backend_returns_host_when_allowed ... ok
test wave1_tests::check_result_all_pass_requires_every_check ... ok
test wave1_tests::host_backend_collect_logs_preserves_outputs_and_timings ... ok
test wave1_tests::select_backend_falls_back_to_host_when_allowed ... ignored, environment-dependent: fails when docker/podman is available because auto→container succeeds before host fallback
test wave1_tests::select_backend_rejects_unapproved_host_fallback_in_sealed_mode ... ok

test result: ok. 11 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 2.16s


running 1 test
test cancellation_stops_owned_host_descendant ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s


running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 2 tests
test forge_engine_docs_state_the_authority_boundary ... ok
test forge_engine_manifest_pins_local_authority_boundary ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 14 tests
test attribute_effects_uses_mapped_positions ... ok
test i1_cea_instrumentation_extracts_effect_signatures ... ok
test i2_cea_edit_op_signature_no_raw_source ... ok
test i3_cea_graph_update_is_idempotent ... ok
test i3b_cea_graph_update_is_atomic_when_run_log_insert_fails ... ok
test i3c_cea_graph_update_is_atomic_when_run_log_insert_fails_with_store_conn ... ok
test i3d_cea_graph_update_is_atomic_with_store_transaction_connection ... ok
test i3e_cea_graph_load_graph_works_with_store_transaction_connection ... ok
test i3f_cea_graph_restart_preserves_edge_stats_and_predictions ... ok
test i3g_cea_update_graph_persists_negative_evidence_for_absent_effects ... ok
test i3h_cea_restart_preserves_negative_evidence_predictions ... ok
test i3i_cea_legacy_edge_rows_are_migrated_forward_with_stats ... ok
test i4_cea_prediction_returns_neutral_for_unknown_sigs ... ok
test i5_cea_zero_shot_requires_explicit_enable ... ok

test result: ok. 14 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.05s


running 12 tests
test backward_compat_old_json_without_new_fields ... ok
test backward_compat_old_status_names ... ok
test canonical_bundle_roundtrip_preserves_authoritative_contract ... ok
test claim_strength_display ... ok
test confidence_bounds_on_hypothesis ... ok
test episode_conversion_from_evidence_bundle ... ok
test episode_meta_and_content_capture_verification_artifacts ... ok
test forge_limits_defaults ... ok
test local_experiment_runner_construction ... ok
test sealed_bundle_immutability ... ok
test serialization_round_trip_evidence_bundle ... ok
test verification_type_variants ... ok

test result: ok. 12 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 8 tests
test copy_dir_recursive_skips_symlinks ... ok
test env_allowlist_blocks_secrets_passes_cargo ... ok
test f1_host_backend_runs_command_captures_output ... ok
test f2_host_backend_timeout ... ok
test f4_container_backend_autodetect_fallback_host ... ok
test f5_container_backend_sealed_requires_no_network ... ok
test g1_cargo_adapter_detects_cargo_project ... ok
test g3_cargo_adapter_parses_clippy_json ... ok

test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.06s


running 31 tests
test baseline_descriptor_fingerprint_deterministic ... ok
test build_hypothesis_edges_from_diff ... ok
test comparability_class_filtering ... ok
test compute_confidence_basic ... ok
test derive_status_from_counts ... ok
test evidence_assessment_categories ... ok
test experiment_config_defaults ... ok
test experiment_diff_from_all_pass_both ... ok
test experiment_diff_from_baseline_fail_patched_pass ... ok
test experiment_diff_from_baseline_pass_patched_fail ... ok
test experiment_diff_single_pair_not_statistically_meaningful ... ok
test experiment_diff_stable_failures_are_neutral ... ok
test experiment_diff_with_detailed_effects ... ok
test local_experiment_runner_does_not_fabricate ... ok
test multiple_effects_one_primary_claim ... ok
test objective_policy_bug_fix ... ok
test objective_policy_performance_no_scalarization ... ok
test objective_policy_stability_excluded_insufficient_trials ... ok
test pair_comparability_timeout_asymmetry ... ok
test pair_comparability_valid ... ok
test pair_comparability_workload_mismatch ... ok
test patch_execution_plan_cargo_default ... ok
test receipt_content_hash_mismatch_detected ... ok
test receipt_content_hash_verification ... ok
test update_hypotheses_from_diff_new_effect ... ok
test update_hypotheses_stable_failure_is_neutral ... ok
test verification_plan_basic_generation ... ok
test verification_plan_has_budget ... ok
test verification_plan_respects_step_limit ... ok
test verification_plan_with_ablation ... ok
test workspace_policy_defaults ... ok

test result: ok. 31 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 27 tests
test already_exported_check ... ok
test caller_managed_and_helper_produce_identical_payloads ... ok
test claim_and_relation_records_carry_stable_version_ids_and_bundle_valid_time ... ok
test claim_metadata_carries_projected_verification_summary ... ok
test claim_record_carries_supersedes_claim_version_id_when_present ... ok
test claim_record_does_not_synthesize_claim_lineage ... ok
test claim_semantics_intentionally_group_without_constraint_seed_kind ... ok
test episode_export_builds_canonical_forge_envelope ... ok
test episode_export_builds_canonical_forge_v2_envelope ... ok
test episode_export_from_bundle_populates_all_fields ... ok
test evidence_refs_bind_to_exported_claim_version ... ok
test export_bundle_idempotent_skip ... ok
test export_bundle_projects_store_promotions_into_claim_metadata ... ok
test export_bundle_returns_canonical_v3_envelope ... ok
test export_includes_experiment_diff_relations ... ok
test export_includes_receipt_evidence_refs ... ok
test export_includes_verification_trials_and_refutation_artifacts ... ok
test export_key_is_deterministic ... ok
test export_key_varies_with_inputs ... ok
test export_receipt_idempotent ... ok
test export_receipt_with_write_through_status ... ok
test export_with_hypothesis_edges_includes_relations ... ok
test export_without_attempt_or_run_keeps_single_evidence_ref ... ok
test relation_records_carry_supersedes_relation_version_id_when_hinted ... ok
test relation_records_do_not_synthesize_relation_lineage_when_unknown ... ok
test rendering_version_is_three ... ok
test to_export_envelope_v3_survives_bridge_transform_and_memory_import ... ok

test result: ok. 27 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.16s


running 6 tests
test h1_evaluate_one_candidate_one_task_mocked_patch ... ok
test h2_archive_insert_replaces_lower_score ... ok
test h3_archive_insert_below_gate_discarded ... ok
test h4_archive_preserves_higher_score ... ok
test h5_raw_scoring_model ... ok
test h6_anti_fluff_guard ... ok

test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.03s


running 18 tests
test evidence_bundle_crud ... ok
test evidence_bundle_insert_or_replace ... ok
test evidence_bundle_not_found ... ok
test experiment_run_insert ... ok
test export_receipt_insert_and_check ... ok
test failure_class_from_error ... ok
test failure_class_retriability ... ok
test failure_class_serde_round_trip ... ok
test fresh_db_has_v2_and_v3_tables ... ok
test legacy_split_only_evidence_row_rebuilds_canonical_bundle ... ok
test migration_v2_idempotent_reopen ... ok
test run_failure_empty_for_unknown_run ... ok
test run_failure_insert_and_query ... ok
test schema_hash_is_stable ... ok
test schema_hash_matches_forge_schema_hash ... ok
test user_version_is_v4_after_open ... ok
test v1_db_upgraded_to_latest ... ok
test verification_plan_insert ... ok

test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.07s


running 22 tests
test c1_patch_rejects_forbidden_path_tests ... ok
test c2_patch_rejects_forbidden_path_snap ... ok
test c3_patch_rejects_cap_files ... ok
test c4_patch_rejects_cap_total_lines ... ok
test c5_patch_accepts_well_formed ... ok
test c6_patch_validation_returns_all_violations ... ok
test c7_patch_rejects_path_traversal ... ok
test d1_apply_insert_after_line ... ok
test d2_apply_replace_range ... ok
test d3_apply_delete_range ... ok
test d4_apply_match_anchor ... ok
test d5_apply_fails_on_ambiguous_context ... ok
test d6_apply_is_atomic ... ok
test d7_apply_returns_line_attribution_map ... ok
test d8_apply_rejects_path_traversal ... ok
test e1_diff_render_produces_valid_unified_format ... ok
test e2_diff_render_is_stable ... ok
test e4_diff_fallback_renders_correctly ... ok
test insert_after_line_1_does_not_shift_line_1_mapping ... ok
test negative_offset_returns_error_not_wrap ... ok
test patch_rejects_symlink_directory_in_workspace ... ok
test patch_rejects_symlink_file_in_workspace ... ok

test result: ok. 22 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s


running 24 tests
test attempt_id_is_not_trace_id ... ok
test baseline_timeout_patched_success_invalid ... ok
test bundle_has_all_required_fields ... ok
test bundle_serialization_round_trip_with_phase5_fields ... ok
test causal_question_format ... ok
test covariates_has_all_required_fields ... ok
test derive_status_phase5_semantics ... ok
test episode_content_includes_required_fields ... ok
test episode_meta_includes_phase5_fields ... ok
test forge_limits_has_patch_and_graph_limits ... ok
test hypothesis_edge_fields ... ok
test hypothesis_edge_kinds_complete ... ok
test pair_comparability_backend_mismatch ... ok
test pair_comparability_checks_mismatch ... ok
test pair_comparability_config_flags_mismatch ... ok
test patched_timeout_baseline_success_is_failure_outcome ... ok
test receipt_kind_variants ... ok
test receipt_ref_has_required_fields ... ok
test receipt_storage_variants ... ok
test sealed_bundle_blocks_reseal ... ok
test sealed_bundle_check_not_sealed ... ok
test stable_failure_edge_has_zero_confidence ... ok
test unit_definition_describes_paired_run ... ok
test verification_plan_records_dropped_steps ... ok

test result: ok. 24 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 5 tests
test promotion_reads_assessment_from_canonical_bundle_not_legacy_split_column ... ok
test promotion_rejects_contradicted_evidence_assessment ... ok
test promotion_rejects_missing_verification_evidence ... ok
test promotion_rejects_when_improvement_does_not_clear_baseline ... ok
test promotion_succeeds_with_deterministic_eval_and_evidence_gate ... ok

test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.03s


running 4 tests
test candidate_cannot_rewrite_python_evaluator_or_build_manifests ... ok
test empty_python_suite_cannot_be_a_passing_experiment ... ok
test python_tools_are_explicit_and_bad_summaries_refuse ... ok
test real_python_pair_corrects_failure_without_installing_dependencies ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.48s


running 8 tests
test actual_timeout_retains_completed_and_interrupted_sides_only ... ok
test empty_check_plan_cannot_be_a_passing_experiment ... ok
test empty_or_ignored_only_test_execution_is_not_a_passing_check ... ok
test invalid_modes_counts_and_unqualified_timing_refuse_before_execution ... ok
test patch_policy_refuses_before_workspace_or_commands ... ok
test repeated_pairs_execute_every_side_in_fresh_workspaces ... ok
test symlink_source_refuses_before_backend_dispatch ... ok
test within_policy_patch_runs_real_baseline_and_candidate ... ok

test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 4.75s


running 3 tests
test b1_mindstate_render_is_deterministic ... ok
test b2_mindstate_snapshot ... ok
test mindstate_render_returns_result ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 8 tests
test a1_refuse_to_open_non_forge_db ... ok
test a2_refuse_to_open_missing_forge_meta ... ok
test a3_refuse_to_open_wrong_schema_hash ... ok
test a4_refuse_to_open_semantic_memory_signature ... ok
test a5_forge_writes_only_to_forge_db ... ok
test a6_refuse_to_open_reads_only_magic_bytes ... ok
test forge_db_round_trip ... ok
test transaction_commit_and_rollback ... ok

test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.02s


running 1 test
test forge_tool_receipt_sink_persists_raw_receipt ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 25 tests
test fixture_map::tests::empty_workspace_returns_no_fixtures ... ok
test fixture_map::tests::extract_package_name_parses_toml ... ok
test fixture_map::tests::resolve_signature_finds_crate ... ok
test governance_gate::tests::active_incident_blocks_execution ... ok
test governance_gate::tests::default_observation_blocks_execution ... ok
test governance_gate::tests::degradation_blocks_promotion_downgrades_to_advisory ... ok
test governance_gate::tests::fail_open_mode_returns_blocked_without_error ... ok
test governance_gate::tests::fail_open_mode_returns_default_on_empty_store ... ok
test governance_gate::tests::governance_receipt_roundtrip ... ok
test governance_gate::tests::invalid_authority_with_preflight_blocks ... ok
test governance_gate::tests::invalid_authority_without_preflight_allows ... ok
test governance_gate::tests::non_blocking_degradation_allows_execution ... ok
test governance_gate::tests::observe_governance_active_incident_blocks ... ok
test governance_gate::tests::observe_governance_empty_store_returns_error ... ok
test governance_gate::tests::observe_governance_reads_real_artifacts ... ok
test governance_gate::tests::observed_empty_observation_allows_execution ... ok
test governance_gate::tests::parse_bool_claim_values ... ok
test governance_gate::tests::pending_amendment_downgrades_to_advisory ... ok
test governance_gate::tests::strict_mode_allows_advisory_only ... ok
test governance_gate::tests::strict_mode_allows_on_allow_gate_result ... ok
test governance_gate::tests::strict_mode_allows_populated_store ... ok
test governance_gate::tests::strict_mode_blocks_on_blocked_gate_result ... ok
test governance_gate::tests::strict_mode_errors_on_empty_store ... ok
test receipts::tests::repair_record_preserves_variant_specific_semantics ... ok
test receipts::tests::verification_plan_artifact_carries_replay_recipe_and_blockers ... ok

test result: ok. 25 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.61s


running 5 tests
test main_support::tests::detect_project_root_walks_up_from_nested_source_folder ... ok
test main_support::tests::managed_storage_root_prefers_project_root_over_source_subdir ... ok
test main_support::tests::normalize_user_path_expands_tilde ... ok
test main_support::tests::normalize_user_path_makes_relative_paths_absolute ... ok
test main_support::tests::prepare_runtime_layout_moves_managed_defaults_out_of_source_tree ... ok

test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 1 test
test local_edit_preserves_unaffected_chunk_ids_when_practical ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.32s


running 1 test
test bootstrap_source_creates_deterministic_chunk_records ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.19s


running 1 test
test bootstrap_source_deletions_are_manifest_driven_and_non_destructive ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 1 test
test bootstrap_source_delta_reimports_only_changed_local_subtree ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 1 test
test observation_reports_bootstrap_richness_across_thin_chunked_and_symbolized_states ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.54s


running 1 test
test bootstrap_source_marks_rust_line_scanner_as_degraded_for_unsupported_surface ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.11s


running 4 tests
test bootstrap_source_imports_workspace_into_canonical_observation_state ... ok
test bootstrap_source_is_idempotent_for_unchanged_workspace ... ok
test bootstrap_source_reports_empty_scan_cleanly ... ok
test bootstrap_source_reports_invalid_utf8_skips ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.47s


running 1 test
test bootstrap_source_extracts_symbols_for_rust_typescript_python_and_generic ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 2 tests
test derived_policy_changes_do_not_count_as_source_changes ... ok
test observation_current_state_comes_from_latest_manifest_only ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.35s


running 3 tests
test decision_audit_records_cheapest_and_blocked_steps_deterministically ... ok
test loop_iteration_report_carries_normalization_and_lineage_receipts ... ok
test target_taxonomy_normalizes_to_canonical_case_classes ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.35s


running 3 tests
test loop_runner_bounds_repeated_degraded_thin_export_cycles ... ok
test observation_degrades_honestly_when_kernel_payload_is_missing ... ok
test thin_export_yields_explicit_target_instead_of_invented_structure ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.39s


running 3 tests
test missing_oracle_outcome_cannot_fabricate_support_pair_or_completed_trial ... ok
test no_op_and_incomparable_results_do_not_gain_support ... ok
test repeated_raw_trials_and_exact_patch_identity_survive_bundle_and_v3_export ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 3.02s


running 1 test
test tool_runtime_to_pilot_to_control_lineage_stays_coherent ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 8 tests
test corrupt_forge_db_is_reported_explicitly ... ok
test empty_workspace_stays_empty_instead_of_forcing_import ... ok
test forge_bundles_without_imports_surface_exact_import_command ... ok
test imported_scope_remains_ready ... ok
test missing_storage_is_reported_explicitly ... ok
test namespace_mismatch_reports_dedicated_status_for_requested_namespace ... ok
test pilot_can_bootstrap_memory_from_recent_forge_bundles ... ok
test source_files_without_imports_surface_import_required ... ok

test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.95s


running 1 test
test loop_runner_stops_when_time_budget_is_exhausted ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.13s


running 6 tests
test canonical_roundtrip_records_durable_failure_receipt_when_import_breaks ... ok
test loop_runner_executes_oracle_and_patch_families_with_real_roundtrips ... ok
test observation_only_accepts_explicit_paths ... ok
test observation_only_cannot_escape_into_execution_or_canonical_import ... ok
test observation_only_mode_never_executes_the_oracle_family ... ok
test observation_only_refuses_ambient_default_paths ... ok

test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.86s


running 1 test
test reconstructs_observation_from_public_runtime_and_import_surfaces ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 2 tests
test exact_oracle_slice_must_match_the_owner_selected_slice ... ok
test oracle_plan_executes_and_roundtrips_through_canonical_v3_lane ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.28s


running 2 tests
test full_loop_missing_or_inapplicable_policy_refuses_instead_of_falling_back ... ok
test loop_defaults_do_not_mint_a_permissive_policy ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.48s


running 1 test
test repo_chat_ownership_answers_abstain_honestly ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 1 test
test repo_chat_change_questions_use_latest_manifest_delta ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.14s


running 1 test
test repo_chat_routes_questions_explicitly ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 3 tests
test repo_chat_abstains_when_imported_source_memory_is_missing ... ok
test repo_chat_answers_with_grounded_workspace_source_citations ... ok
test repo_chat_filters_historical_imports_out_of_current_state_answers ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.37s


running 2 tests
test scoring_and_ordering_are_deterministic_and_retry_decay_applies ... ok
test target_selection_is_compared_against_a_simple_deterministic_baseline ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.24s


running 7 tests
test full_loop_accepts_scoped_approval_record ... ok
test full_loop_emits_v9_plan_and_consumer_only_proof_artifacts ... ok
test full_loop_policy_deny_blocks_execution_before_action ... ok
test full_loop_refuted_promoted_state_triggers_rollback_invalidation ... ok
test full_loop_requires_approval_when_policy_demands_it ... ok
test full_loop_uncalibrated_path_forces_advisory_only ... ok
test loop_report_boundary_parser_repairs_missing_approval_records ... ok

test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.60s


running 7 tests
test tests::env_allowlist_matches_expected_prefixes ... ok
test tests::path_guard_rejects_absolute_and_parent_components ... ok
test tests::path_guard_rejects_multiple_escape_shapes ... ok
test tests::reject_symlinks_blocks_existing_symlink_segments ... ok
test tests::relative_path_guard_accepts_safe_relative_shapes ... ok
test tests::relative_path_guard_rejects_parent_escape_shapes ... ok
test tests::resolve_workspace_path_rejects_symlinked_parent_escape ... ok

test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.05s


running 11 tests
test tests::create_parent_dirs_rejects_nested_escape_attempts ... ok
test tests::local_patch_fs_rejects_escape_paths ... ok
test tests::local_patch_fs_rejects_generated_escape_paths ... ok
test tests::local_patch_fs_rejects_reads_and_writes_through_symlink_segments ... ok
test tests::local_patch_fs_round_trips_file_contents ... ok
test tests::local_patch_fs_round_trips_safe_relative_paths ... ok
test tests::prepare_workspace_copies_fixture_recursively ... ok
test tests::prepare_workspace_skips_symlink_entries ... ok
test wave1_tests::local_patch_fs_rejects_escape_paths ... ok
test wave1_tests::local_patch_fs_round_trips_and_removes_files ... ok
test wave1_tests::prepare_workspace_copies_fixture_tree ... ok

test result: ok. 11 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.14s


running 6 tests
test tests::apply_patch_rolls_back_when_a_later_edit_fails ... ok
test tests::idempotent_replace_patch_can_be_applied_twice ... ok
test tests::idempotent_replace_patch_remains_stable_for_arbitrary_line ... ok
test tests::parent_dir_paths_are_always_rejected ... ok
test tests::strategy_helpers_count_lines ... ok
test tests::validate_patch_rejects_workspace_escape_and_invalid_occurrence ... ok

test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s


running 4 tests
test tests::denied_method_cannot_execute ... ok
test tests::duplicate_policy_vocab_is_rejected ... ok
test tests::scoped_approval_can_satisfy_runtime_generated_case_ids ... ok
test tests::v21_v24_policy_profiles_roundtrip ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 3 tests
test approval_window_uses_instants_and_a_half_open_interval ... ok
test future_malformed_and_reversed_approval_windows_refuse ... ok
test invalid_approval_metadata_cannot_authorize_a_matching_scope ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 3 tests
test changed_plan_or_case_with_reused_ids_cannot_consume_decision ... ok
test modifying_public_decision_flags_cannot_turn_denial_into_permission ... ok
test policy_report_roundtrip_is_projection_not_execution_authority ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 4 tests
test continuity_policy_profile_v1_roundtrips ... ok
test delegation_policy_profile_v1_roundtrips ... ok
test effect_policy_profile_v1_roundtrips ... ok
test release_policy_profile_v1_roundtrips ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 3 tests
test invalid_snapshot_metadata_and_invalid_query_time_cannot_select_authority ... ok
test missing_future_expired_malformed_and_reversed_policies_do_not_apply ... ok
test temporal_policy_selection_compares_instants_not_wire_spelling ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 4 tests
test access_purpose_matrix_v1_roundtrips ... ok
test audit_extraction_policy_v1_roundtrips ... ok
test privacy_retention_profile_v1_roundtrips ... ok
test redaction_rule_set_v1_roundtrips ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 1 test
test profile_p1_fixture_bundles_parse_into_owned_types ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 4 tests
test cross_boundary_transfer_class_v1_roundtrips ... ok
test locality_exception_v1_roundtrips ... ok
test residency_policy_profile_v1_roundtrips ... ok
test tenant_boundary_profile_v1_roundtrips ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 1 test
test profile_p2_fixture_bundles_parse_into_owned_types ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 1 test
test v14_examples_roundtrip_and_validate ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 3 tests
test advisory_only_plan_cannot_mint_execution_permit ... ok
test approval_records_influence_admission_requirement ... ok
test policy_decision_carries_v25_citation_shape ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

warning: profiles for the non root package will be ignored, specify profiles at the workspace root:
package:   /home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/quant-governor/Cargo.toml
workspace: /home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/Cargo.toml
   Compiling verification-policy v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/verification-policy)
   Compiling verification-adjudication v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/verification-adjudication)
   Compiling forge-pilot v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/forge-pilot)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 31.53s
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/check_runner-e97138040d26273b)
     Running tests/cancellation_tests.rs (/tmp/audit/rust-target/debug/deps/cancellation_tests-4d3ffe0107ec0a75)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/forge_engine-ad3a094d10e1ed08)
     Running tests/authority_boundary_tests.rs (/tmp/audit/rust-target/debug/deps/authority_boundary_tests-7c94e0141aaf0913)
     Running tests/cea_tests.rs (/tmp/audit/rust-target/debug/deps/cea_tests-e64f33acac5be77d)
     Running tests/evidence_tests.rs (/tmp/audit/rust-target/debug/deps/evidence_tests-afe0d748ba43a3bd)
     Running tests/exec_tests.rs (/tmp/audit/rust-target/debug/deps/exec_tests-f94d1cba4ed56879)
     Running tests/experiment_tests.rs (/tmp/audit/rust-target/debug/deps/experiment_tests-d695c28119c6a0b3)
     Running tests/export_tests.rs (/tmp/audit/rust-target/debug/deps/export_tests-559d53f14ab7fc91)
     Running tests/lab_tests.rs (/tmp/audit/rust-target/debug/deps/lab_tests-23f7bc4ba8d60d3f)
     Running tests/migration_tests.rs (/tmp/audit/rust-target/debug/deps/migration_tests-a8a5a89e624c1a1f)
     Running tests/patch_tests.rs (/tmp/audit/rust-target/debug/deps/patch_tests-615086417d05e453)
     Running tests/phase5_tests.rs (/tmp/audit/rust-target/debug/deps/phase5_tests-60242e5f93c5a69f)
     Running tests/promotion_tests.rs (/tmp/audit/rust-target/debug/deps/promotion_tests-b5f01858b1c12728)
     Running tests/python_adapter_tests.rs (/tmp/audit/rust-target/debug/deps/python_adapter_tests-4a0b54a64c52829a)
     Running tests/rsi_contract_tests.rs (/tmp/audit/rust-target/debug/deps/rsi_contract_tests-af04cc9103944bbd)
     Running tests/runtime_tests.rs (/tmp/audit/rust-target/debug/deps/runtime_tests-8be41a2c8c338f0e)
     Running tests/safety_tests.rs (/tmp/audit/rust-target/debug/deps/safety_tests-ce938e66b97515ee)
     Running tests/tool_receipt_sink_tests.rs (/tmp/audit/rust-target/debug/deps/tool_receipt_sink_tests-fd579ed50a04de12)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/forge_pilot-fa7a2266b44566d7)
     Running unittests src/main.rs (/tmp/audit/rust-target/debug/deps/forge_pilot-4c1ece5249150260)
     Running tests/bootstrap_chunk_identity_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_chunk_identity_tests-d980db53aa306a20)
     Running tests/bootstrap_chunk_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_chunk_tests-14595833c53fcba7)
     Running tests/bootstrap_delete_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_delete_tests-d17af9f0fc6ad3f0)
     Running tests/bootstrap_delta_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_delta_tests-070df0d711f2de85)
     Running tests/bootstrap_observation_richness_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_observation_richness_tests-793d033cd1d056c6)
     Running tests/bootstrap_rust_extractor_degradation_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_rust_extractor_degradation_tests-9b3709650690143e)
     Running tests/bootstrap_source_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_source_tests-f72e424e0a17f102)
     Running tests/bootstrap_symbol_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_symbol_tests-7fd1b3ec7152ee8a)
     Running tests/bootstrap_truth_tests.rs (/tmp/audit/rust-target/debug/deps/bootstrap_truth_tests-af53efccd020c01a)
     Running tests/closed_loop_normalization_tests.rs (/tmp/audit/rust-target/debug/deps/closed_loop_normalization_tests-d77cee78257a2dc5)
     Running tests/degradation_tests.rs (/tmp/audit/rust-target/debug/deps/degradation_tests-919d67343b175fdf)
     Running tests/evidence_honesty_tests.rs (/tmp/audit/rust-target/debug/deps/evidence_honesty_tests-dda474cf320cd439)
     Running tests/execution_evidence_lineage_tests.rs (/tmp/audit/rust-target/debug/deps/execution_evidence_lineage_tests-43c532ae1f9c6fe6)
     Running tests/import_requirement_tests.rs (/tmp/audit/rust-target/debug/deps/import_requirement_tests-1f0991de61b7b22f)
     Running tests/loop_halt_tests.rs (/tmp/audit/rust-target/debug/deps/loop_halt_tests-65230bd8ada88301)
     Running tests/loop_roundtrip_tests.rs (/tmp/audit/rust-target/debug/deps/loop_roundtrip_tests-08d0bd087c57f0c4)
     Running tests/observation_fixture_tests.rs (/tmp/audit/rust-target/debug/deps/observation_fixture_tests-0a939ef3a964d4b6)
     Running tests/oracle_plan_tests.rs (/tmp/audit/rust-target/debug/deps/oracle_plan_tests-a33cffe25f4bfb4c)
     Running tests/policy_admission_tests.rs (/tmp/audit/rust-target/debug/deps/policy_admission_tests-141572ca0e83fb69)
     Running tests/repo_chat_abstention_tests.rs (/tmp/audit/rust-target/debug/deps/repo_chat_abstention_tests-77eb34eb210f48a6)
     Running tests/repo_chat_change_tests.rs (/tmp/audit/rust-target/debug/deps/repo_chat_change_tests-d46a9b61034af9ee)
     Running tests/repo_chat_routing_tests.rs (/tmp/audit/rust-target/debug/deps/repo_chat_routing_tests-86dc864af59df8c5)
     Running tests/repo_chat_tests.rs (/tmp/audit/rust-target/debug/deps/repo_chat_tests-d8edb8f63d9307aa)
     Running tests/scoring_tests.rs (/tmp/audit/rust-target/debug/deps/scoring_tests-5564fb0e1a68794d)
     Running tests/verification_control_tests.rs (/tmp/audit/rust-target/debug/deps/verification_control_tests-e6bf4c19825f03c9)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/forge_policy-ac1a3028864e4451)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/sandbox_workspace-cb098cd6929029ab)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/typed_patch-e3206b40402ad4a4)
     Running unittests src/lib.rs (/tmp/audit/rust-target/debug/deps/verification_policy-bcd5012da34869a9)
     Running tests/approval_time_tests.rs (/tmp/audit/rust-target/debug/deps/approval_time_tests-73741e5f8d6cf5c7)
     Running tests/execution_binding_tests.rs (/tmp/audit/rust-target/debug/deps/execution_binding_tests-60d47c537f83e641)
     Running tests/policy_profile_example_roundtrip.rs (/tmp/audit/rust-target/debug/deps/policy_profile_example_roundtrip-298586d1fd8547ee)
     Running tests/policy_time_tests.rs (/tmp/audit/rust-target/debug/deps/policy_time_tests-5ec6450f0b8892e6)
     Running tests/profile_p1_example_roundtrip.rs (/tmp/audit/rust-target/debug/deps/profile_p1_example_roundtrip-958f4125072df677)
     Running tests/profile_p1_fixture_conformance.rs (/tmp/audit/rust-target/debug/deps/profile_p1_fixture_conformance-9679a89ffe5ce989)
     Running tests/profile_p2_example_roundtrip.rs (/tmp/audit/rust-target/debug/deps/profile_p2_example_roundtrip-e1140d5b66d87a7b)
     Running tests/profile_p2_fixture_conformance.rs (/tmp/audit/rust-target/debug/deps/profile_p2_fixture_conformance-14d10bdcf2478a6d)
     Running tests/v14_example_roundtrip.rs (/tmp/audit/rust-target/debug/deps/v14_example_roundtrip-f0f8c866176b0481)
     Running tests/v25_policy_citation_flow.rs (/tmp/audit/rust-target/debug/deps/v25_policy_citation_flow-d6a101a33e40adc3)

```

## Verbatim approved-clippy-final

```text
warning: profiles for the non root package will be ignored, specify profiles at the workspace root:
package:   /home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/quant-governor/Cargo.toml
workspace: /home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/Cargo.toml
    Checking verification-policy v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/verification-policy)
    Checking verification-adjudication v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/verification-adjudication)
    Checking forge-pilot v0.1.0 (/home/sikmindz/.ares/runs/rsi-tranche-20261112T020205-0600/Libraries/forge-pilot)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 7.26s

```
