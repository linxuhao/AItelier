# A gate that never ran is not a failing gate — the repository gate protocol

2026-09-21; rev 5 2026-09-25. How `run_tests` reads a repository's own gate
(`run_tests.sh`): what the gate has to write, how its report is found under
its ticket, and how failure identities are named. The game repository's own
gate is the other half of the pair and is **another card** (see "The other
card" below).

**Where a report goes, and what a gate run is worth, is stated in one place:
the routing table below.** It is rendered from `ROWS` in
`tests/unit/test_repo_gate_outcome_table.py`, whose rows each drive the real
`run_tests` and walk the transitions of the real `configs/coding_impl.yaml`.
`test_the_doc_routing_table_is_rendered_from_rows` fails when the table and
`ROWS` differ by one byte. The rest of this document and
`aitelier/tools/run_tests/tool.yaml` point here instead of restating it.

## Routing, row by row

Columns: `attribution` is `repo_gate.report_attribution.state`; `measured` is
`repo_gate.measured`; `repo_gate_absent` is the flag on the tool's return and
in `test_report.json`; `next` is the node the `test` step's outcome reaches in
`configs/coding_impl.yaml`; `identity error` is whether
`repo_gate.retained_error` or `failure_identity_error` is set. Each row
asserts every column but the last.

<!-- routing table: rendered from ROWS in tests/unit/test_repo_gate_outcome_table.py by render_routing_table(); edit ROWS and run `python -m tests.unit.test_repo_gate_outcome_table` -->

| row | attribution | measured | repo_gate_absent | next | identity error | the shape |
|---|---|---|---|---|---|---|
| `clean_refusal` | `own` | `unmeasured` | `true` | `test_gate_absent` | `false` | the engine refused the gate's only request; exit 2 |
| `python_red_then_refused` | `own` | `measured_fail` | `false` | `implement` | `false` | a python-stage red retained, then a refused `/script`; exit 2 |
| `compile_red_then_refused` | `own` | `measured_fail` | `false` | `implement` | `false` | an answered `/compile` red, then a refused `/script`; exit 2 |
| `second_manifest_same_repo` | `own` | `measured_fail` | `false` | `implement` | `true` | a python red, plus a second manifest naming the same repository, created after the gate's report directory |
| `foreign_unreadable_manifest` | `own` | `measured_fail` | `false` | `implement` | `true` | a python red, plus a truncated manifest elsewhere under the ticket |
| `missing_stage_report` | `own` | `measured_fail` | `false` | `implement` | `true` | a python red, plus a manifest stage whose report file is missing |
| `nested_two_levels` | `own` | `measured_fail` | `false` | `implement` | `false` | a python red retained two directories below the ticket |
| `empty_findings_failed_stage` | `own` | `measured_fail` | `false` | `implement` | `false` | `compile.json` says `passed: false`, `compile-findings.json` is `[]` |
| `foreign_names_repo_gate_wrote_none` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | another writer's report names this repository with a red; the gate's first entry holds no manifest; refused |
| `foreign_names_repo_beside_own` | `own` | `unmeasured` | `true` | `test_gate_absent` | `true` | the same, beside the gate's own clean report; refused |
| `other_repo_reports_beside_green_gate` | `own` | `measured_pass` | `false` | `done` | `false` | reports for another repository beside a green gate |
| `declared_absence` | `own` | `unmeasured` | `true` | `test_gate_absent` | `false` | `AITELIER_REPO_GATE_UNMEASURED={"state": "blocked"}`, exit 3 |
| `declared_failed_is_not_an_absence` | `own` | `measured_fail` | `false` | `implement` | `true` | the same line with `"state": "failed"`, exit 3 |
| `gate_killed_at_its_timeout` | `own` | `unmeasured` | `true` | `test_gate_absent` | `false` | the harness killed the gate at `REPO_GATE_TIMEOUT` |
| `exit_2_after_an_answered_request` | `own` | `measured_fail` | `false` | `implement` | `true` | the engine answered, then the gate exited 2 |
| `exit_1_after_a_refusal` | `own` | `measured_fail` | `false` | `implement` | `true` | the engine refused, then the gate exited 1 |
| `pytest_red_beside_refused_gate` | `own` | `unmeasured` | `false` | `implement` | `false` | AItelier's pytest red; the gate refused |
| `pytest_wall_beside_refused_gate` | `own` | `unmeasured` | `true` | `test_gate_absent` | `false` | AItelier's pytest killed at its wall; the gate refused (director ruling rev 4: a pytest killed at its wall measured nothing) |
| `node_runner_unavailable_beside_refused_gate` | `own` | `unmeasured` | `false` | `test_evidence_missing` | `false` | no npm (`node.skipped`); the gate refused (director ruling rev 5: a missing runner, npm or pytest, ends at `test_evidence_missing`) |
| `pytest_runner_unavailable_beside_refused_gate` | `own` | `unmeasured` | `false` | `test_evidence_missing` | `false` | no pytest could be provisioned; the gate refused (the same ruling) |
| `green_gate` | `own` | `measured_pass` | `false` | `done` | `false` | the engine answered green |
| `answered_red` | `own` | `measured_fail` | `false` | `implement` | `false` | the engine answered red; exit 1 |
| `lockfile_first_red_refused` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | the gate creates `.gate.lock` first, then its report with a python red; refused |
| `cachedir_first_red_refused` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | the gate creates a `cache` directory first, then its report with a python red; refused |
| `file_first_red_refused` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | the gate writes `manifest.json` and its python red straight into the ticket; refused |
| `first_entry_names_other_repo_red_refused` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | a writer that names another repository creates its report before the gate's; the gate retains a python red; refused |
| `first_entry_names_this_repo_clean_refused` | `own` | `measured_fail` | `false` | `implement` | `true` | a writer that names this repository with a red creates its report before the gate's; the gate is clean; refused |
| `report_without_manifest_red_refused` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | the gate's report directory holds a python red and no `manifest.json`; refused |
| `lockfile_first_green_gate` | `unattributable` | `measured_pass` | `false` | `done` | `false` | the gate creates `.gate.lock` first; no red anywhere; the engine answered green |
| `pytest_runner_unavailable_beside_unattributable_red` | `unattributable` | `unattributable` | `false` | `test_evidence_missing` | `true` | no pytest could be provisioned, beside the `lockfile_first` red (director ruling rev 5) |
| `inotify_unavailable_own_red` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | `inotify_init1` fails; the gate's own python red; refused |
| `inotify_unavailable_foreign_red` | `unattributable` | `unattributable` | `false` | `test_gate_report_unattributable` | `true` | `inotify_init1` fails; another writer's red naming this repository, beside the gate's clean report; refused |
| `inotify_unavailable_no_red` | `unattributable` | `unmeasured` | `true` | `test_gate_absent` | `false` | `inotify_init1` fails; no red anywhere; refused |

<!-- routing table: end -->

## What the gate has to do

A repository declares its gate by shipping an executable `run_tests.sh` at its
root. `run_tests` runs it with two variables set: `GODOT_BUILDER_URL` (the
admission relay, below) and `GATE_REPORT_DIR`, a new, empty directory for this
gate run alone (its **ticket**, `$AITELIER_HOME/gate-reports/rt-<UTC>-<8 hex>`).

1. **Its report directory comes first.** The gate's first creation under
   `GATE_REPORT_DIR` is a directory, and its report lives inside it. Nothing
   else (a lock file, a cache directory, a report written straight into
   `GATE_REPORT_DIR`, another writer's directory) may be created there before
   it. This is a requirement of the protocol, not an observation about any
   particular gate; a gate that breaks it leaves its report unattributable
   ("Which report is the gate's" below). Checked by
   `tests/unit/test_repo_gate_outcome_table.py::test_the_gate_must_create_its_report_directory_first`.
   The game gate meets it today: its first act is
   `mkdtemp(prefix="wuxia-godot-gate-", dir=parent)` (`tools/godot_gate.py:436`
   at `cee4a0e9`, read by review gnr4).
2. **The report.** Inside that directory: one `manifest.json` whose `repo`
   names the repository the gate ran for and whose `stages` name every stage
   it ran (`{"<stage>": {"report": "<stage>.json"}}`), a `<stage>.json` per
   stage, and a `<stage>-findings.json` list for the stages it judges itself.
   Reds are read from there, never from the gate's stdout.
3. **The declaration**, for a gate that knows it did not run: one whole line
   of its output,

   ```
   AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"..."}
   ```

   with `state` one of `unmeasured`, `not_run`, `blocked`. `failed` and `red`
   are not in that set. The line is read from the gate's whole output before
   the 2000-character tail is kept, so it survives a long log.
4. **Case records**, for a gate that keeps no report: one whole line per
   failed case, `AITELIER_REPO_GATE_CASE={"case_id":"stable/id","status":"failed","detail":"..."}`.
   A truncated, malformed, duplicated or ambiguous set is an identity error.

The exit code is `0`, `1`, or anything else; what each is worth beside the
relay's record and the report is in the table.

## Which report is the gate's (rev 5)

`_run_repo_gate` adds an inotify watch on the ticket before the gate starts
(`_FirstEntry`) and reads, after the gate exits, the first entry the kernel
reported created there. `_retained_findings` then puts every `manifest.json`
under the ticket into one of three classes:

* **`own`**: the order was observed, the first entry is a directory, and a
  `manifest.json` inside it (at any depth) names this repository. That is the
  gate's report.
* **`not_own`**: the gate's report was identified, and this one is not it.
  A second report naming this repository is an identity error; a report
  naming another repository is not read.
* **`unattributable`**: every other case — the order could not be observed
  (`inotify_init1` or `inotify_add_watch` failed, or the events could not be
  read), nothing was created, the first entry is not a directory, or the first
  entry holds no manifest naming this repository. Then no report under the
  ticket is known to be the gate's, and none is known not to be.

There is no fallback to the repository a report names. `repo_gate.report_attribution`
records `state`, `observed`, `first_entry`, `first_is_dir`, `why` (for
`unattributable`: the reason, and for an unobserved order the failing call and
its errno) and `reports` (each manifest's class). When the ticket is
`unattributable`, `repo_gate.unattributed_reds` lists every red under it —
every readable manifest's stages, and every non-empty `*-findings.json` no
readable manifest covers — each as `<path under the ticket> [<stage>]: <finding>`.

The first-entry observation does not tell a writer apart from the gate when
that writer creates a directory naming this repository before the gate's own:
by what the kernel reports, that directory is the gate's. The row
`first_entry_names_this_repo_clean_refused` records what this gives, and the
gate's own later report shows up as a second report for the repository.

Reds only accumulate inside the gate's own report: each readable part adds
its own (each entry of a `<stage>-findings.json`; the `errors[]`, else the
`summary`, of a stage report that says `passed: false` when its findings list
is empty or missing). A part that cannot be read adds to
`repo_gate.retained_error` and takes away nothing already read.

## How the graph reads the report

`run_tests` returns `repo_gate_absent` and `repo_gate_unattributable` in its
result dict, which the engine merges into the `test` step's flags, and
`configs/coding_impl.yaml` matches them as `{field: …, value: true}` FLAGS.
They are not `from_file` reads, for two reasons measured in round 5:
`from_file` resolves against the evaluating step's own output dir, so on the
`test_evidence` validator (which writes no file) it raised
`FileNotFoundError: Output file not found: test_report.json` on every
evaluation; and the engine's `_flags_match` calls `data.get(field)` on
whatever the file parses to, so a report that parses to a list takes the
resolver down. The file still carries both keys for `core/gate_deferral.py`
and for a reader.

`test_gate_report_unattributable` is a terminal gate in `end_conditions`
(result `failed`); the reds and where each was read are in the report's
`failures[]` and in `repo_gate.unattributed_reds`. A report whose reds belong
to no identified report sets `failure_identity_error`, so it neither seeds nor
prunes the known-red baseline.

## The admission relay (r2, 2026-09-24)

`_run_repo_gate` starts an `AdmissionRelay` (`aitelier/gate_admission.py`) for
each gate run and hands the gate its URL as `GODOT_BUILDER_URL`. The relay
forwards every request to the real builder. On the render routes (`/playtest`,
`/script`, `/x11_input_smoke`) it adds `render_wait_timeout_sec`
(`AITELIER_REPO_GATE_RENDER_WAIT_SECONDS`, default 1500) when the gate sent
none, so the request waits in the harness render queue behind a live owner.
For each request it records the route, the status and the owner fields of a
409 payload, as `answered`, `not_admitted`, `engine_error`, `unreachable` or
`abandoned`. The summary (from the LAST request) is written to
`<ticket>/admission.json` and to `repo_gate.admission`.

**The gate's client timeout counts from admission.** The game gate reads each
render answer with one socket timeout (`/script` 1800 s, `/playtest` 3600 s).
With the relay's 1500 s queue wait in front of it, a `/script` render had
300 s of that timeout left. The relay sends the gate an interim
`HTTP/1.1 100 Continue` every `AITELIER_REPO_GATE_KEEPALIVE_SECONDS`
(default 20) while the request waits in the harness queue. Python's
`http.client` reads past interim responses, and each one restarts the socket
timeout. At admission (a new row for the request's `operation_id` in the
harness's `GET /lifecycle` owner table) one more goes, then none, so the
render itself is bounded by the gate's own timeout, counted from admission.
Nothing is sent to an HTTP/1.0 client, or when `/lifecycle` cannot be read
(`admission_observed: false`). A request with no `operation_id` gets
`relay-<uuid>` so it can be watched. Each request record carries `keepalives`
and `admitted_after_sec`.

## Identities that survive a second run (r3)

A red gate's identities are read from the findings in its own report, one per
finding, never from the bounded tail of its stdout. The game gate writes the
observed value into a failing assertion's finding
(`"  <scenario> / <name>: <expr> -> actual <actual>; observed <observed>"`),
and its summary into `hard gate failed: <summary>`. A hash of that text is a
new id on every run. Each finding is named, in this order:

1. a failing assertion: `<stage>/<scenario>/<assertion name>@<frame>`, all
   three read from the matching row of the stage report
   (`behavior.scenarios[].asserts[]`). Findings are matched to rows by the
   text before ` -> actual `, in report order, so the same assertion failing
   at two frames is two ids;
2. a repeatability mismatch: `<stage>/repeatability/<scenario>/<name>@<frame>`,
   from the first row the finding embeds;
3. a finding that ends with the stage report's `summary`: a sha256 prefix of
   the text before the summary;
4. anything else: a sha256 prefix of the whole text.

Two findings with the same id are both kept, the second as `<id>~2`.

A real `playtest.json` reaches 442 MB, 441 MB of it `captures`, and parsing
it whole took 2.1 GB of memory. Above 64 MiB the report is read from its last
top-level `"behavior":` member to the end (`_stage_report_fields`); if that
tail does not parse as the report's own members, no row is read and the ids
fall back to rule 3 or 4.

## The scheduler's wait at `test_gate_absent` (round 5, r2, r3)

One step invocation makes ONE gate call (`REPO_GATE_UNMEASURED_ATTEMPTS = 1`),
so its worst-case hold is the gate's own timeout (`REPO_GATE_TIMEOUT`,
5400 s) rather than the 3 x 5400 + 2 x 60 s an in-step re-acquisition cost
(round 5, measured). Waiting for the gate is the SCHEDULER's job
(`core/gate_deferral.py`), and it protects two different resources:

* the wall-clock bound protects a SHARED resource — the scheduler and its
  poller may never be held by one step forever;
* the accounting rule protects the LEDGER — one gate that produced no verdict
  may never be charged to the implementer, however long it stays silent.

While a run stands at `test_gate_absent` inside its episode, the deferral
ledger holds it for one wait per silent report (keyed by report path and
mtime): the tick does not advance or claim it, and the poller keeps serving
every other project. When the wait runs out, the tick logs
`gate_deferral_reacquire` and advances along the gate's only edge,
`test_gate_absent -> test`, which re-runs the `test` step alone.

The run ends when the episode's wall-clock ceiling
(`GATE_DEFERRAL_EPISODE_MAX_SECONDS`) passes, or once that edge has spent its
`max_loop: 100`, which the engine counts over the whole run
(`skillflow_edge_counts`, read by `core/gate_deferral.py:absence_laps_spent`).
With a 60 s wait, 100 laps fit inside the 3 h ceiling; a run standing at
`test` has already been granted its last lap and runs it, so a run uses all
100 re-acquisitions (101 gate calls) before it ends. The sentence it ends
with is

    gate did not run: no verdict was measured (run_tests.sh, N gate run(s))

where `N` sums `repo_gate.attempts` over the distinct silent reports, however
many ticks noted each. It carries no word that could be read as a code
failure; before round 5 the same runs ended on `Cycle limit exceeded`.

The execution points that read the ledger:

* `core/scheduler.py` — the tick consults `observe_run` before the NB-1
  runaway valves and returns without spending anything while the episode is
  live; an expired episode calls `fail_run` with the sentence above.
* `AItelierSkillFlow.advance_run` — the host refuses to advance a run whose
  gate is silent inside its wait, because advancing is what re-runs the gate.
* `core/scheduler.py` `_MAX_CLAIMS_PER_INSTANCE` — a deferral releases no
  claim and does not move the row, and the tick returns at
  `state == "silent"` before the valve check, so each instance during a live
  episode is claimed exactly once (measured). The valve stays the bound for
  a non-deferral runaway.

Both knobs are bounded: `GATE_DEFERRAL_WAIT_SECONDS` and
`GATE_DEFERRAL_EPISODE_MAX_SECONDS` may be tuned through the environment and
may not be removed — a non-positive value falls back, and each is clamped to
its own ceiling.

## The known-red baseline (round 6)

`_apply_baseline` is the one place that reads `failures[]` and writes a
durable record. A report with `repo_gate_absent` or `repo_gate_unmeasured`
may not SEED a baseline (its only gate entry says the gate was NOT measured,
and seeding it would let that gate's next real red be forgiven), and may not
report `passed_relative: true`; with no baseline behind it, it gets its own
`baseline_state`, `unmeasured`, which is in neither `BASELINE_MEASURED` nor
the write path. A report with `failure_identity_error` (which includes every
report whose reds are unattributable) gets `baseline_state: unreadable` and
`passed_relative: false`, and writes nothing. `core/gate_deferral.py:read_absence`
reads `repo_gate_absent` when the key is there, and falls back to
`repo_gate_unmeasured` only for a report written before the key existed.

## The four witnesses

`evidence/gate-cycle-accounting-20260921/` holds four `run_tests` reports
taken verbatim out of `skillflow_steps.outputs_json` (run
`aad5aa9b-6839-4156-8a64-c8c59728e85f`, node
`art.seven-actions-are-static-poses-with-no-motion-in-them` rev 2). They are
the **record of an incident**, byte-frozen, and not an input to the
classifier.

| file | step | what it recorded |
|---|---|---|
| `step-6730.outputs.json` | 6730 | `rc=1`, GDScript parse FAILED on two files the round had just written |
| `step-6769.outputs.json` | 6769 | `rc=2`, python `2630 passed`, compile `OK (364/364)`, play-test never started: `godot-builder unreachable … HTTP Error 409: Conflict — gate NOT run` |
| `step-6773.outputs.json` | 6773 | `rc=2`, same shape, `2630 passed` |
| `step-6777.outputs.json` | 6777 | `rc=2`, same shape, `2633 passed` |

The attempt died with `Cycle limit exceeded`. Three of its four cycles took
no measurement at all. Their `new_failures[0]` is 1538 characters, the old
tool's bound: each one is a truncated tail of the gate output, which is why
the declaration is now read before the tail is cut. The same shape recurred
on `751e9e26`, `attempt-d3d14632` and `f6e21064`.

## The other card (not this one)

The game repository's `tools/godot_gate.py` still has to change, and this card
does not touch it:

* on its genuinely unmeasured paths (the builder unreachable, the render lock
  held) it has to emit the declaration line above, with `state: "blocked"`;
* it uses exit `2` for `incomplete`, which includes contract files the author
  wrote wrong (its own text: "no authored scenarios found under ... — an
  empty one is not a pass"). It has to stop using `2` for those. The
  `manifest` line (`{0: passed, 1: failed}.get(code, "incomplete")`) is where
  that split has to become visible.

## Records

Mutation runs and drives of earlier revisions, kept as they were measured.
Each names a test that fails when the mutation is applied.

`run_tests` protocol (round 4):

| mutation | killed by |
|---|---|
| M2 delete the `output_truncated` branch | `test_run_tests_unmeasured_declaration.py::test_a_bounded_fragment_is_not_a_record_on_its_own`; `test_run_tests_known_red_baseline.py::test_unreliable_repo_gate_identity_never_seeds_or_changes_baseline` |
| M4 delete `result["measured"]` | `test_run_tests_unmeasured_declaration.py::test_every_repo_gate_run_carries_its_measurement` |
| M8 unmeasured branch: `repo_gate_cases=None` → `set()` | `test_run_tests_unmeasured_declaration.py::test_an_unmeasured_repo_gate_may_not_prune_its_known_red` |
| M18 measured-pass branch: `set()` → `None` | `test_run_tests_unmeasured_declaration.py::test_a_repo_gate_that_measured_green_may_prune_its_known_red` |
| M19 invert the truncation branch | `test_run_tests_unmeasured_declaration.py::test_a_truncated_gate_with_no_record_at_all_is_a_red` |
| M17 add `"failed"`/`"red"` to the state set | `test_run_tests_unmeasured_declaration.py::test_a_declaration_that_says_failed_is_not_an_absence[failed]`; `::test_the_unmeasured_state_set_holds_no_red_word` |
| ABS let a declared absence seed a baseline or pass relatively | `test_run_tests_unmeasured_declaration.py::test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative`; `test_tree_level_accounting_witnesses.py::test_ABS_the_absence_branch_is_ahead_of_the_seed_in_the_real_source`; `::test_ABS_the_unmeasured_state_is_not_a_measurement` |

Absence accounting (round 5), in `tests/unit/test_gate_deferral_is_accounted.py`:

| mutation | killed by |
|---|---|
| treat any red report as an absence | `test_a_report_that_graded_the_code_states_no_absence` |
| expire the absence on the first tick | `test_a_declared_absence_is_silent_inside_its_wait`; `test_a_zero_ceiling_cannot_expire_a_run_instantly` |
| restart the episode from the last tick | `test_the_episode_does_not_restart_on_every_tick` |
| end with `Cycle limit exceeded` (or any red word) | `test_an_expired_absence_names_the_absence_and_never_the_code` |
| widen the wait to 24h / 1e9 | `test_the_wait_cannot_be_widened_until_the_ceiling_disappears` |
| remove the episode ceiling | `test_the_episode_ceiling_cannot_be_removed` |
| force `hold_remaining` to 0 | `test_hold_remaining_is_not_constant_zero` |

The M21 family (rounds 8-10): the card's literal M21 (`startswith` -> `in`,
the `line[len(prefix):]` slice unchanged) is observable with a header exactly
`len(prefix)` long and the prefix quoted inside the JSON.
`test_run_tests_unmeasured_declaration.py::test_the_r4_shaped_witness_flips_under_the_literal_m21`
asserts both readings of that shape, and `…_m21b` does the same on the case
channel. The mutation catalog (`tools/mutation_catalog/mutations.py`) carries
`M21` and `M21b` verbatim; the `in` + `line.index(...)` pair is a second,
blunter mutation (`M21PAIR` / `M21BPAIR`). `measured` is read by the fold in
`run_tests` and by `_acquire_repo_gate`.

Admission (r2), against the 12 files of the gate and deferral suites:

| mutation | killed by |
|---|---|
| delete the admission clause in `_repo_gate_outcome` | `test_repo_gate_admission.py::test_pole_1_a_gate_the_engine_did_not_admit_is_not_run[live_render]`, `[owner_lost]`, `::test_an_unreachable_engine_is_not_run`, `::test_an_answered_gate_that_exits_nonzero_stays_a_measured_failure[2]`, `[3]`, `[124]`; `test_coding_impl_busy_gate.py::test_a_busy_gate_never_sends_the_run_back_to_implement` |
| let an exit 1 become not-run (`not in (0,)`) | `test_repo_gate_admission.py::test_exit_1_is_a_red_whatever_the_relay_saw` |
| skip the report directory (`if False:`) | `test_repo_gate_admission.py::test_pole_2_a_gate_that_ran_and_went_red_is_a_code_red`; `test_repo_gate_identity_from_report_dir.py::test_a_long_red_output_still_names_every_failure` |
| stop adding `render_wait_timeout_sec` | `test_repo_gate_admission.py::test_two_concurrent_repo_gates_are_each_admitted_by_the_queue`, `::test_a_gate_that_sends_its_own_wait_keeps_it`, `::test_pole_1_a_gate_the_engine_did_not_admit_is_not_run[live_render]` |
| `test_gate_absent` back to `to: null` | `test_coding_impl_busy_gate.py::test_a_busy_gate_never_sends_the_run_back_to_implement`; 8 tests in `test_coding_impl_gate_absence.py` |
| hold for the whole episode (drop `hold_remaining > 0`) | `test_coding_impl_busy_gate.py::test_a_busy_gate_never_sends_the_run_back_to_implement`; `test_coding_impl_gate_absence.py::test_the_host_hold_alone_keeps_the_rows_where_the_absence_left_them` |
| drop `evidence_state = "not_run"` | `test_repo_gate_admission.py::test_pole_1_a_gate_the_engine_did_not_admit_is_not_run[live_render]`, `[owner_lost]` |
| read every manifest under the ticket | `test_repo_gate_identity_from_report_dir.py::test_reports_the_repositorys_own_tests_retained_are_not_the_gates` |

r3, one throwaway copy per mutation, with an ignition probe, against the 18
files of the gate, deferral, admission and identity suites (logs:
`logs/gate_not_run_r3/mut_<id>.txt`, `mut_<id>.ignition`). The red counts are
r3's. Rev 4 deleted
`test_repo_gate_red_beats_absence.py::test_a_pytest_killed_at_its_wall_beside_a_refused_gate_is_not_an_absence`,
which r3 counted among the killers of N2 and N3, and put
`test_repo_gate_red_beats_absence.py::test_a_pytest_killed_at_its_wall_beside_a_refused_gate_is_an_absence`
(the opposite verdict, director ruling rev 4) in its place. The deleted test is
left out of the N2 and N3 rows below; their counts are the r3 counts that
included it.

| mutation | ignition count | red / 253 | killed by |
|---|---|---|---|
| CONTROL (no change) | 0 | 0 | none |
| N1 drop the retained-finding clause in `_repo_gate_outcome` | 266 | 8 | `test_repo_gate_red_beats_absence.py::test_pole_3_a_retained_red_then_a_refusal_is_a_measured_red[python_red]`, `[compile_red]`; `::test_a_retained_finding_outranks_every_source_of_unmeasured[source0]` to `[source5]` |
| N2 `repo_gate_absent = True` whatever else failed | 198 | 3 | `test_repo_gate_red_beats_absence.py::test_pole_4_a_pytest_red_beside_a_refused_gate_is_not_an_absence`; `test_coding_impl_absence_needs_nothing_else_red.py::test_pole_4_a_pytest_red_behind_a_busy_gate_goes_back_to_implement` |
| N3 `read_absence` reads `repo_gate_unmeasured` (r2) | 984 | 10 | `test_repo_gate_red_beats_absence.py::test_pole_4_a_pytest_red_beside_a_refused_gate_is_not_an_absence`, `::test_read_absence_follows_repo_gate_absent_when_it_is_there[flags1-False]`; `test_coding_impl_absence_needs_nothing_else_red.py::test_pole_4_a_pytest_red_behind_a_busy_gate_goes_back_to_implement`; 4 in `test_gate_deferral_is_accounted.py`; 2 in `test_gate_deferral_execution_points.py` |
| N4 `_assert_identities` returns `{}` | 17 | 2 | `test_repo_gate_identity_is_stable.py::test_different_observed_values_keep_the_same_identities`, `::test_an_assertion_is_named_by_scenario_name_and_frame` |
| N5 no `~N` suffix for a repeated id | 95 | 2 | `test_repo_gate_identity_is_stable.py::test_an_assertion_is_named_by_scenario_name_and_frame`, `::test_findings_with_identical_text_are_both_kept` |
| N6 the keepalive writes nothing | 7 | 1 | `test_gate_admission_keepalive.py::test_a_render_after_a_queue_longer_than_the_client_timeout_is_answered` |
| N7 the keepalive goes on after admission | 16 | 1 | `test_gate_admission_keepalive.py::test_the_client_timeout_still_bounds_the_render_itself` |
| N8 keepalive with no readable `/lifecycle` | 33 | 1 | `test_gate_admission_keepalive.py::test_no_keepalive_when_admission_cannot_be_seen` |
| N9 `absence_laps_spent` always `False` | 418 | 3 | `test_gate_deferral_reads_the_run.py::test_spent_laps_end_the_run_naming_the_absence`, `::test_laps_are_read_from_the_engine_counter`; `test_coding_impl_absence_needs_nothing_else_red.py::test_the_absence_gate_laps_run_out_naming_the_absence` |
| N10 lap check on any node | 584 | 2 | `test_gate_deferral_reads_the_run.py::test_the_last_lap_granted_is_run`; `test_coding_impl_absence_needs_nothing_else_red.py::test_the_absence_gate_laps_run_out_naming_the_absence` |
| N11 (review R5) the host hold skips the latest report | 460 | 2 | `test_gate_deferral_reads_the_run.py::test_the_host_starts_a_new_wait_from_a_new_silent_report`, `::test_spent_laps_end_the_run_naming_the_absence` |
| N12 no tail read above 64 MiB | 2 | 1 | `test_repo_gate_identity_is_stable.py::test_a_report_too_large_to_parse_whole_is_read_from_its_tail` |

Rev 4 and rev 5: `logs/gate_not_run_r4/`, `logs/gate_not_run_r5/`, and the
delivery notes in `final/`.
