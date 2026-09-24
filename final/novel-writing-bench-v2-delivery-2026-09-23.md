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
   step_id=None)` accepts the step id as an explicit argument, and its docstring
   records that the sole production call site omits it. Session installation on
   the production path therefore DOES still depend on field-assignment order:
   the call site is `begin_observed_review(self)`, so the step id comes from
   `engine._current_step`. That is exactly why the block sits below
   `self._current_step = step_id`.

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
therefore a frozen submission's `engine` identity change. What that actually
refuses, per the code: `Bench.input` (`aitelier/writing_bench/bench.py:221`,
the `m["engine"] == engine_identity()` require) and `Bench.promote` (which
reaches that same `Bench.input` require through `_verify_stage`
(`bench.py:471-472`); the `stage["engine"] == engine_identity()` require at
`bench.py:473` carries the different message "stage binding changed")
raise "policy or implementation changed; start a new submission" once the hash
moves. `Bench.accepted` does NOT check `engine_identity`: it only compares the
stage's recorded `engine` against the FROZEN manifest
(`aitelier/writing_bench/bench.py`, `stage["engine"] == m["engine"]`), so an
already-accepted chapter whose backup is still pending CAN still be backed up
after an engine-identity change. That is the behaviour the existing test
`tests/writing_bench/test_review_host_boundary.py::
test_already_accepted_backup_survives_code_upgrade_not_new_approval` pins:
after `monkeypatch.setattr(domain, 'engine_identity', lambda: 'f'*64)` it asserts
`bench.accepted('run1', stage['commit']) == receipt` and
`bench.record_backup(...)['status'] == 'backed_up'`, while `bench.promote` still
raises. Accepted history is never rewritten. This is the existing, deliberate
guard; it was not weakened this round.

## Tests added

- `tests/writing_bench/test_observed_session_real_path.py` — drives the REAL
  `AgentStepRunner` → REAL `PipelineEngine` (built per step) through the shipped
  `novel_writing_bench_v2` graph for `literary_review` and `ledger_audit`. Only
  the provider call is a double. Asserts: the session is installed, the
  messages actually handed to the model are recorded, the host certificate is
  written for both phases, and the stage gate consumed it.
  Reverse-pole: restoring the session block above `self._current_step = step_id`
  turns this test red — measured this round at the `literary_check` tool step,
  not at observer installation. With the block hoisted, the two review steps
  still complete (no certificate is written), and
  `aitelier/writing_bench/adapter.py:252` calls
  `host.observed_review(...)`, which calls `load_certificate` and raises
  "host-observed complete reading certificate required" from
  `aitelier/writing_bench/reading.py:287`. The failing assertion is therefore
  `test_real_runner_installs_the_observed_session_for_both_review_steps`
  reaching the stage gate without a certificate, inside the
  `session.sf.advance_run(run)` call at
  `tests/writing_bench/test_observed_session_real_path.py:189`.
- `tests/writing_bench/test_reviewer_r2_mutation_survivors_killed.py` — one
  named test per mutation an independent review measured surviving `31c5be92`:
  `M9a` `_review_evidence` skips a missing certificate, `M9b`
  `validate_certificate` returns early on a missing certificate, `N3` a
  certificate from another run/step/attempt, `N4b` the title is ignored, `N4c`
  the prose hash only matches as a prefix, `N5` the stored certificate checksum
  is not verified, `N8` any tool output is credited as a read, `N9` the
  `complete` flag is ignored. Each is also planted as the real edit in
  `final/mutations/<id>.patch`; each patch turns the full `tests/writing_bench`
  suite red and names its test, and each restore returns the bare exit code to
  0 (logs in `logs/`).
- `tests/writing_bench/test_named_mutation_survivors_killed.py` — one test (or
  one positive-pole pair of tests) per survivor M3, M9, M11. The M9 block that
  previously passed vacuously was reduced to its single call under test.

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

### Superseded by the 2026-09-24 round

`final/novel-writing-bench-v2-delivery-2026-09-24.md` is the authoritative
note for the current candidate: it carries the eight named mutation patches,
their two poles, and the corrected statements above.

- The certificate proves the material was PRESENTED, not that the model
  understood it. Literary judgment still needs the independent review and the
  director's manual approval.
- Model and network boundaries in these tests are doubles. They do not show
  that a live reviewer run or a live private backup has ever executed.
