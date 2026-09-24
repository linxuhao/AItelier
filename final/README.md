# Delivery: a gate that never ran is not a failing gate (rev 10)

Base `dcfa78729628f0cbbbc703a44d0e97098a3b9c8f` (the r9 candidate). Branch
`director/gateacct-r10-20260924`. Round 8's `final/` is replaced: its four
logs (`criteria_green.txt`, `mutation_catalog.txt`, `reader_green.txt`,
`two_pole_mutations.txt`) are removed, and every file under `final/` was
written in this round.

Three commits matter:

* `b104f18c2e7e1d0f0c59e99cb32a077e7896dd28` is the code the mutation runner
  measured (`head(s) measured: b104f18c2e7e1d0f0c59e99cb32a077e7896dd28` in `final/logs/mutations/summary.txt`).
* `96721904a7d9b6b461109cfcded6d0abd03f469c` adds only two files after it:
  `final/capture.py` and `final/probes/p_rows.py`
  (`2 files changed, 129 insertions(+)` in `final/logs/mutation_head_to_code_head.txt`).
  Every criterion probe and the candidate's whole-suite run ran at this
  commit. The base's whole-suite run ran at `dcfa7872` in its own worktree.
* The delivery commit, on top of that, adds `final/README.md`,
  `final/render_readme.py`, `final/probes/p_hold_ign.py`,
  `final/probes/p_repeats.py` and `final/logs/`. It changes nothing outside
  `final/`.

## How the logs were made

* **`final/logs/mutations/`** is written by `tools/mutation_catalog/run_mutations.py`
  itself. It ran as four shards (`--shard i/4`). Each shard ran in its own
  throwaway container (`docker run --rm --init -m 3g`, the image
  `aitelier:latest`) with the root
  `/home/linxuhao/.AItelier/worktrees-scratch/ga10`. Every mutation and every
  control ran in a fresh `git worktree add --detach` under
  `/home/linxuhao/.AItelier/worktrees-scratch/ga10-mut/`, which was removed
  afterwards. `final/logs/mutations/summary.txt` was written by the runner's
  `--report` (`BARE_RC=0` in `final/logs/mutations_report_console.txt`).
* **Every other log under `final/logs/`** was written by `final/capture.py`.
  - Each log's header holds the command, the real cwd, HEAD with its
    `git status --porcelain` line count (taken when the command finished),
    the full `docker run` argv (or `host`), the environment that was added,
    and the sha256 of every file argument.
  - Each log ends with `BARE_RC=<n>`, the exit code of the command itself.
    Nothing was piped.
  - The Python process writes the log file. No deliverable was written by a
    shell redirection.
  - In the logs whose cwd is `ga10`, the porcelain lines are this round's
    untracked files under `final/`; no tracked file was modified. The two
    `runner_refuses_*` logs ran in a throwaway worktree, `ga10-pol`. There the
    line is the planted file, or the runner's own output directory.
* **Probes.**
  - `p_crit.py`, `p_perm7.py`, `p_dup8.py` and `plus_grep.py` are the r9
    review's own. They ran unchanged from
    `/home/linxuhao/.AItelier/director/reports/gateacct-r9-review-20260923/probes/`,
    and each log carries their sha256.
  - `final/probes/p_rows.py`, `p_hold_ign.py` and `p_repeats.py` are mine.
* **Read-back diffs** are in `final/logs/readback/`:
  - one `git diff --word-diff` per changed file, `dcfa7872..96721904`;
  - `full_diff_a057ce66.txt`, the whole `git diff a057ce66..96721904`.
* **The mutation table below** is copied out of `summary.txt` by
  `final/render_readme.py`. The same script checks every citation in this
  file: each is written as a code span, then "in", then a log path, and the
  script checks that the snippet occurs verbatim in that log.
  `final/logs/readme_citations.txt` is its run.

## Whole suite

Both runs were throwaway containers with `--init`: `python -m pytest -p no:cacheprovider -q -rfE tests/`.

| tree | result | bare RC | log |
|---|---|---|---|
| candidate `96721904` | `4713 passed, 10 skipped, 11 deselected` in `final/logs/suite_full_cand.txt` | `BARE_RC=0` in `final/logs/suite_full_cand.txt` | `final/logs/suite_full_cand.txt` |
| base `dcfa7872` | `4703 passed, 10 skipped, 11 deselected` in `final/logs/suite_full_base.txt` | `BARE_RC=0` in `final/logs/suite_full_base.txt` | `final/logs/suite_full_base.txt` |

* The candidate has 10 more tests than the base. They are the new tests this
  round added.
* The known flake, `test_parallel_runs_progress_but_competing_controllers_admit_one_owner`,
  did not fail in either run (neither log has a `FAILED` line), so I did not
  rerun it alone.

**An earlier pair of runs went red, and I kept both logs.** Those two runs
went at the same time as each other and as another session's containers on
the same host.

* Candidate: `FAILED tests/unit/test_bash_cleanup_is_async.py::test_a_timed_out_command_keeps_the_loop_responsive` in `final/logs/suite_full_cand_concurrent.txt`, `1 failed, 4712 passed` in `final/logs/suite_full_cand_concurrent.txt`.
* Base: `FAILED tests/unit/test_bash_cleanup_is_async.py::test_escalating_to_sigkill_keeps_the_loop_responsive` in `final/logs/suite_full_base_concurrent.txt`, `2 failed, 4701 passed` in `final/logs/suite_full_base_concurrent.txt`.
* Both ended `BARE_RC=1`.

The red tests are loop-responsiveness timing tests, and they went red on both
trees. I then ran the two suites one after the other, and those runs are the
table above.

