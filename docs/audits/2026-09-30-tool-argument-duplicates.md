# Duplicate tool-argument rejection: review record

## Bounded behavior

Model-emitted tool arguments must be JSON objects. PR77 adds duplicate-key rejection before JSON objects are collapsed into dictionaries, including nested objects, objects inside arrays and equivalent escaped key spellings. A valid sibling tool call retains normal dispatch. It does not authorize a tool, change permits or certify every other numeric/JSON boundary.

## Source evidence

The current-main parser at 14ac9e3a00ee60f811772c77addf65f27572f2db accepted four duplicate-key counterexamples. Direct execution of the same parser owner with the proposed hook rejected all four and still accepted a valid object. The repository's existing sequential/concurrent dispatch regression was extended with those four cases. The direct parser probe is narrower than full agent dispatch; exact-head hosted tests remain the delivery gate.

The refresh preserves both original PR77 ancestry and current main. Only agent/tool_executor.py and tests/run_agent/test_malformed_tool_arguments.py change product/test behavior. This review record adds documentation without changing runtime semantics.

## Preserved failures and remaining gates

GitHub initially retained the older PR base 996ffac2595f2d279f3394ac22ee88486a920ddd, causing the first refreshed CI event to classify 240 files already on main. Explicitly refreshing the main target corrected the base to 14ac9e3 and restored the actual two-file code/test diff. No ci-reviewed label or gate waiver was added. The original CI run 36737830354 is retained; fresh events must use the corrected base.

The paired native-owner qualification run 36737828471 also failed scoped_daemon_retirement_lost_ack_restart_and_old_verifier_fence with 'retirement did not persist'. It tests an explicitly patched recursive-agent candidate 2297e9444922ff0aef1eb21358084dbfe3e13ec4, not the unpatched checkout base. The test closes its socket without reading the ACK, then polls durable transition state. Source inspection alone does not establish why this run failed or whether it is a runtime or fixture problem. One same-source job replay was requested as diagnosis; a later pass alone does not mean the failure was repaired. This finding remains documented and must be reconciled before claiming merge readiness.

## Rollback and limits

Revert the isolated duplicate-key parser/test change to roll back; do not revert unrelated current-main work. No installed runtime, host permissions, operator keys, package release or deployment was changed. Required checks must remain enabled, and failed or unobserved relevant gates must be reported rather than relabeled as success.

## Readiness reconciliation (2026-09-30)

Independent review of main 14ac9e3 to candidate d662df28 confirmed that only the parser, its regression cases and this audit record changed. The native test executes inside the patched recursive-agent workspace before Ares is installed or executed in that job; this Python parser is not on that failure path. The earlier timeout remains unattributed reliability debt, not a repaired runtime defect.

The requested same-source replay was cancelled when the newer documentation push started; it is not passing evidence. Fresh qualification run 36739843322 passed, and native job 109971544210 explicitly passed the retirement test. The other three exact-head workflows also passed. Python slice 4 job 109971311077 executed all 18 malformed-argument dispatch cases successfully. These observations, together with independent parser review, support the isolated duplicate-key repair; they do not establish that the intermittent native failure has been fixed. Any subsequent source revision still requires its own applicable CI before merge. See PR #77 for final delivery and post-merge status.
