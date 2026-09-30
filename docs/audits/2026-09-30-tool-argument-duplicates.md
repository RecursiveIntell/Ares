# Duplicate tool-argument rejection: review record

## Bounded behavior

Model-emitted tool arguments must be JSON objects. PR77 adds duplicate-key rejection before JSON objects are collapsed into dictionaries, including nested objects, objects inside arrays and equivalent escaped key spellings. A valid sibling tool call retains normal dispatch. It does not authorize a tool, change permits or certify every other numeric/JSON boundary.

## Source evidence

The current-main parser at 14ac9e3a00ee60f811772c77addf65f27572f2db accepted four duplicate-key counterexamples. Direct execution of the same parser owner with the proposed hook rejected all four and still accepted a valid object. The repository's existing sequential/concurrent dispatch regression was extended with those four cases. The direct parser probe is narrower than full agent dispatch; exact-head hosted tests remain the delivery gate.

The refresh preserves both original PR77 ancestry and current main. Only agent/tool_executor.py and tests/run_agent/test_malformed_tool_arguments.py change product/test behavior. This review record adds documentation without changing runtime semantics.

## Preserved failures and remaining gates

GitHub initially retained the older PR base996ffac2595f2d279f3394ac22ee88486a920ddd, causing the first refreshed CI event to classify240 files already on main. Explicitly refreshing the main target corrected the base to14ac9e3 and restored the actual two-file code/test diff. No ci-reviewed label or gate waiver was added. The original CI run36737830354 is retained; fresh events must use the corrected base.

The paired native-owner qualification run36737828471 also failed scoped_daemon_retirement_lost_ack_restart_and_old_verifier_fence with 'retirement did not persist'. It tests an explicitly patched recursive-agent candidate2297e9444922ff0aef1eb21358084dbfe3e13ec4, not the unpatched checkout base. The test closes its socket without reading the ACK, then polls durable transition state. Source inspection alone does not establish why this run failed or whether it is a runtime or fixture problem. One same-source job replay was requested as diagnosis; a later pass alone does not mean the failure was repaired. This finding remains documented and must be reconciled before claiming merge readiness.

## Rollback and limits

Revert the isolated duplicate-key parser/test change to roll back; do not revert unrelated current-main work. No installed runtime, host permissions, operator keys, package release or deployment was changed. Required checks must remain enabled, and failed or unobserved relevant gates must be reported rather than relabeled as success.