## Criterion by criterion

### `an-absence-may-never-become-a-relative-pass`

I ran the r9 review's `p_crit.py` on the candidate and on the base. Four
declared absences in a row, on the candidate:

* each run has `"passed_relative": false` in `final/logs/criteria/p_crit_cand.txt`;
* each has `"baseline_state": "unmeasured"` in `final/logs/criteria/p_crit_cand.txt`;
* no baseline file is written: `"c1_baseline_files": []` in `final/logs/criteria/p_crit_cand.txt`.

The other pole is a measured red. It still enters the baseline:
`"repo_gate:run_tests.sh#compile/A"` in `final/logs/criteria/p_crit_cand.txt`.

The base gives the same values (`"c1_baseline_files": []` in `final/logs/criteria/p_crit_base.txt`).
Both logs end `BARE_RC=0`.

The four mutations are ABSREL, ABSSEED, ABSSTATE and ABSWRITE; the table
below has their rows.

ABSREL deletes the absence branch of `passed_relative`. In r7, r8 and r9 its
only red test was a source-text witness. It now has a behavioural killer,
`test_an_absence_whose_failures_are_all_known_red_still_does_not_pass`. That
test declares an absence whose only failure is already a known red, and
asserts `passed_relative` is False.

### `an-expired-absence-is-never-charged-to-the-implementer`

I ran the r9 review's `p_perm7.py` on the real `configs/coding_impl.yaml`
and the real `AItelierSkillFlow`. The gate never yields a verdict, and the
probe drives in `_run_skillflow_tick`'s order. There are four poles, each
set through the environment shown in the log header:

| pole | env ceiling | effective ceiling | implement_runs | gate_calls | final status | log |
|---|---|---|---|---|---|---|
| A | `AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS=100000` in `final/logs/criteria/POLE_A_cand.txt` | `"episode_max_seconds": 21600.0` in `final/logs/criteria/POLE_A_cand.txt` | `"implement_runs": 1` in `final/logs/criteria/POLE_A_cand.txt` | `"gate_calls": 1` in `final/logs/criteria/POLE_A_cand.txt` | `"run_status": "running"` in `final/logs/criteria/POLE_A_cand.txt` | POLE_A_cand.txt |
| B | `AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS=0` in `final/logs/criteria/POLE_B_cand.txt` | `"episode_max_seconds": 10800.0` in `final/logs/criteria/POLE_B_cand.txt` | `"implement_runs": 1` in `final/logs/criteria/POLE_B_cand.txt` | `"gate_calls": 1` in `final/logs/criteria/POLE_B_cand.txt` | `"run_status": "running"` in `final/logs/criteria/POLE_B_cand.txt` | POLE_B_cand.txt |
| C (small ceiling, observed for less than the ceiling) | `AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS=2` in `final/logs/criteria/POLE_C_cand.txt` | `"episode_max_seconds": 2.0` in `final/logs/criteria/POLE_C_cand.txt` | `"implement_runs": 1` in `final/logs/criteria/POLE_C_cand.txt` | `"gate_calls": 1` in `final/logs/criteria/POLE_C_cand.txt` | `"run_status": "running"` in `final/logs/criteria/POLE_C_cand.txt` | POLE_C_cand.txt |
| C′ (small ceiling, observed past it) | `AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS=2` in `final/logs/criteria/POLE_Cexp_cand.txt` | `"episode_max_seconds": 2.0` in `final/logs/criteria/POLE_Cexp_cand.txt` | `"implement_runs": 1` in `final/logs/criteria/POLE_Cexp_cand.txt` | `"gate_calls": 1` in `final/logs/criteria/POLE_Cexp_cand.txt` | `"run_status": "failed"` in `final/logs/criteria/POLE_Cexp_cand.txt` | POLE_Cexp_cand.txt |

* C′ ends naming the absence:
  `"error_reason": "gate did not run: no verdict was measured (run_tests.sh, 41 attempt(s))"` in `final/logs/criteria/POLE_Cexp_cand.txt`.
* `Cycle limit exceeded` appears in none of the four logs.
* Each pole log ends `BARE_RC=0`.

**Routing.** This round did not change it. An honest report still writes
`test_report.json`. The `test` step's `repo_gate_absent` flag edge goes to
`test_gate_absent`, a gate whose only transition is `to: null`, and that edge
is listed before the `written: "test_report.json"` edge. So a declared absence
never reaches `test_outcome → implement`. (The configs section below has the
diff.)

The same poles are in the test suite, on the same graph, driven by the real
tick. There is now a separate non-expiring small-ceiling pole,
`test_pole_c_a_tiny_ceiling_inside_the_window_is_honoured_and_uncharged`.
Its small ceiling is longer than the window it observes, so the run stays
`running` with `implement_runs == 1`. That makes 8 tests in the file:
`8 passed` in `final/logs/criteria/absence_e2e_cand.txt`.

### `every-new-branch-has-a-mutation-that-kills-a-test`

N9, M21 and M21b are in the table below. Their rows give:

* ignition at the targeted and the full scope;
* both bare RCs, all 1;
* the killer tests.

Their targeted lists now name the tests that go red under them:

* N9: `test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop`, which asserts the deleted segment itself;
* M21: `test_the_r4_shaped_witness_flips_under_the_literal_m21`;
* M21b: `test_the_r4_shaped_witness_flips_under_the_literal_m21b`.

