# Guarantee ledger — every sentence, its test, its mutation

Date: 2026-09-23 (round 11, `transport.the-guard-must-not-depend-on-the-shape-of-its-dependency`).
Rule: a guarantee sentence that no test can falsify is deleted or made true.
Each row names the test that can fire on it and the mutation that shows the test
has teeth. Every number in a row is produced by the test the row names, on this
tree; the round's raw logs (command, environment, bare exit code) live beside
this file in the delivery folder.

| # | Sentence | Falsifying test | Mutation / ignition |
|---|---|---|---|
| 1 | "Confidentiality is decided by the ACTION, at the moment the private read executes, AND by the TABLE the connection reads." | `tests/unit/test_private_read_verdict_at_execution.py` — every action derived from `WRITER_ONLY_READS` is refused through `execute` and by a direct service call; `tests/unit/test_every_private_table_reader_is_judged_at_the_read.py` — a reader defined at test time, registered nowhere, is refused by the connection | Neutralize `core.state_privacy.refuse_private_read`: `IGNITION_COUNT = 9`; disarm `read_authorizer`: `IGNITION_COUNT = 3` (the three notebook tables the after-the-fact reader reaches) |
| 2 | "A route that bypasses `execute` is refused by the same function." | same file — a fresh app and `api.main.app`, each with a handler that calls the service method directly | Remove the `@writer_only_read` decorators: the direct calls return bodies |
| 3 | "`judged` means an authorization dependency executed; a public read cleared by its declaration reads `cleared`, never `judged`." | `tests/integration/test_coverage_measures_judged_not_reached.py::TestJudgedMeansADependencyExecuted::test_restoring_arrival_counting_re_lies_and_is_caught` | Restore arrival counting: `IGNITION_COUNT = 1` (honest `judged=False` against mutated `judged=True`) |
| 4 | "The ban on lie-keeping phrases covers every file this card touched, scope taken from git against the card's FIRST-round base, with no line-prefix exclusion." | `tests/integration/test_no_unfalsifiable_guarantees.py` — the scope is `git diff` against `9c79f11f`; `test_the_scan_is_anchored_on_the_cards_first_round_base` pins the base and proves it resolves in git, and `test_no_line_prefix_can_exclude_a_banned_phrase` plants a phrase and the same predicate catches it | Move the base to the previous round's candidate (`98bffeac`, the r10 candidate): `test_the_scan_is_anchored_on_the_cards_first_round_base` fails AND a phrase planted into `api/state_only.py` is missed, because the narrower base left that file out of scope |
| 5 | "The guard runs a verdict dependency on FastAPI's own machinery for six shapes." | `tests/integration/test_verdict_runs_on_fastapi_machinery.py` — six shapes against a plain `Depends(D)`; the hand-call base pole is in-file | Hand-call `dependency(request)`: the async and yield shapes return a coroutine and the refusal inside never runs |
| 6 | "The private-delivery check runs before any early `ok=True`." | `tests/integration/test_private_delivery_before_early_ok.py` — both poles plus the product `/api/state/query/{action}` positive case | Revert the order: 200 with the private body |
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

## Changed this round (r11)

Row 1 now names the TABLE layer as well as the ACTION layer. Ten rounds put the
verdict on a list - route sources, dispatch entries, declared-trust classes - and
each review found the anonymous 200 one layer below it. A reader written after
the round, imported by nobody and named in no table, is now judged by the
connection it reads through (`core.state_privacy.TrustBoundDatabase` ->
`read_authorizer`). The private table set is the one classification beside the
schema, checked against every `state_*` table by
`tests/unit/test_undeclared_reader_is_untrusted.py::test_every_state_table_is_classified_in_one_place`.

The three reclassified sentences, each grep-checked to zero hits this round:
`api/state_http.py:210-211`'s teardown-order explanation; the teardown test's
claim that a later early close of the verdict stack would show up there (measured
0/167); and `test_private_read_verdict_at_execution.py`'s "reachable only through
`execute`" about the driver guide. The guide is now a PUBLIC read — its text is
`core/state_driver_guide.py` in a PUBLIC repository, and the MCP prompt and
resource already served it — so it is a positive control, not a leak.

Rows 1 and 2 replace the r8 claim that the route reader settles what a handler
delivers. `binding_for` and the AST reader remain in the tree as defence in
depth, but no sentence here rests on them.

Row 4's scan base moved from the r10 candidate back to the card's first-round
base `9c79f11f`. Anchored on the previous candidate, 17 of the 41 files the card
had already changed fell outside the scan; anchored on the card base they are in
scope, and two of them (`api/main.py`, `api/state_only.py`) carried a banned
phrase and were reworded this round. This round's own new files are in scope
either way.
