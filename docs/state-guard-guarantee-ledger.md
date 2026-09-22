# Guarantee ledger — every sentence, its test, its mutation

Date: 2026-09-22 (round 9, `transport.the-guard-must-not-depend-on-the-shape-of-its-dependency`).
Rule: a guarantee sentence that no test can falsify is deleted or made true.
Each row names the test that can fire on it and the mutation that shows the test
has teeth. Every number in a row is produced by the test the row names, on this
tree; the round's raw logs (command, environment, bare exit code) live beside
this file in the delivery folder.

| # | Sentence | Falsifying test | Mutation / ignition |
|---|---|---|---|
| 1 | "Confidentiality is decided by the ACTION, at the moment the private read executes." | `tests/unit/test_private_read_verdict_at_execution.py` — every action derived from `WRITER_ONLY_READS` is refused through `execute` and by a direct service call | Neutralize `core.state_privacy.refuse_private_read`: 9 of the 11 paths return a private body, `IGNITION_COUNT = 9` |
| 2 | "A route that bypasses `execute` is refused by the same function." | same file — a fresh app and `api.main.app`, each with a handler that calls the service method directly | Remove the `@writer_only_read` decorators: the direct calls return bodies |
| 3 | "`judged` means an authorization dependency executed; a public read cleared by its declaration reads `cleared`, never `judged`." | `tests/integration/test_coverage_measures_judged_not_reached.py::TestJudgedMeansADependencyExecuted::test_restoring_arrival_counting_re_lies_and_is_caught` | Restore arrival counting: `IGNITION_COUNT = 1` (honest `judged=False` against mutated `judged=True`) |
| 4 | "The ban on lie-keeping phrases covers every file this round touched, scope taken from git, with no line-prefix exclusion." | `tests/integration/test_no_unfalsifiable_guarantees.py` — the scope is `git diff` against the round base; `test_no_line_prefix_can_exclude_a_banned_phrase` plants a phrase and the same predicate catches it | Exclude the declaration line (the old hole): the planted phrase stops being caught |
| 5 | "The guard runs a verdict dependency on FastAPI's own machinery for six shapes." | `tests/integration/test_verdict_runs_on_fastapi_machinery.py` — six shapes against a plain `Depends(D)`; the hand-call base pole is in-file | Hand-call `dependency(request)`: the async and yield shapes return a coroutine and the refusal inside never runs |
| 6 | "The private-delivery check runs before any early `ok=True`." | `tests/integration/test_private_delivery_before_early_ok.py` — both poles plus the product `/api/state/query/{action}` positive case | Revert the order (patched `old_binding_for`): 200 with the private body |
| 7 | "A declaration the reader cannot see, or that delivers a private action, is REFUSED." | `tests/integration/test_unreadable_delivery_is_refused.py` — the hidden-delivery shapes, binding and guard poles | Delete the fail-closed branches: each shape binds `ok=True` and the guard pole returns 200 with the body |

## Changed this round

The two hollow demonstrations in
`tests/integration/test_coverage_measures_judged_not_reached.py` are DELETED,
not rewritten: the `if row["responded"]` reproduction of the old metric (which
counted the leaking route as covered whatever the metric said) and the whole
`with_guard=False` mount. Every remaining test there must name a mutation that
turns it red; `test_restoring_arrival_counting_re_lies_and_is_caught` is the one
whose ignition this round measured (`IGNITION_COUNT = 1`).

The duplicated `/health` assertion in
`tests/integration/test_project_gate_assembly_points.py` is deleted.

The r8 teardown explanation in
`tests/integration/test_verdict_runs_on_fastapi_machinery.py` is deleted: the
deployed FastAPI already closes a `yield` dependency at the right point, so that
test is green on the base tree as well and is kept as a regression guard only.

Rows 1 and 2 replace the r8 claim that the route reader settles what a handler
delivers. `binding_for` and the AST reader remain in the tree as defence in
depth, but no sentence here rests on them.