Both r8 sentences the criterion names are gone: the prose grep over this
round's added lines finds `'ONE noise length': 0 hit(s)` and
`'inertness witness': 0 hit(s)` in `final/logs/criteria/prose_grep_added_lines.txt`.
Where a docstring said a test "kills M21" but the test stays green under the
literal M21, it now names the mutation that does redden it, M21PAIR or
M21BPAIR.

### `no-fix-may-lengthen-how-long-one-step-holds-the-scheduler`

* `"hold_gate_invocations_per_step": 1` in `final/logs/criteria/p_crit_cand.txt`
* `"REPO_GATE_TIMEOUT": 5400` in `final/logs/criteria/p_crit_cand.txt`

So one step holds the scheduler for at most one gate run, bounded by that timeout.

Two mutations show that the assertions go red and name the number:

* ATTEMPTS3 sets the gate runs per step back to 3. It is killed by
  `test_one_step_holds_the_scheduler_for_at_most_one_gate_run`.
* TIMEOUTBIG raises the per-run timeout. It is killed by the same test.

TIMEOUT60 is r8's edit of the old name S1. It is killed by
`test_the_budget_goes_red_and_names_the_number_that_moved`.

### `the-declaration-survives-a-real-gates-output`

* The real gate output is truncated: `"c5_output_truncated": true` in `final/logs/criteria/p_crit_cand.txt`.
* The declaration is not in the retained tail: `"c5_declaration_in_retained_tail": false` in `final/logs/criteria/p_crit_cand.txt`.
* It is still read: `"state": "blocked"` in `final/logs/criteria/p_crit_cand.txt`.
* So the run is `"c5_measured": "unmeasured"` in `final/logs/criteria/p_crit_cand.txt`.

The three mutations the criterion names are M2, M19 and TAILONLY2, now under
their original edits. D1 and DECLNONE are also in the table.

How this sits with M21:

* M21 concerns how one line is matched. The prefix must be at the start of the
  line (`startswith`), and the slice starts at the prefix's real length.
* This criterion concerns which text is scanned: the whole output, not the
  retained tail (`"c5_retained_output_chars": 2000` in `final/logs/criteria/p_crit_cand.txt`).
* Each has its own killers: M21 and M21b for the first; M2, M19 and TAILONLY2
  for the second.

### `the-deferral-mechanism-has-readers-of-its-own`

**① `_drive` runs the production holds.**
`tests/skillflow/test_coding_impl_gate_absence.py::_drive` calls the real
`core/scheduler._run_skillflow_tick` once per tick. It reads what the tick
decided from the tick's own `tick_log` line.

* The tick hold is the scheduler's own: `observe_run`, then an early return
  on `silent`, or `fail_run` on `expired`.
* After each tick that held the run, `core/run_driver._step` steps the same
  run once. This is the second production driver, and it reaches the host
  hold in `AItelierSkillFlow.advance_run`.
* The file has no copy of either hold.

**② Poles.** `_assert_test_row_untouched` is called from:

* poles A, B, C and C′;
* `test_a_gate_that_finally_answers_clears_the_deferral`;
* the dedicated rows test;
* two isolation tests, one for each hold.

The helper first checks the four step-row properties the criterion names:

* the `test` row is `completed`;
* its `retry_count` is 0;
* its `release_count` is 0;
* every instance is claimed once (`claim_epoch` 1, one claim per instance).

Then it checks the run row: `running` (or `failed` at C′), at
`test_gate_absent`.

**③ Hold-breaking mutations.** I measured which of them light up this file
and which assertion they redden. The source is the runner's own logs, read
by `final/probes/p_hold_ign.py` into
`final/logs/criteria/hold_mutations_in_absence_file.txt`.

The first two write the rows directly (SCHEDROWRETRY, SCHEDROWSTATUS):

* `SCHEDROWRETRY targeted_line_hits=127 igniting_tests_in_absence_file=7 red_in_absence_file=7` in `final/logs/criteria/hold_mutations_in_absence_file.txt`. The message is `deferral charged a retry: 37` in `final/logs/mutations/SCHEDROWRETRY.txt`.
* `SCHEDROWSTATUS targeted_line_hits=127 igniting_tests_in_absence_file=7 red_in_absence_file=7` in `final/logs/criteria/hold_mutations_in_absence_file.txt`. The message is `test row status drifted: pending` in `final/logs/mutations/SCHEDROWSTATUS.txt`.

The rest break a hold (S1, VALVEREACH, HOST_HOLD_INERT, TICK_HOLD_INERT):

* `S1 targeted_line_hits=36 igniting_tests_in_absence_file=8 red_in_absence_file=8` in `final/logs/criteria/hold_mutations_in_absence_file.txt`
* `VALVEREACH targeted_line_hits=115 igniting_tests_in_absence_file=7 red_in_absence_file=1` in `final/logs/criteria/hold_mutations_in_absence_file.txt`
* `HOST_HOLD_INERT targeted_line_hits=31 igniting_tests_in_absence_file=8 red_in_absence_file=7` in `final/logs/criteria/hold_mutations_in_absence_file.txt`
* `TICK_HOLD_INERT targeted_line_hits=153 igniting_tests_in_absence_file=8 red_in_absence_file=2` in `final/logs/criteria/hold_mutations_in_absence_file.txt`

Each of the four reddens the run-row assertion of the same helper:
`the run row moved while the gate was silent: status=completed node=test_gate_absent`
(in `final/logs/mutations/S1.txt`, `VALVEREACH.txt`, `HOST_HOLD_INERT.txt`
and `TICK_HOLD_INERT.txt`).

`TICK_HOLD_INERT` now edits `core/scheduler.py`: the tick's
`if deferral["state"] == "silent":` becomes `if False:`. Its round-9 edit,
in `gate_deferral.observe_run`, keeps its meaning under a new name,
OBSERVENONE.

