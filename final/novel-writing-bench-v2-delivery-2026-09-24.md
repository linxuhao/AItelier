# Writing Bench v2 — round notes (2026-09-24)

Base candidate: `31c5be92fd3237856c4a4ff70545a5b48f81a4af`. An independent
review measured `tests/writing_bench` 99 passed, bare exit code 0, on that base
and found eight named mutants still alive plus three delivery-note statements
that did not match the code.

This round (a) delivers one real patch per named mutant under
`final/mutations/`, (b) adds one test per mutant that turns the full suite red
when the real edit is planted, (c) fixes the vacuous M9 block, and (d) corrects
the three untrue statements in `final/novel-writing-bench-v2-delivery-2026-09-23.md`.
It does NOT deploy, restart, or touch the novel repository.

## 1. The eight named mutants, each with a real patch and a killing test

Each row: the patch file is the exact edit; planting it in the real source file
turns the full `tests/writing_bench` suite red and names the test below; restoring
returns the bare exit code to 0. Command, environment, bare exit codes and the
named failing test are recorded in
`logs/writing-bench-r2-mutations-two-pole-2026-09-24.txt` (planted, red) and
`logs/writing-bench-r2-restore-green-2026-09-24.txt` (restored, green).

| id | patch | guard the mutant removes | test that goes red |
|----|-------|--------------------------|--------------------|
| M9a | `final/mutations/M9a.patch` | `_review_evidence` always calls `validate_certificate` (`aitelier/writing_bench/bench.py:274`); the mutant wraps it in `if certificate is not None:` | `test_reviewer_r2_mutation_survivors_killed.py::test_m9a_a_domain_verdict_without_any_host_certificate_is_refused`, and `test_named_mutation_survivors_killed.py::test_m9_missing_certificate_is_refused_at_the_real_tool_entry` |
| M9b | `final/mutations/M9b.patch` | `validate_certificate` refuses a missing certificate (`aitelier/writing_bench/reading.py:82`); the mutant returns early on `None` | `test_reviewer_r2_mutation_survivors_killed.py::test_m9b_validate_certificate_refuses_a_missing_certificate` |
| N3 | `final/mutations/N3.patch` | `observed_review` compares `claim.run_id`, `claim.step_id` and `claim.step_instance_id` against this run and its completed reviewer instance (`aitelier/writing_bench/adapter.py:75-79`) | `test_reviewer_r2_mutation_survivors_killed.py::test_n3_a_certificate_from_another_run_is_refused` |
| N4b | `final/mutations/N4b.patch` | `validate_targets` compares `reviewed_chapters` bytes exactly, title included (`aitelier/writing_bench/reading.py:77`) | `test_reviewer_r2_mutation_survivors_killed.py::test_n4b_the_chapter_title_is_part_of_the_review_target` |
| N4c | `final/mutations/N4c.patch` | the same comparison matches `prose_sha256` in full | `test_reviewer_r2_mutation_survivors_killed.py::test_n4c_the_chapter_prose_hash_must_match_in_full` |
| N5 | `final/mutations/N5.patch` | `load_certificate` verifies `sha(raw) == pointer["sha256"]` (`aitelier/writing_bench/reading.py:282`) | `test_reviewer_r2_mutation_survivors_killed.py::test_n5_the_published_certificate_checksum_is_verified` |
| N8 | `final/mutations/N8.patch` | `Coverage.observe` credits only `read`, `novel_bench_read`, `recall_observation` (`aitelier/writing_bench/reading.py:175`) | `test_reviewer_r2_mutation_survivors_killed.py::test_n8_only_the_allowlisted_read_tools_grant_coverage` |
| N9 | `final/mutations/N9.patch` | `validate_certificate` requires `cert["complete"] is True` (`aitelier/writing_bench/reading.py:94`) | `test_reviewer_r2_mutation_survivors_killed.py::test_n9_the_certificate_complete_flag_is_required` |

All eight are killed: 8/8 planted runs report bare exit status 1 and name a
test; 8/8 restores report bare exit status 0. The exact red counts per row are
in the log.

### The vacuous M9 block (fixed)

`tests/writing_bench/test_named_mutation_survivors_killed.py` had a final
`pytest.raises(BenchError)` block that called `bench.literary(run, value)` twice
and then appended `adapter.Host().observed_review(run, "literary", value)`.
That last call raises `BenchError` on its own (no certificate is present), so
the block passed even when `bench.literary` silently accepted a missing
certificate. The block now holds exactly the single call under test:
`bench.literary(run, value)`. It is this reduced block that goes red under the
M9a and M9b mutants above.

The plan also asked for a reuse-shaped case: an old self-reported receipt with
`reading: null` must not become evidence for a new run. That is
`test_m9_a_legacy_receipt_without_a_certificate_cannot_be_reused` in the new
file; it refuses through the reuse branch of `Bench.literary`
(`aitelier/writing_bench/bench.py:299`, `proof = receipt.get("reading")`).

