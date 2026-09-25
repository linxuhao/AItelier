# Delivery notes: gate-not-run, rev 5 (2026-09-25)

Node `harness.a-gate-that-could-not-run-is-not-a-failing-test` rev 5, attempt
`attempt-16bcffa219ec4bceac36505cfcc65aaa`. Branch
`director/gatenotrun-r5-20260925`, base `7c8f9e5aeef8c962e5de180ffe6029d959b464a2`.
Code, tests, config and doc: commit `afa808b64de248968c6705128db94248b62de343`;
every evidence run below was on it. The commit after it adds only
`logs/gate_not_run_r5/` and this file. The whole suite runs on the last commit
of the branch (log outside the tree:
`~/.AItelier/worktrees-scratch/gn5-logs/suite_<sha>.txt`).

Every test run below was in a throwaway `aitelier:latest` container
(`docker run --rm --init -m 3g`, script `logs/gate_not_run_r5/scripts/run.sh`),
with at most 4 throwaway containers on the server; the script counts them
before each launch and refuses at 4. Each log starts with the command, the
in-container `git rev-parse HEAD` and the import paths, and ends with
`BARE_RC`.

## 1. Attribution: own, not own, unattributable

`_retained_findings` (`aitelier/tools/run_tests/impl.py`) classes every
`manifest.json` under the ticket:

* `own`: the order of creation was observed, the first entry is a directory,
  and a manifest inside it names this repository;
* `not_own`: the gate's report was identified and this manifest is another;
* `unattributable`: everything else. That covers an unobserved order, a first
  entry that is a file, and a first-entry directory with no manifest naming
  this repository.

The fallback to the repository a manifest names is deleted. When the ticket is
`unattributable` and any red lies under it, `repo_gate.measured` is
`unattributable`, `run_tests` returns `repo_gate_unattributable: true` and
`repo_gate_absent: false`, `failure_identity_error` is set (the baseline is
neither seeded nor pruned), every red is listed in `repo_gate.unattributed_reds`
and in `failures[]`, and `configs/coding_impl.yaml` sends the run to
`test_gate_report_unattributable`, a gate in `end_conditions` with result
`failed`. With no red under an unattributable ticket, the rules that were
already there apply (`inotify_unavailable_no_red`: unmeasured, absence gate;
`lockfile_first_green_gate`: measured_pass, done).

Director ruling rev 5, recorded by name: a missing runner (npm or pytest) ends
at `test_evidence_missing`. It wins over an unattributable red
(`pytest_runner_unavailable_beside_unattributable_red`): the flag
`repo_gate_unattributable` is set only when `infrastructure_unavailable` is not.

`aitelier/gate_evidence.py:report_state` reads `unattributable` as its own
state, which `release_disposition` gives as `unresolved`.

## 2. The outcome table, candidate against base

`tests/unit/test_repo_gate_outcome_table.py`, 33 rows in `ROWS`. Each row
drives the real `run_tests` once with a stub gate script (`TABLE_GATE`) and the
real admission relay against a fixture harness, then walks the transitions of
the real `configs/coding_impl.yaml`. No engine render. Cells:
attribution / measured / `repo_gate_absent` / next node / identity error.
Base values are from the same test file placed untracked on the base tree
(the base has no `report_attribution`, so its attribution is `None`).