**Why a broken hold reddens the run row and not a step row.** When a hold
lets the run advance, the run leaves through `test_gate_absent`'s only
transition, `to: null`, and it writes no step row.

* The four step-row assertions come first in the helper, and they pass.
* The next assertion, on the run row, fails and says so:
  `status=completed node=test_gate_absent`.
* In this catalog, the step-row assertions are reddened by the two mutations
  that write the rows (SCHEDROWRETRY, SCHEDROWSTATUS), and each names its
  number.

**Why VALVEREACH and TICK_HOLD_INERT redden fewer tests in this file.** The
two holds are in series.

* A broken tick hold lets the tick walk on into `advance_run`, where the host
  hold still refuses. So in the tests that run both holds, nothing visible
  changes.
* A broken host hold is visible there, because the second driver reaches
  `advance_run` directly.

So each hold also has a test that runs it alone:

* `test_the_tick_hold_alone_keeps_the_rows_where_the_absence_left_them`: the
  host's `hold_blocks_advance` answers False, and the tick is the only driver;
* `test_the_host_hold_alone_keeps_the_rows_where_the_absence_left_them`: the
  tick is handed `none`, and the real `observe_run` still writes the ledger.

VALVEREACH and TICK_HOLD_INERT redden the tick-alone test, and
HOST_HOLD_INERT reddens the host-alone test (the `RED` lines in
`final/logs/criteria/hold_mutations_in_absence_file.txt`).

**④ Point 2 of the original criterion.** The sentence "等待期间步骤回到
pending" (the step returns to `pending` while it waits) was wrong. It does not
happen.

While the gate is silent:

* the `test` row stays `completed`, with `retry_count` 0, `release_count` 0
  and `claim_epoch` 1;
* each instance is claimed once;
* the run row is `running`, parked at `test_gate_absent`.

The source is a real tick drive by `final/probes/p_rows.py`:

* `ROW {'id': 2, 'step_id': 'test', 'status': 'completed', 'retry_count': 0, 'release_count': 0, 'claim_epoch': 1}` in `final/logs/criteria/rows_during_hold_cand.txt`
* `CLAIMS {('implement', 1): 1, ('test', 2): 1}` in `final/logs/criteria/rows_during_hold_cand.txt`
* `RUN {'status': 'running', 'current_node': 'test_gate_absent', 'error_reason': None}` in `final/logs/criteria/rows_during_hold_cand.txt`

The later rows are `pending` because they have never been claimed
(`claim_epoch` 0); nothing moved them back. The hold works by refusing to
advance the run. It does not put the step back in the queue.

### `the-delivery-and-its-contract-documents-are-neither-corrupted-nor-stale`

**The repeat list is a (file, statement, consecutive count) triple, and the
counts match the tree:**

* `derived_equals_listed_with_counts True` in `final/logs/criteria/p_dup8_counts_cand.txt`
* `listed_entries 13 listed_count_sum 14` in `final/logs/criteria/p_dup8_counts_cand.txt`
* The r9 review's own count agrees: `A_sites_total 14 files 11` and
  `A_non_test_sites 0`, both in `final/logs/criteria/p_dup8_cand.txt`. The base
  has `A_sites_total 15 files 12` in `final/logs/criteria/p_dup8_base.txt`.
  The extra site on the base is the duplicated comment line in the
  checker's own file, which this round removed.

The review's probe also prints `B_set_equal False` in
`final/logs/criteria/p_dup8_cand.txt`. That line compares
(file, statement) pairs with the list's triples, so the shapes cannot match.
The count comparison above is `p_repeats.py`'s.

The checker now counts comment lines as well
(`test_a_duplicated_comment_line_is_red`). The three r8 fixes still hold:
`DEFECT_CHECK aitelier/tools/run_tests/impl.py lines [692]` in `final/logs/criteria/p_dup8_cand.txt`.

**Splice damage fixed:**

* `core/scheduler.py:30-31`: the stray `n` and the orphaned line;
* `tests/unit/test_run_tests_unmeasured_declaration.py:617-618`: the dropped line is restored;
* `tests/unit/test_no_verbatim_previous_line_dup.py:30-31`: the duplicated comment;
* `aitelier/tools/run_tests/impl.py:747`: the indentation.

**Stale text fixed:**

* the header of `tools/mutation_catalog/mutations.py` ("rev 8", "a copy of the tree");
* `docs/repo-gate-unmeasured-protocol.md:292-293`;
* `GILCLAMP` is now `CEILCLAMP`.

**Read-back.** `final/logs/readback/full_diff_a057ce66.txt` is
`git diff a057ce66..96721904` over every changed line. I read it hunk by
hunk, looking for:

* a middle line dropped between two kept ones;
* two lines fused into one;
* a sentence cut off;
* an added line identical to the one before it.

I found none. There is also one word-diff per changed file in
`final/logs/readback/`.

### `the-four-witnesses-explain-the-incident-they-do-not-get-reclassified`

* `git diff 7bcbe667 96721904 -- evidence/` is empty, with `BARE_RC=0`
  (`final/logs/criteria/four_witnesses_evidence_diff.txt`).
