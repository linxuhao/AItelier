# Writing Bench v2 — round notes (2026-09-23)

Base candidate: `523c845`. This round's work is the assembly-order fix for the
host-observed review session, plus tests that turn the three known surviving
mutants red.

## What was wrong

`core/dpe_pipeline.py` installed the review session at the top of
`_run_native_step` (`begin_observed_review(self)`), and
`aitelier/writing_bench/adapter.py` read the live step id off the engine
(`engine._current_step`) to decide whether the step is a Writing Bench reviewer.
`self._current_step = step_id` was assigned ~90 lines further down, and
`aitelier/runner.py` builds a fresh `PipelineEngine` for every step, so the field
was `None` on the real path. The session was therefore never installed: no host
certificate was written, and the downstream `literary_check` had nothing to
accept. All 93 tests were green because every one of them built its
`ReviewSession` by hand.

## What changed

1. `core/dpe_pipeline.py` — the session block moved to AFTER
   `self._current_step = step_id`. The comment above it records why it must not
   be hoisted again.
2. `aitelier/writing_bench/adapter.py` — `begin_observed_review(engine,
   step_id=None)`. The step id is now an explicit argument; when omitted it
   still falls back to `engine._current_step`, so existing callers keep working.
   Session installation no longer depends on field-assignment order.

No behaviour change for any pipeline that is not `novel_writing_bench_v2`: the
function returns `None` for every other config and step id.

`core/ai_router.py` was NOT modified. `aitelier/writing_bench/bench.py` was NOT
modified in the shipped candidate (it was edited only transiently to plant the
M9/M11 mutants, then restored; the two-pole runs are logged below).

### `engine_identity()`

`aitelier/writing_bench/bench.py:engine_identity()` hashes every file in
`aitelier/writing_bench/*.py` plus `aitelier/novel_state.py`,
`core/dpe_pipeline.py` and `core/ai_router.py`. This round DID change
`core/dpe_pipeline.py` and `aitelier/writing_bench/adapter.py`, so the hash and
therefore a frozen submission's `engine` identity change. Consequence, stated
plainly: a chapter already waiting at the `stage` manual gate, or already
accepted but not yet backed up, is refused by `Bench.input` /
`Bench.promote` / `Bench.accepted` with "policy or implementation changed; start
a new submission" until an operator either finishes it on the previous
deployment (523c845) or resubmits. Accepted history is never rewritten. This is
the existing, deliberate guard; it was not weakened this round.

## Tests added

- `tests/writing_bench/test_observed_session_real_path.py` — drives the REAL
  `AgentStepRunner` → REAL `PipelineEngine` (built per step) through the shipped
  `novel_writing_bench_v2` graph for `literary_review` and `ledger_audit`. Only
  the provider call is a double. Asserts: the session is installed, the
  messages actually handed to the model are recorded, the host certificate is
  written for both phases, and the stage gate consumed it.
  Reverse-pole: restoring the session block above `self._current_step = step_id`
  turns this test red (the gateway then reports no installed observer).
- `tests/writing_bench/test_named_mutation_survivors_killed.py` — one test (or
  one positive-pole pair of tests) per survivor M3, M9, M11.

## Suite-order repairs (this round, second pass)

The full suite surfaced two order-dependent failures that neither the
narrow `tests/writing_bench` run nor either test alone showed. Both were
tests measuring the wrong thing once the suite ran in one process, and both
are now fixed in the shipped candidate:

1. `tests/unit/test_a_round_reports_what_it_paid_twice_for.py::
   test_an_engine_without_the_counter_costs_a_key_not_a_step` — the test
   simulated a wheel without `skillflow.read_accounting` by putting `None`
   into `sys.modules`. But `from skillflow import read_accounting` resolves
   through the PACKAGE attribute first, and an earlier test in the suite
   leaves that attribute bound to the real counter, so the test silently
   exercised the real engine and saw its `{reads: 0, ...}` summary instead of
   `{}`. The test now also removes the package attribute. Alone it passed;
   after `tests/unit/test_coding_impl_self_verification.py` it failed.
2. `tests/writing_bench/test_observed_session_real_path.py` — it stubbed the
   code-path lookup with a CLASS-level `monkeypatch.setattr` on
   `core.workspace_manager.WorkspaceManager`. `tests/unit/
   test_public_read_hardening.py` calls
   `importlib.reload(core.workspace_manager)`, which rebinds the class object;
   the workspace the test built keeps the ORIGINAL class, so the class-level
   stub missed and the real `run_isolation` resolver raised
   `IsolationUnavailable`, failing the test before any review step ran. The
   stub is now installed on the workspace INSTANCE, which no reload can
   stale. Alone it passed; after `tests/unit/test_public_read_hardening.py`
   it failed.

Both repairs are in test files only. No production module changed in this
pass; `core/dpe_pipeline.py` and `aitelier/writing_bench/adapter.py` carry the
assembly-order fix described above and nothing else.

Log: `logs/writing-bench-suite-order-2026-09-23.txt` records the red pairs
(each failing test with its polluter, bare exit code) and the green runs
(`tests/writing_bench` plus both repaired tests, 209 passed, bare exit code 0).

## Known limits
## Known limits

- The certificate proves the material was PRESENTED, not that the model
  understood it. Literary judgment still needs the independent review and the
  director's manual approval.
- Model and network boundaries in these tests are doubles. They do not show
  that a live reviewer run or a live private backup has ever executed.