| row | candidate | base |
|---|---|---|
| `clean_refusal` | own / unmeasured / true / test_gate_absent / false | same |
| `python_red_then_refused` | own / measured_fail / false / implement / false | same |
| `compile_red_then_refused` | own / measured_fail / false / implement / false | same |
| `second_manifest_same_repo` | own / measured_fail / false / implement / true | same |
| `foreign_unreadable_manifest` | own / measured_fail / false / implement / true | same |
| `missing_stage_report` | own / measured_fail / false / implement / true | same |
| `nested_two_levels` | own / measured_fail / false / implement / false | same |
| `empty_findings_failed_stage` | own / measured_fail / false / implement / false | same |
| `foreign_names_repo_gate_wrote_none` | unattributable / unattributable / false / test_gate_report_unattributable / true | unmeasured / true / test_gate_absent / true (**red**) |
| `foreign_names_repo_beside_own` | own / unmeasured / true / test_gate_absent / true | same |
| `other_repo_reports_beside_green_gate` | own / measured_pass / false / done / false | same |
| `declared_absence` | own / unmeasured / true / test_gate_absent / false | same |
| `declared_failed_is_not_an_absence` | own / measured_fail / false / implement / true | same |
| `gate_killed_at_its_timeout` | own / unmeasured / true / test_gate_absent / false | same |
| `exit_2_after_an_answered_request` | own / measured_fail / false / implement / true | same |
| `exit_1_after_a_refusal` | own / measured_fail / false / implement / true | same |
| `pytest_red_beside_refused_gate` | own / unmeasured / false / implement / false | same |
| `pytest_wall_beside_refused_gate` | own / unmeasured / true / test_gate_absent / false | same |
| `node_runner_unavailable_beside_refused_gate` | own / unmeasured / false / test_evidence_missing / false | same |
| `pytest_runner_unavailable_beside_refused_gate` | own / unmeasured / false / test_evidence_missing / false | same |
| `green_gate` | own / measured_pass / false / done / false | same |
| `answered_red` | own / measured_fail / false / implement / false | same |
| `lockfile_first_red_refused` | unattributable / unattributable / false / test_gate_report_unattributable / true | unmeasured / true / test_gate_absent / true (**red**) |
| `cachedir_first_red_refused` | unattributable / unattributable / false / test_gate_report_unattributable / true | unmeasured / true / test_gate_absent / true (**red**) |
| `file_first_red_refused` | unattributable / unattributable / false / test_gate_report_unattributable / true | measured_fail / false / implement / false (**red**) |
| `first_entry_names_other_repo_red_refused` | unattributable / unattributable / false / test_gate_report_unattributable / true | unmeasured / true / test_gate_absent / true (**red**) |
| `first_entry_names_this_repo_clean_refused` | own / measured_fail / false / implement / true | same |
| `report_without_manifest_red_refused` | unattributable / unattributable / false / test_gate_report_unattributable / true | unmeasured / true / test_gate_absent / false (**red**) |
| `lockfile_first_green_gate` | unattributable / measured_pass / false / done / false | measured_pass / false / done / true (**red**) |
| `pytest_runner_unavailable_beside_unattributable_red` | unattributable / unattributable / false / test_evidence_missing / true | unmeasured / false / test_evidence_missing / true (**red**) |
| `inotify_unavailable_own_red` | unattributable / unattributable / false / test_gate_report_unattributable / true | measured_fail / false / implement / false (**red**) |
| `inotify_unavailable_foreign_red` | unattributable / unattributable / false / test_gate_report_unattributable / true | measured_fail / false / implement / true (**red**) |
| `inotify_unavailable_no_red` | unattributable / unmeasured / true / test_gate_absent / false | same |

Candidate: 71 passed, BARE_RC 0, `logs/gate_not_run_r5/table_candidate.txt`
(one `ROW` JSON line per row, printed by `-s`). Base: 48 failed, 23 passed,
BARE_RC 1, `logs/gate_not_run_r5/table_base.txt`: 10 rows red in
`test_the_outcome_of_every_report_shape` (the ten marked above), all 33 in
`test_the_attribution_of_every_report_shape`, all 4 in
`test_the_gate_must_create_its_report_directory_first`, and
`test_the_doc_routing_table_is_rendered_from_rows`. Side by side:
`logs/gate_not_run_r5/rows_compare.txt`. Coverage: these 33 constructed
shapes, each driven once; no other order of creation and no other writer
timing was tried.

### The three observation-failure rows

`inotify_unavailable_own_red`, `inotify_unavailable_foreign_red`,
`inotify_unavailable_no_red` were forced two ways:

1. **Real exhaustion.** `logs/gate_not_run_r5/scripts/exhaust.py`, run as uid
   1099 (no process and no passwd entry for that uid on the host:
   `logs/gate_not_run_r5/inotify_uid1099_unused.txt`), calls the real
   `inotify_init1` until the kernel refuses: 128 instances held, errno 24
   (EMFILE), `fs.inotify.max_user_instances=128`. It then runs the three rows
   with `AITELIER_TABLE_INOTIFY_EXHAUSTED=1`, which makes the table use the
   real libc for them. Result: 6 passed (3 rows x the outcome and attribution
   tests), BARE_RC 0, each row's `why` ends
   `inotify_init1 failed (errno 24: Too many open files)`, `inotify` field
   `exhausted by the caller`. Log: `logs/gate_not_run_r5/inotify_exhausted.txt`.