* This round's diff is empty too, with `BARE_RC=0`: `git diff --exit-code dcfa78729628f0cbbbc703a44d0e97098a3b9c8f 96721904a7d9b6b461109cfcded6d0abd03f469c -- configs/ evidence/` in `final/logs/criteria/four_witnesses_this_round_diff.txt`.
* `git diff 7bcbe667 96721904 -- configs/` is not empty. `--exit-code` gives
  `BARE_RC=1` (`final/logs/criteria/four_witnesses_configs_diff.txt`).
  - It is `configs/coding_impl.yaml | 60 +` in `final/logs/criteria/four_witnesses_configs_diffstat.txt`.
  - Earlier rounds made it, in commits `e8088249`, `6c43702f` and `eed64145` (`final/logs/criteria/four_witnesses_configs_commits.txt`).
  - Beyond comment lines, it adds two lines to the `test` step:
    `match: { field: "repo_gate_absent", value: true }` in `final/logs/criteria/four_witnesses_configs_diff.txt`
    and its `to: "test_gate_absent"`. It also adds the `test_gate_absent`
    gate, whose only transition is `to: null`.
  - It adds no line containing `max_loop`. `max_loop: 3` is still at
    `configs/coding_impl.yaml:228:        max_loop: 3` in `final/logs/criteria/four_witnesses_max_loop.txt`.
  - It touches no evidence file.

The edge sends a declared absence to a terminal gate instead of to
`test_outcome → implement`. It does not reclassify any of the four witnesses.

### `the-named-mutations-are-applied-as-written`

**The catalog** (`tools/mutation_catalog/mutations.py`) has one entry per
mutation the runner ran: `merged from 4 control(s) and 48 mutation(s)` in `final/logs/mutations/summary.txt`.

* The seven goal-table entries (N9, M21, M21b, G2, G2b, DUPIMPL, RESTART) are
  byte-identical to the base's edits. Only M21's and M21b's `targeted` lists
  changed (see above).
* The nine names r8 gave new meanings (S1, ABSTERM, ABSTERM2, VALVEREACH, D1,
  D4, M2, M19, TAILONLY2) now carry the review's edits again.
* r8's edits for them moved to new names: TIMEOUT60, ABSTERMTEXT,
  TERMREASONCYCLE, VALVEOFF, NEVEREXPIRE, READNONDICT and DECLNONE. r8's M2
  and TAILONLY2 edits were dropped, because they matched the restored
  TAILONLY2 and M2 edits.
* DUPHITS's anchor is a real newline again.
* New this round:
  - SCHEDROWRETRY and SCHEDROWSTATUS, the review's scheduler-hold row
    mutations;
  - OBSERVENONE;
  - the `core/scheduler.py` TICK_HOLD_INERT.
  - The catalog's header records where each name's edit came from.

**The runner.**

1. Every control and every mutation runs in its own
   `git worktree add --detach`, which is removed afterwards.
2. A dirty root is a failure: exit 3, before anything runs. I measured this
   in a throwaway worktree of `96721904` with one planted file:
   `ROOT NOT CLEAN before the run:` and `BARE_RC=3`, both in
   `final/logs/criteria/runner_refuses_a_dirty_root.txt`.
3. The empty control runs at both scopes, the targeted union and the full
   scope. If either bare RC is not 0, the runner reports no kill and exits 2.
   I measured this in the same throwaway worktree. The environment set the
   episode ceiling away from its default, so the G2 reader's control is red:
   `CONTROL NOT GREEN (targeted rc=1, full rc=1). Refusing to report kills.`
   and `BARE_RC=2`, both in `final/logs/criteria/runner_refuses_a_red_control.txt`.
   All four real shards' controls were green; the four `CONTROL_shard` rows
   are the table's first rows.
4. Anchors are checked in the runner before anything is applied, and each
   must match exactly once; a miss is recorded as `anchor-error`, never as a
   kill. `test_each_mutation_anchor_hits_exactly_once` is no longer in the
   suite.
5. Ignition counts executions of the lines the mutation wrote, using
   `sys.monitoring` LINE events (`tools/mutation_catalog/ignition_plugin.py`).
   For a deletion, it counts the line before the gap. For kind "text" entries
   (N9, DUPIMPL, DUPYIELD, DUPHITS) the property is the text itself, so
   ignition is the number of reads of the mutated file (`file_reads` in the
   table).
6. A red test counts as a killer only if both of these hold:
   - the control does not have it red;
   - it is not a source-text witness, that is, a test that read the mutated
     file's text but executed none of the mutated lines. For kind "text"
     entries the reader of the text is the witness, so it does count.
7. After each mutation the worktree's `git status --porcelain` must be empty:
   `# porcelain after restore=''` in `final/logs/mutations/N9.txt`, and the
   same line is in every mutation log.
8. The output is written to `final/logs/mutations/`.

**Scope, with its cross product.**

* "full" is `tests/unit tests/skillflow`: `## full scope: /usr/local/bin/python -m pytest -q -rfE -p no:cacheprovider -p ignition_plugin tests/unit tests/skillflow` in `final/logs/mutations/CONTROL_shard1of4.txt`.
* That is 48 mutations × {targeted union, `tests/unit` + `tests/skillflow`}.
* `tests/integration` and `tests/e2e` are covered only by the whole-suite runs.
* `# killed: 48 of 48` in `final/logs/mutations/summary.txt`.

**Load-sensitive tests in the full-scope killer column.** Some rows list
tests from `tests/unit/test_bash_cleanup_is_async.py`, for example
`test_a_timed_out_command_keeps_the_loop_responsive`. They are timing tests
of loop responsiveness. The same tests went red in the whole-suite runs that
shared the host with other containers (see Whole suite). They are not what
kills any mutation at the targeted scope: every row has a targeted bare RC of
1, from tests in the mutation's own targeted list.