Every `pytest.raises` block in both files holds exactly the one call under
test. The pre-existing blocks elsewhere in `tests/writing_bench` were checked
and already hold one call each.

## 2. The three delivery-note statements, corrected against the code

**① Does session assembly depend on assignment order?** YES. The production
call site is `begin_observed_review(self)` — `core/dpe_pipeline.py:3346`, one
argument — so the step id comes from `getattr(engine, "_current_step", None)`
(`aitelier/writing_bench/adapter.py:345`). `begin_observed_review` accepts a
`step_id` argument, but that argument is not passed by the sole production
caller. The 09-23 note said "the step id is now an explicit argument; … session
installation no longer depends on field-assignment order"; that was wrong and
is corrected in the 09-23 file, and the `begin_observed_review` docstring now
records the same. The block must stay below `self._current_step = step_id`
(`core/dpe_pipeline.py:3334`).

**② Can an already-accepted, not-yet-backed-up chapter still be backed up after
an engine-identity change?** YES. `Bench.accepted` does not consult
`engine_identity()`; it compares the stage's recorded `engine` against the
FROZEN manifest (`aitelier/writing_bench/bench.py`, `stage["engine"] ==
m["engine"]`), so a pending backup still succeeds. `Bench.input` and
`Bench.promote` DO consult it (`m["engine"] == engine_identity()` /
`_verify_stage`) and refuse with "policy or implementation changed; start a new
submission". This is pinned by
`tests/writing_bench/test_review_host_boundary.py::test_already_accepted_backup_survives_code_upgrade_not_new_approval`.
The 09-23 note said such a chapter "is refused"; that was wrong and is
corrected.

**③ Where does the real-path test fail when assembly is missing?** At the
`literary_check` tool step, not at observer installation. With the block hoisted
above `self._current_step = step_id`, both review steps still complete without a
certificate; `aitelier/writing_bench/adapter.py:252` calls
`host.observed_review(...)`, which calls `load_certificate`
(`aitelier/writing_bench/reading.py:279`) and raises
`BenchError: host-observed complete reading certificate required`
(`aitelier/writing_bench/reading.py:287`). The failure surfaces inside
`session.sf.advance_run(run)` at
`tests/writing_bench/test_observed_session_real_path.py:189`. The 09-23 note
said "the gateway then reports no installed observer"; that was wrong and is
corrected. Measured this round at exit status 1 (log in `logs/`).

## 3. What changed in the shipped candidate

- `aitelier/writing_bench/adapter.py` — `begin_observed_review` docstring only;
  it now states that installation depends on `engine._current_step` on the
  production path.
- `core/dpe_pipeline.py` — comment above the session block only; the executable
  code is byte-for-byte the 09-23 shipped order.
- `tests/writing_bench/test_reviewer_r2_mutation_survivors_killed.py` — new, 9
  tests.
- `tests/writing_bench/test_named_mutation_survivors_killed.py` — the vacuous
  M9 block reduced to its one call under test.
- `final/novel-writing-bench-v2-delivery-2026-09-23.md` — the three statements
  above corrected.

No production behaviour changed. `engine_identity()` hashes these files, so the
frozen-submission consequence in ② applies unchanged.

## Judgement on the acceptance criteria

| criterion | verdict | evidence |
|-----------|---------|----------|
| `named-mutation-survivors-are-killed` (M3/M9/M11) | PASS | `test_named_mutation_survivors_killed.py`, two-pole in the 09-23 log; the M9 block is now non-vacuous and the M9a/M9b mutants are killed (09-24 log) |
| `reviewer-r2-mutation-survivors-are-killed` (M9a/M9b/N3/N4b/N4c/N5/N8/N9) | PASS | 8/8 planted red and named, 8/8 restored green — `logs/writing-bench-r2-mutations-two-pole-2026-09-24.txt` |
| `delivery-doc-matches-code` | PASS | section 2 above, with code lines and test names |
| `review-session-installed-on-the-real-step-path` | PASS | `test_observed_session_real_path.py`, reverse pole measured at exit status 1 |
| `observed-read-coverage`, `review-target-binding`, `frozen-file-submission`, `manual-exact-acceptance`, `grounded-editor-context`, `recoverable-delivery`, `regression-and-independent-review` | not re-measured this round | covered by the 09-23 candidate and its logs; no production behaviour changed here |
| `versioned-rollout-handoff` | NOT DONE this round | no deployment, no restart, no novel-repo touch; the stage-gate rehearsal and the private backup are the director's to execute |

## Known limits

- A mutation patch proves a test fails when the guard is removed. It does not
  prove a live reviewer run or a live private backup has ever executed.
- The certificate proves the material was PRESENTED, not that the model
  understood it. Literary judgement still needs the independent review and the
  director's manual approval.
- `final/mutations/*.patch` are unified diffs against the base paths; they are
  evidence of the exact planted edit, not an automatic applier.