2. **Syscall-layer stub** in the ordinary run: `_LibcWithoutInotify` replaces
   only `inotify_init1` on the object `ctypes.CDLL(None)` returns (it sets
   errno EMFILE and returns -1); `_FirstEntry` and `_retained_findings` run
   unchanged. `_FirstEntry` is not stubbed anywhere.

## 3. The protocol requirement "the gate creates its own report directory first"

`tests/unit/test_repo_gate_outcome_table.py::test_the_gate_must_create_its_report_directory_first`,
parametrized over the four violations: a lock file first
(`lockfile_first_red_refused`), a cache directory first
(`cachedir_first_red_refused`), the report written straight into the ticket
(`file_first_red_refused`), and another writer's directory naming another
repository first (`first_entry_names_other_repo_red_refused`). Each gate
retains one python red and is refused. A violation gives: attribution
`unattributable`, `retained_findings` null, exactly one unattributed red ending
`[python]: the python suite exited 1`, `measured: unattributable`, next node
`test_gate_report_unattributable`, and that red in `failures[]`. No red is
dropped and none is made up. On the base all four are red.

The requirement is stated in `docs/repo-gate-unmeasured-protocol.md`
("What the gate has to do", item 1) and in `aitelier/tools/run_tests/tool.yaml`.

## 4. The routing table, generated from `ROWS`

* Renderer: `render_routing_table()` in
  `tests/unit/test_repo_gate_outcome_table.py`; `write_routing_table()`
  replaces the block in the doc; `python -m tests.unit.test_repo_gate_outcome_table`
  runs it (log: `logs/gate_not_run_r5/render.txt`, BARE_RC 0).
* Markers in `docs/repo-gate-unmeasured-protocol.md`: begin
  `<!-- routing table: rendered from ROWS in tests/unit/test_repo_gate_outcome_table.py by render_routing_table(); edit ROWS and run `python -m tests.unit.test_repo_gate_outcome_table` -->`,
  end `<!-- routing table: end -->`.
* Byte comparison: `test_the_doc_routing_table_is_rendered_from_rows`.
* Rev 4 item 3 (every routing sentence cites its test) is withdrawn by name:
  `test_every_routing_sentence_cites_a_row` and its helpers are deleted.
* The r3 mutation table no longer cites the deleted
  `test_a_pytest_killed_at_its_wall_beside_a_refused_gate_is_not_an_absence`
  in its N2 and N3 rows; a note under the table names it as deleted and names
  its replacement. `logs/gate_not_run_r5/cited_tests.txt`: 57 cited tests
  found, 1 missing, which is that deleted test, named as deleted.
* `tests/unit/test_tree_level_accounting_witnesses.py::test_N9_the_protocol_text_has_a_reader`
  pinned routing phrases in `tool.yaml` that item 4 removes. The two goals
  conflict; N9 now pins the same properties on seven rows of the rendered
  table (`declared_absence`, `gate_killed_at_its_timeout`, `clean_refusal`,
  `exit_1_after_a_refusal`, `exit_2_after_an_answered_request`,
  `python_red_then_refused`, `pytest_red_beside_refused_gate`), the pointer in
  `tool.yaml`, and the scheduler sentences in the doc. The phrases it no
  longer pins are the ones item 4 required removed.
* Two exact pins in `tests/skillflow/test_config_loading.py` were extended with
  the new edge and the new terminal; no assertion was removed.

Routing-related prose still in `aitelier/tools/run_tests/tool.yaml`, quoted:

* "`node` (npm install/build/test checks) is present when the repo holds a
  node project and a failing check also flips `passed`."
* "Missing, duplicate, malformed, ambiguous, or truncated case identities make
  `passed_relative` false and do not seed or change the baseline."
* "A suite that collected NOTHING is reported as passed:false +
  no_tests_collected:true — a gate that checked nothing has not passed."
* "If no test runner can be provisioned at all the gate records a distinct,
  non-passing infrastructure_unavailable outcome."
* "`repo_gate.report_attribution` says whether the gate's report was
  identified under the ticket (`own`) or not (`unattributable`, with `why`)."
* "Where a report goes and what a repository gate run is worth: the routing
  table in `docs/repo-gate-unmeasured-protocol.md`, rendered from `ROWS` in
  `tests/unit/test_repo_gate_outcome_table.py`."

Routing-related prose still in `docs/repo-gate-unmeasured-protocol.md`
outside the table, quoted:

* "a gate that breaks it leaves its report unattributable"
* "The exit code is `0`, `1`, or anything else; what each is worth beside the
  relay's record and the report is in the table."
* "`test_gate_report_unattributable` is a terminal gate in `end_conditions`
  (result `failed`); the reds and where each was read are in the report's
  `failures[]` and in `repo_gate.unattributed_reds`. A report whose reds
  belong to no identified report sets `failure_identity_error`, so it neither
  seeds nor prunes the known-red baseline."
* "When the wait runs out, the tick logs `gate_deferral_reacquire` and
  advances along the gate's only edge, `test_gate_absent -> test`, which
  re-runs the `test` step alone."
* "A report with `failure_identity_error` (which includes every report whose
  reds are unattributable) gets `baseline_state: unreadable` and
  `passed_relative: false`, and writes nothing."

## 5. The real graph

`tests/skillflow/test_coding_impl_absence_needs_nothing_else_red.py::test_an_unattributable_red_ends_the_run_on_its_own_terminal`
drives the real `configs/coding_impl.yaml` through skillflow with a busy rig
and `TABLE_GATE` (`MODE=python_red`, `PRE=lockfile`): implement runs once, the
run fails with `test_gate_report_unattributable` in its `error_reason`, no
cycle limit, `test_gate_absent` never reached, one gate call. 19 passed with
`tests/skillflow/test_config_loading.py`, BARE_RC 0,
`logs/gate_not_run_r5/drive_unattributable.txt`.

## 6. Mutations of the new code

`logs/gate_not_run_r5/scripts/mutate.py` (each mutant replaces one exact
source text and appends a byte to an ignition file each time the mutated code
runs) and `mut_loop.sh` (one `git archive` copy of `afa808b6` per mutant),
against 7 files: the outcome table, both identity tests,
`test_repo_gate_red_beats_absence.py`, `test_repo_gate_admission.py`,
`test_tree_level_accounting_witnesses.py`,
`tests/skillflow/test_coding_impl_absence_needs_nothing_else_red.py`; 130
tests. Logs: `logs/gate_not_run_r5/mut/mut_<name>.txt` and `.ignition`.

| mutant | bare RC | ignition bytes | red / 130 |
|---|---|---|---|
| CONTROL (probe only) | 0 | 197 | 0 |
| B1 unobserved order falls back to the named report | 1 | 4 | 5 |
| B2 a file first entry is the report (the A3 case) | 1 | 6 | 3 |
| B3 a first directory without a manifest takes the named report | 1 | 6 | 8 |
| B4 the first directory is always the gate's | 1 | 93 | 11 |
| B5 unattributed reds not reported | 1 | 15 | 14 |
| B6 the outcome ignores unattributed reds | 1 | 15 | 14 |
| B7 a missing runner does not win | 1 | 10 | 1 |
| B8 no identity error beside unattributable reds | 1 | 10 | 9 |
| B9 findings without a manifest unread | 1 | 1 | 1 |
| B10 the failing call is not recorded | 1 | 3 | 3 |
| B11 the flag is not returned | 1 | 163 | 13 |
| B12 a second report is not an error | 1 | 82 | 4 |
| B13 release reads unattributable as failed | 1 | 9 | 8 |
| B14 no edge to the terminal (config) | 1 | n/a (config text) | 16 |

B2 ignites (6) and is killed by `file_first_red_refused` in the outcome,
attribution and protocol-requirement tests. B10 ignites only on the stubbed
inotify rows in this loop (3); the real-exhaustion run is section 2.

## 7. What I did not do, and why

* The pre-emptive writer that names this repository
  (`first_entry_names_this_repo_clean_refused`) is attributed `own`: by the
  definition in goal item 1 its directory is the first entry and names the
  repository, so it cannot be told apart from `second_manifest_same_repo`.
  The row records the result (measured_fail, back to implement, identity
  error) and the doc says so. Telling them apart needs something beyond the
  first-entry observation (for example the gate announcing its directory),
  which this card did not specify.
* `aitelier/gate_evidence.py`'s audit buckets were not changed: a state the
  bucket map does not know, which now includes `unattributable`, falls into
  `failed_gates`. Only `report_state` and `release_disposition` read it.
* The game repository's gate (`tools/godot_gate.py`) was not touched; it is
  another card. Nothing here ran the engine, `/playtest`, `/script` or the
  render lock.
* No merge, tag, release or State DAG write.