<!-- BEGIN final/logs/mutations/summary.txt -->
| name | status | ignition (targeted / full) | targeted bare RC | full bare RC | killers (targeted / full) | killer tests | log |
|---|---|---|---|---|---|---|---|
| CONTROL_shard1of4 | control | – | 0 | 0 | 78 passed, 1 warning in 11.40s / 4072 passed, 9 skipped, 4 warnings in 330.77s (0:05:30) | reds: 0 / 0 | `final/logs/mutations/CONTROL_shard1of4.txt` |
| CONTROL_shard2of4 | control | – | 0 | 0 | 41 passed, 1 warning in 4.67s / 4072 passed, 9 skipped, 4 warnings in 318.74s (0:05:18) | reds: 0 / 0 | `final/logs/mutations/CONTROL_shard2of4.txt` |
| CONTROL_shard3of4 | control | – | 0 | 0 | 93 passed, 1 warning in 11.87s / 4072 passed, 9 skipped, 4 warnings in 329.73s (0:05:29) | reds: 0 / 0 | `final/logs/mutations/CONTROL_shard3of4.txt` |
| CONTROL_shard4of4 | control | – | 0 | 0 | 78 passed, 1 warning in 11.42s / 4072 passed, 9 skipped, 4 warnings in 330.21s (0:05:30) | reds: 0 / 0 | `final/logs/mutations/CONTROL_shard4of4.txt` |
| N9 | killed | file_reads 1 / 24 | 1 | 1 | 1 / 1 | test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop | `final/logs/mutations/N9.txt` |
| M21 | killed | line_hits 1 / 101 | 1 | 1 | 1 / 2 | test_the_r4_shaped_witness_flips_under_the_literal_m21; test_M21_the_tree_witnesses_go_red_on_the_actual_mutation[line.startswith(_REPO_GATE_UNMEASURED_PREFIX)-_REPO_GATE_UNMEASURED_PREFIX | `final/logs/mutations/M21.txt` |
| M21b | killed | line_hits 1 / 28 | 1 | 1 | 1 / 1 | test_the_r4_shaped_witness_flips_under_the_literal_m21b | `final/logs/mutations/M21b.txt` |
| G2 | killed | line_hits 2 / 2 | 1 | 1 | 1 / 1 | test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded | `final/logs/mutations/G2.txt` |
| G2b | killed | line_hits 2 / 2 | 1 | 1 | 1 / 3 | test_a_timed_out_command_keeps_the_loop_responsive; test_escalating_to_sigkill_keeps_the_loop_responsive; test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded | `final/logs/mutations/G2b.txt` |
| DUPIMPL | killed | file_reads 1 / 22 | 1 | 1 | 1 / 3 | test_a_timed_out_command_keeps_the_loop_responsive; test_cancelling_an_ordinary_command_keeps_the_loop_responsive; test_no_verbatim_previous_line_dup_in_non_test_code | `final/logs/mutations/DUPIMPL.txt` |
| RESTART | killed | line_hits 5 / 33 | 1 | 1 | 1 / 7 | test_a_timed_out_command_keeps_the_loop_responsive; test_cancelling_an_ordinary_command_keeps_the_loop_responsive; test_during_a_deferral_hold_the_step_claim_is_held_not_released; test_the_deferral_ledger_is_not_persisted_so_a_restart_re_measures (+3) | `final/logs/mutations/RESTART.txt` |
| ABSCEIL | killed | line_hits 2 / 2 | 1 | 1 | 1 / 1 | test_no_environment_can_raise_the_episode_ceiling_past_six_hours | `final/logs/mutations/ABSCEIL.txt` |
| ABSENCEWORD | killed | line_hits 1 / 2 | 1 | 1 | 1 / 1 | test_the_absent_terminal_checker_refuses_a_sentence_that_blames_the_code | `final/logs/mutations/ABSENCEWORD.txt` |
| ABSREL | killed | line_hits 21 / 118 | 1 | 1 | 1 / 1 | test_an_absence_whose_failures_are_all_known_red_still_does_not_pass | `final/logs/mutations/ABSREL.txt` |
| ABSSEED | killed | line_hits 11 / 80 | 1 | 1 | 1 / 1 | test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative | `final/logs/mutations/ABSSEED.txt` |
| ABSSTATE | killed | line_hits 8 / 63 | 1 | 1 | 1 / 1 | test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative | `final/logs/mutations/ABSSTATE.txt` |
| ABSTERM | killed | line_hits 6 / 8 | 1 | 1 | 1 / 1 | test_the_absent_terminal_checker_refuses_a_sentence_that_blames_the_code | `final/logs/mutations/ABSTERM.txt` |
| ABSTERM2 | killed | line_hits 4 / 5 | 1 | 1 | 1 / 1 | test_the_absent_terminal_checker_refuses_a_sentence_that_blames_the_code | `final/logs/mutations/ABSTERM2.txt` |
| ABSTERMTEXT | killed | line_hits 2 / 2 | 1 | 1 | 2 / 3 | test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence; test_an_expired_absence_names_the_absence_and_never_the_code; test_the_absent_terminal_checker_refuses_a_sentence_that_blames_the_code | `final/logs/mutations/ABSTERMTEXT.txt` |
| ABSWRITE | killed | line_hits 11 / 80 | 1 | 1 | 1 / 1 | test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative | `final/logs/mutations/ABSWRITE.txt` |
| ATTEMPTS3 | killed | line_hits 1 / 13 | 1 | 1 | 1 / 1 | test_one_step_holds_the_scheduler_for_at_most_one_gate_run | `final/logs/mutations/ATTEMPTS3.txt` |
| CEILCLAMP | killed | line_hits 12 / 323 | 1 | 1 | 2 / 3 | test_G2_episode_max_seconds_is_hard_capped_against_1e9; test_no_environment_can_raise_the_episode_ceiling_past_six_hours; test_the_episode_ceiling_cannot_be_removed | `final/logs/mutations/CEILCLAMP.txt` |
| D1 | killed | line_hits 17 / 17 | 1 | 1 | 2 / 2 | test_a_declaration_survives_output_far_past_the_retention_bound; test_re_acquisition_is_bounded_and_the_last_verdict_is_the_reported_one | `final/logs/mutations/D1.txt` |
| D4 | killed | line_hits 14 / 14 | 1 | 1 | 9 / 11 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+7) | `final/logs/mutations/D4.txt` |
| DECLNONE | killed | line_hits 1 / 45 | 1 | 1 | 1 / 1 | test_a_declaration_survives_output_far_past_the_retention_bound | `final/logs/mutations/DECLNONE.txt` |
| DUPHITS | killed | file_reads 1 / 4 | 1 | 1 | 1 / 2 | test_named_test_repeats_are_pinned_and_current; test_the_checker_file_is_clean_under_its_own_rule | `final/logs/mutations/DUPHITS.txt` |
| DUPYIELD | killed | file_reads 1 / 4 | 1 | 1 | 1 / 2 | test_named_test_repeats_are_pinned_and_current; test_the_checker_file_is_clean_under_its_own_rule | `final/logs/mutations/DUPYIELD.txt` |
| H2 | killed | line_hits 30 / 32 | 1 | 1 | 8 / 11 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+7) | `final/logs/mutations/H2.txt` |
| HOST_HOLD_INERT | killed | line_hits 31 / 32 | 1 | 1 | 7 / 8 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+4) | `final/logs/mutations/HOST_HOLD_INERT.txt` |
| M19 | killed | line_hits 1 / 3 | 1 | 1 | 1 / 3 | test_unreliable_repo_gate_identity_never_seeds_or_changes_baseline; test_a_bounded_fragment_is_not_a_record_on_its_own; test_a_truncated_gate_with_no_record_at_all_is_a_red | `final/logs/mutations/M19.txt` |
| M2 | killed | line_hits 1 / 45 | 1 | 1 | 1 / 2 | test_escalating_to_sigkill_keeps_the_loop_responsive; test_a_bounded_fragment_is_not_a_record_on_its_own | `final/logs/mutations/M2.txt` |
| M21BPAIR | killed | line_hits 2 / 51 | 1 | 1 | 1 / 3 | test_a_log_echo_of_the_case_prefix_mid_line_is_not_a_case_record; test_a_mid_line_echo_kills_the_in_operator_on_the_case_channel; test_the_r4_shaped_witness_flips_under_the_literal_m21b | `final/logs/mutations/M21BPAIR.txt` |
| M21PAIR | killed | line_hits 2 / 123 | 1 | 1 | 1 / 6 | test_a_log_echo_of_the_prefix_mid_line_is_not_a_declaration; test_a_mid_line_echo_kills_the_in_operator_on_the_declaration_channel; test_a_mid_line_prefix_with_valid_json_after_it_is_still_not_a_record; test_the_r4_shaped_witness_flips_under_the_literal_m21 (+2) | `final/logs/mutations/M21PAIR.txt` |
| NEVEREXPIRE | killed | line_hits 149 / 175 | 1 | 1 | 1 / 3 | test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence; test_an_expired_absence_names_the_absence_and_never_the_code; test_the_episode_does_not_restart_on_every_tick | `final/logs/mutations/NEVEREXPIRE.txt` |
| OBSERVENONE | killed | line_hits 38 / 107 | 1 | 1 | 8 / 14 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+10) | `final/logs/mutations/OBSERVENONE.txt` |
| RC2 | killed | line_hits 8 / 28 | 1 | 1 | 8 / 27 | test_four_real_reds_still_exhaust_the_cycle_limit; test_real_repo_gate_a_then_a_plus_b_exposes_b_and_keeps_detail_stable; test_real_repo_gate_fixed_then_rebroken_case_becomes_new; test_repo_gate_without_state_dir_stays_strict (+23) | `final/logs/mutations/RC2.txt` |
| READABSENCE | killed | line_hits 27 / 178 | 1 | 1 | 1 / 3 | test_a_gate_that_finally_answers_clears_the_deferral; test_the_tick_refuses_to_claim_while_the_gate_is_silent; test_a_report_that_graded_the_code_states_no_absence | `final/logs/mutations/READABSENCE.txt` |
| READNONDICT | killed | line_hits 32 / 183 | 1 | 1 | 4 / 4 | test_a_report_that_is_not_an_object_states_no_absence[3]; test_a_report_that_is_not_an_object_states_no_absence[absent]; test_a_report_that_is_not_an_object_states_no_absence[payload0]; test_a_report_that_is_not_an_object_states_no_absence[payload1] | `final/logs/mutations/READNONDICT.txt` |
| RETFLAG | killed | line_hits 8 / 59 | 1 | 1 | 8 / 9 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+5) | `final/logs/mutations/RETFLAG.txt` |
| S1 | killed | line_hits 36 / 71 | 1 | 1 | 10 / 10 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+6) | `final/logs/mutations/S1.txt` |
| SCHEDROWRETRY | killed | line_hits 127 / 129 | 1 | 1 | 7 / 9 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+5) | `final/logs/mutations/SCHEDROWRETRY.txt` |
| SCHEDROWSTATUS | killed | line_hits 127 / 129 | 1 | 1 | 7 / 9 | test_a_gate_that_finally_answers_clears_the_deferral; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence; test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence (+5) | `final/logs/mutations/SCHEDROWSTATUS.txt` |
| TAILONLY2 | killed | line_hits 1 / 45 | 1 | 1 | 1 / 1 | test_a_declaration_survives_output_far_past_the_retention_bound | `final/logs/mutations/TAILONLY2.txt` |
| TERMREASONCYCLE | killed | line_hits 2 / 3 | 1 | 1 | 1 / 2 | test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence; test_an_expired_absence_names_the_absence_and_never_the_code | `final/logs/mutations/TERMREASONCYCLE.txt` |
| TICK_HOLD_INERT | killed | line_hits 153 / 192 | 1 | 1 | 2 / 5 | test_pole_c_a_tiny_ceiling_inside_the_window_is_honoured_and_uncharged; test_the_tick_hold_alone_keeps_the_rows_where_the_absence_left_them; test_a_timed_out_command_keeps_the_loop_responsive; test_during_a_deferral_hold_the_step_claim_is_held_not_released (+1) | `final/logs/mutations/TICK_HOLD_INERT.txt` |
| TIMEOUT60 | killed | line_hits 1 / 13 | 1 | 1 | 2 / 2 | test_the_budget_goes_red_and_names_the_number_that_moved[3-gate; test_the_budget_goes_red_and_names_the_number_that_moved[4-gate | `final/logs/mutations/TIMEOUT60.txt` |
| TIMEOUTBIG | killed | line_hits 1 / 13 | 1 | 1 | 1 / 13 | test_four_real_reds_still_exhaust_the_cycle_limit; test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle; test_one_step_holds_the_scheduler_for_at_most_one_gate_run; test_real_repo_gate_a_then_a_plus_b_exposes_b_and_keeps_detail_stable (+9) | `final/logs/mutations/TIMEOUTBIG.txt` |
| TIMEOUT_NOT_UNMEASURED | killed | line_hits 1 / 43 | 1 | 1 | 1 / 2 | test_a_killed_gate_is_unmeasured_and_its_returncode_is_not_why; test_a_runner_that_cannot_start_is_unmeasured | `final/logs/mutations/TIMEOUT_NOT_UNMEASURED.txt` |
| VALVEBYPASS | killed | line_hits 10 / 45 | 1 | 1 | 2 / 2 | test_the_valve_is_a_simple_bound_and_the_deferral_path_cannot_reach_it; test_the_valve_fires_on_excess_claims_regardless_of_episode | `final/logs/mutations/VALVEBYPASS.txt` |
| VALVEOFF | killed | line_hits 5 / 40 | 1 | 1 | 4 / 5 | test_the_valve_is_a_simple_bound_and_the_deferral_path_cannot_reach_it; test_the_episode_ceiling_ends_the_run_before_the_valve_is_needed; test_the_valve_fires_on_excess_claims_regardless_of_episode; test_the_valve_is_narrow_not_blanket (+1) | `final/logs/mutations/VALVEOFF.txt` |
| VALVEREACH | killed | line_hits 115 / 115 | 1 | 1 | 3 / 3 | test_the_tick_hold_alone_keeps_the_rows_where_the_absence_left_them; test_during_a_deferral_hold_the_step_claim_is_held_not_released; test_the_tick_refuses_to_claim_while_the_gate_is_silent | `final/logs/mutations/VALVEREACH.txt` |
| WAITCLAMP | killed | line_hits 1 / 181 | 1 | 1 | 1 / 1 | test_the_wait_cannot_be_widened_until_the_ceiling_disappears | `final/logs/mutations/WAITCLAMP.txt` |
<!-- END final/logs/mutations/summary.txt -->

### `unmeasured-is-declared-never-inferred`

The exit-code sweep:

* `"0": "measured_pass"` in `final/logs/criteria/p_crit_cand.txt`;
* every other code (1, 2, 3, 124, 137, 255, −1, −9) is `measured_fail`:
  `"c7_any_unmeasured": []` in `final/logs/criteria/p_crit_cand.txt`;
* `"c7_has_legacy_constant": false` in `final/logs/criteria/p_crit_cand.txt`;
* `"c7_contract_error_rc2": "measured_fail"` in `final/logs/criteria/p_crit_cand.txt`.

The base is the same (`"c7_any_unmeasured": []` in `final/logs/criteria/p_crit_base.txt`).

The two mutations are RC2 and TIMEOUT_NOT_UNMEASURED; the table has their
rows.

## Not done, and why

* **`held_keys` in `p_crit`.** Its value is an error:
  `module 'core.gate_deferral' has no attribute 'reset'` in `final/logs/criteria/p_crit_cand.txt`.
  The probe calls a function the tree does not have. That happens on the
  base as well, and the probe is the review's, so I did not change it.
* **`configs/coding_impl.yaml` has two overlapping comment paragraphs** above
  the absence edge, both from earlier rounds. One begins "THE ABSENCE EDGE
  LIVES ON THIS STEP", the other says the edge "IS ON THIS STEP, and it is a
  FLAG match". Editing them would make this round's `configs/` diff non-empty
  and would need its own justification under the four-witnesses criterion.
  So they are left for a round that owns `configs/`.
* **The C′ terminal text counts ticks, not gate runs.** It says
  `(run_tests.sh, 41 attempt(s))` in `final/logs/criteria/POLE_Cexp_cand.txt`,
  while `"gate_calls": 1` in `final/logs/criteria/POLE_Cexp_cand.txt`. The r6–r9 reviews
  recorded this, and this round's brief does not name it.
