# Task-item refusal regressions

Run with exact official skillflow-py 1.5.97 in an offline disposable container:

    python -m pytest -q -s tests/unit/test_dpe_refusal_progress

The scenario calls the real AgentStepRunner, PipelineEngine, AIGateway and SDK
with an owned real Git worktree, task loop and code-only item receipt. Only
litellm.completion is scripted. Dependency composition points to the fixture DB
and SDK; no scope admission, gateway, tool dispatch or engine is mocked.

Cross-instance repeat tests expect the accepted bounded refusal behavior and
are RED on base 3d88. Legacy JSON-files correction is also RED on that base.
Useful path correction, same-batch, JSON syntax repair and refusal-only reclaim
are positive guards. REFUSAL_EVIDENCE records actual final transport requests,
returned responses, invocation events, claim identities, raw denials, SDK rows,
Git identity/status and item receipts, including when a test fails.

This is not a complete production acceptance suite. The small SDK graph keeps
the default name and actual SDK producer/item/code-only boundaries, with normal
conditional review edges returning to the task-loop header, but replaces upstream
planning and the source reviewer with deterministic fixture artifacts. It does
not execute the final test/candidate gates or scheduler/run_driver/meta catches.
Refusal-only reclaim uses genuine SDK release/reclaim and native trace restore;
it does not simulate an unresolved admitted mutation or an incomplete response
batch. JSON callback composition/failover/projection loss, actual unsettled
mutation recovery and missing/corrupt provenance remain uncovered.

Revision2 asserts every actual producer/reviewer SDK row has the raw card item
and the final artifact path is step/item before manually supplying card content.
The prior fixture lacked a return edge and proved only manually supplied-card
scope behavior with a flat directory; its earlier evidence remains historical.

Revision3 observes actual handler entry with wrappers that only record then call
the original. JSON full-tool uses legitimate allow_full_write=False (create/edit
and repository reads), while JSON-content retains its constrained writer. Cards
are read by real required Step3/tasks/$current_task.json SDK context sources; no
resolved-context card or WriteScope is injected. A trusted upstream artifact
revision is tested through a new producer, newly permitted output and receipt.
Earlier json-tool labels actually selected JSON-content; prior evidence remains
historical and cannot establish the full-tool route.
