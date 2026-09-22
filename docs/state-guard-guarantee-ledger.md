# Guarantee ledger — every sentence, its test, its mutation

Date: 2026-09-22 (round 8, `transport.the-guard-must-not-depend-on-the-shape-of-its-dependency`).
Rule: a guarantee sentence that no test can falsify is deleted or made true.
Each row: the sentence, the test that can fire on it, the mutation that proves
the test has teeth, and where the ignition count and bare exit codes live
(every probe in this round was a bare pytest gate, no pipes; exit code 0).

| # | Sentence (as written) | Falsifying test | Mutation / ignition | Evidence |
|---|---|---|---|---|
| 1 | "A delivery the AST reader cannot see is REFUSED, never approved — module-level helper, renamed import, `async def` inner, keyword `action=`." | `tests/integration/test_unreadable_delivery_is_refused.py` — 4 binding poles + 4 guard poles (fresh app, armed) | Restore the old reader: delete the `if delivery.followed` refusal and the bare-name fail-closed `opaque` branch; each shape then binds `ok=True` (fail-open) and the guard pole returns 200 with the private body — 8 named failures | `IGNITION` = the 403 assertions firing on every shape; bare RC=0 on the fixed tree |
| 2 | "The reader follows a module-level helper through the handler's own globals and an aliased `execute` by object identity — derivation, not a name list." | same file, `module_level_helper` / `renamed_import` poles (a refusal via `opaque` alone would fail these: the helper source is READ and its literal delivery named) | Point the resolver at a static name table instead of `__globals__`: the helper pole still refuses, but for the wrong reason — the test asserting the delivered action is NAMED in `binding.reason` goes red | bare RC=0 |
| 3 | "`judged` means an authorization dependency actually executed; a public read cleared by its declaration binding is `cleared`, never `judged`." | `tests/integration/test_coverage_measures_judged_not_reached.py::TestJudgedMeansADependencyExecuted` | Restore arrival counting (`record_judged` forced `dep_backed=True`): the same probe reads `judged=True` with zero dependencies executed — `IGNITION_COUNT = 1`, bare RC=1 | printed `DISPATCH_ROW` and `MUTATED_ROW` |
| 4 | "The old `responded ⇒ covered` metric is hollow: it counts a leaking route as covered." | `test_arrival_based_counting_would_hide_the_leak` — reproduces the old definition HONESTLY (no `and False` constant folding; the old metric is computed, and it names the leak COVERED while the new metric names it UNCOVERED) | The `and False` version is deleted; the comparison now fires by construction (`old == [LEAKY] and new == [LEAKY]`) | bare RC=0 |
| 5 | "The private-delivery check runs before any early `ok=True`; the honest dispatch family still serves." | `tests/integration/test_private_delivery_before_early_ok.py` both poles + product `/api/state/query/{action}` positive case | Reverting the order (patched `old_binding_for`) leaks: 200 + private body, ignition asserted in-file | bare RC=0 |
| 6 | "The guard runs the verdict on FastAPI's own machinery for six dependency shapes, AND teardown order matches `Depends(D)`." | `tests/integration/test_verdict_runs_on_fastapi_machinery.py` — six shapes both poles; new observation point: a `yield` verdict dependency records setup/handler/teardown identically on a plain route and through the guard | Closing the verdict stack inside the guard (the old shape) runs teardown BEFORE the handler: `GUARD_ORDER == ['setup', 'teardown']` vs control `['setup', 'handler', 'teardown']` — the order assertion goes red | printed `CONTROL_ORDER` / `GUARD_ORDER`, bare RC=0 |
| 7 | "The ban on lie-keeping phrases applies to the file that executes the ban." | `tests/integration/test_no_unfalsifiable_guarantees.py` — `ROUND_FILES` now contains itself. THE DISCOVERY: included, the ban breaks on the file's own phrase DATA; the test excludes exactly the `BANNED = [...]` declaration lines and still fires on any prose use | Removing that exclusion makes the file condemn itself — a red named failure, recorded here rather than hidden | bare RC=0 |
| 8 | "The expected surface model is DERIVED from the generated source, not a hand-kept dictionary keyed by body name." | `tests/integration/test_author_surface_generator.py` — `expected_can_serve` now reads `_model_delivery(shape)`, a second scan of the generated handler text (`_BODY_DELIVERS` is deleted) | Hand-writing the dictionary back breaks nothing today (both sides would still agree) — which is why the derivation is in the SCAFFOLD, under `tests/`, not in the API package, and the universal leak invariant is checked against the guard, not the model | bare RC=0 |
| 9 | "Test scaffolding is not an API module: nothing in `api/` imports `state_author_surface`." | structural: the module lives at `tests/support/state_author_surface.py`; `grep -r "state_author_surface" api/` is empty | A re-import from `api/` would be caught by review and by the import graph, not by a test — stated plainly rather than claimed | bare RC=0 |

## Deleted falsified prose

The six "universally true" sentences carried over from round 7 are gone from
the round files; this ledger is the only place they survive, as rows with the
test and mutation that keep them true. The two r6 paste accidents
(`if row["responded"] and False:` and the `with_guard=False` "uncovered"
demonstration) are repaired: the old metric is now COMPUTED, so the comparison
is real, and the no-guard demonstration asserts against the same metric the
guard uses.
