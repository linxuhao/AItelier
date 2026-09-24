# A gate that never ran is not a failing gate — the declaration protocol

2026-09-21. This is the protocol card. It fixes ONE thing: how `run_tests`
decides that a repository gate did not measure anything, and what it does with
that reading. The game repository's own gate is the other half of the pair and
is **another card** (see "The other card" below).

## The rule

> `unmeasured` is **declared**, never inferred.

The only things that produce `unmeasured` are the three the framework can see
for itself (the third since r2, 2026-09-24 — see "A gate the engine did not
admit" below):

1. **The gate's own declaration.** Exactly one whole line, emitted by the gate:

   ```
   AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"..."}
   ```

   `state` must be one of `unmeasured`, `not_run`, `blocked`. `failed` and
   `red` are **not** in that set and must never be added: a gate that failed
   measured something, and one line must not be able to turn a red into an
   absence.
2. **The harness's own observation** that it killed the gate or could not start
   it (`timed_out` / `runner_error`). This is the same shape `run_tests`
   already used for pytest — `skipped_because="pytest_timeout"`, "a timeout is
   ABSENT evidence, not a red suite". The repo gate now aligns with it.
3. **The admission relay's record of the engine's own answer.** The gate
   reaches the engine only through `aitelier/gate_admission.py`, which records
   what the engine answered to every request. A gate that exits neither `0`
   nor `1` while its LAST engine request was refused admission (409),
   unreachable, abandoned or undelivered did not run. `1` is a red whatever
   the relay saw, and any other exit after an answered request stays
   `measured_fail`.

Everything else is measured. `rc=0` is `measured_pass`; **every other exit
code is `measured_fail`**, including `1`, `2`, `124`, `137`, `255`, `-1` and
`-9`, unless source 3 applies. No exit code on its own is ever read as an
absence, and that is the whole point:
the game repo's `tools/godot_gate.py` reserves `2` for `incomplete`, which
includes contract files **the implementer wrote wrong** — its own text says
"no authored scenarios found under ... — an empty one is not a pass". Reading
`2` as "the engine never ran" turned a legitimate red into
`infrastructure_unavailable` with `failures: []`. That is why there is no
`REPO_GATE_UNMEASURED_EXIT_CODE` and why a test asserts the name does not
exist.

## What was broken about the declaration channel

The channel existed on paper and was dead in practice. `_run_node_cmd` retained
`output = out[-2000:]` and set `output_truncated`, and the reader refused to
read a record out of a truncated output — so a declaration was only readable
while the gate's log stayed under 2000 characters. A real gate's log (a
GDScript compile plus 172 authored scenarios) is two orders of magnitude
larger, so the "two opt-ins" were really one, and that one was the exit code
this card removes.

Now the declaration is scanned out of the **whole** output **before** the tail
is retained (`unmeasured_declaration` on the gate dict), and the
`output_truncated` refusal remains only for a caller that holds nothing but the
fragment: a fragment proves nothing about a line that may have been cut in
half. `tests/unit/test_run_tests_unmeasured_declaration.py::test_a_declaration_survives_output_far_past_the_retention_bound`
drives a gate that emits its record first and then 3000 characters of log, and
asserts both halves — that the retained fragment contains no record, and that
the run was still read as `unmeasured`.

## What happens to a declared absence
It is NOT re-acquired inside the step. `REPO_GATE_UNMEASURED_ATTEMPTS` is 1:
one step invocation makes ONE gate call, and its worst-case hold is the gate's
own timeout (`REPO_GATE_TIMEOUT`, 5400 s) rather than the 3 x 5400 + 2 x 60 s
an in-step re-acquisition cost (round 5, measured). Waiting for the gate is the
SCHEDULER's job (`core/gate_deferral.py`): while the absence is live the run is
not advanced and spends no implement cycle, and the poller keeps serving every
other project. Since r2 the hold lasts one wait per silent report; then the run
advances along the absence gate's only edge, back to `test`, which re-runs the
gate alone. `repo_gate.attempts` still records how many runs the reading
cost. A gate that stayed silent ends the story honestly:
`repo_gate_unmeasured: true`, `passed: false`, no case list (so nothing of that
gate's known-red is pruned) and no failure invented out of it.

Why the absence is routed out of the loop: `configs/coding_impl.yaml` keeps
`max_loop: 3` on `test_outcome -> implement`, but its `test` step matches the
`repo_gate_absent` FLAG and routes to the loop-external `test_gate_absent`
gate. Left to the `all_passed: false` edge, a declared absence would spend one
of the three implement laps on a gate that never spoke, and the run would die
on `Cycle limit exceeded`. The flag travels on the tool's own RETURN, so the
edge needs no file reader at all.

The first contention costs no implement cycle and does not end the run.

## The four witnesses

`evidence/gate-cycle-accounting-20260921/` holds four `run_tests` reports taken
verbatim out of `skillflow_steps.outputs_json` (run
`aad5aa9b-6839-4156-8a64-c8c59728e85f`, node
`art.seven-actions-are-static-poses-with-no-motion-in-them` rev 2). They are
the **record of an incident**. They are byte-frozen, and they are deliberately
NOT an input to the classifier: their `rc` and their prose ("409 Conflict —
gate NOT run") support only one kind of reading — inference from the scene —
and inference from the scene is the technique this card forbids. Nothing here
reclassifies them, and none of them carries a declaration, because none of
them could.

| file | step | what it recorded |
|---|---|---|
| `step-6730.outputs.json` | 6730 | `rc=1`, GDScript parse FAILED on two files the round had just written. A real red; one implement cycle well spent. |
| `step-6769.outputs.json` | 6769 | `rc=2`, python `2630 passed`, compile `OK (364/364)`, play-test never started: `godot-builder unreachable … HTTP Error 409: Conflict — gate NOT run`. |
| `step-6773.outputs.json` | 6773 | `rc=2`, same shape, `2630 passed`. |
| `step-6777.outputs.json` | 6777 | `rc=2`, same shape, `2633 passed`. |

The attempt died with `Cycle limit exceeded`. Three of its four cycles took no
measurement at all.

### Had the gate been able to declare

Each of the three `rc=2` cycles was a boxed-out resource (the builder answered
409 because something else held it). Each would have emitted
`AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"godot-builder
unreachable: 409 Conflict"}` and exited with its own `incomplete` code:

* the tool would **re-acquire** the verdict instead of folding an absence in.
  Once the builder was free, the play-test verdict would have been folded in —
  no implementation lap consumed, no change to the code, `implement_runs`
  unchanged;
* had the resource stayed held through every attempt, the report would say
  `repo_gate_unmeasured: true` and name the reason, instead of presenting a
  red that no implementer could act on;
* the round would not have been killed by the **first** contention, and the
  loop counter (`max_loop: 3`) would have been spent on things a round can
  actually fix.

### Why it could not declare

Two reasons, in this order:

1. **The protocol did not exist.** On 2026-09-21 there was no opt-in line for
   "I did not run"; the only channel was the exit code, and the exit code is
   the thing that cannot carry this meaning (`incomplete` includes the
   author's own broken contract files).
2. **Even with the protocol, the text would have been eaten.** The tool
   retained `out[-2000:]` and the reader refused truncated output. The four
   witnesses show the shape of the loss: their `new_failures[0]` is 1538
   characters — the tool's own bound — so each one is a truncated tail of the
   gate output. A declaration written when the play-test was boxed out would
   have been nowhere near a 2000-byte tail. This card fixes that first, which
   is why it is the first thing it fixes.

## The other card (not this one)

The game repository's `tools/godot_gate.py` still has to change, and this card
does not touch it:

* on its genuinely unmeasured paths (the builder unreachable, the render lock
  held) it must emit the declaration line above, with `state: "blocked"`;
* it must **stop** using exit `2` for contract files the author wrote wrong.
  Until it does, a broken authored contract is a `measured_fail` here,
  because "an empty one is not a pass" is a defect the round is
  supposed to fix. The `manifest` line (`{0: passed, 1: failed}.get(code,
  "incomplete")`) is where that split has to become visible.

This card only makes the protocol fixed, written down (here and in
`aitelier/tools/run_tests/tool.yaml`) and usable by that card.

## The absence that outlives the wall clock (round 5)

The first four rounds stopped at "a gate that never ran is not a failing
gate". That was half the property. The other half is the ACCOUNT: an absence
that lasts long enough to outlive the run's wall-clock bound may not be
recorded as a failure of the code under test — which is what `Cycle limit
exceeded` does, and what round 4's candidate shipped.

The two rules are not in conflict, because they protect different resources:

* the wall-clock bound protects a SHARED resource — the scheduler and its
  poller may never be held by one step forever;
* the accounting rule protects the LEDGER — one gate that produced no verdict
  may never be charged to the implementer, however long it stays silent.

`core/gate_deferral.py` holds both. While an absence is live and inside its
ceiling the run is left alone: no advance, no claim, no implement cycle.
Past the ceiling the run may END, and the sentence it ends with is

    gate did not run: no verdict was measured

It names the absence and carries no word that could be read as a code
failure. There is no third option in which the run ends saying the tests
failed, because nothing measured a test.

How the two execution points read it:

* `core/scheduler.py` — the tick consults `observe_run` before the NB-1
  runaway valves and returns without spending anything while the episode is
  live; an expired episode calls `fail_run` with the absence sentence.
* `AItelierSkillFlow.advance_run` — the tick is not the only driver. The
  host refuses to advance a run whose gate is silent, because advancing is
  what routes the absence back into the implement loop.
* `core/scheduler.py` `_MAX_CLAIMS_PER_INSTANCE` — its own comment states
  the premise: one instance is re-claimed only when something reset a
  completed row back to `pending`. A deferral does NOT do that: it releases
  no claim, it does not move the row, and the tick returns at
  `state == "silent"` BEFORE the valve check, so each instance during a live
  episode is claimed exactly ONCE (measured). The valve is therefore not
  bypassed — it is never reached on the deferral path, and it stays the
  ordinary bound for a non-deferral runaway.

Both knobs are bounded: `GATE_DEFERRAL_WAIT_SECONDS` and
`GATE_DEFERRAL_EPISODE_MAX_SECONDS` may be tuned through the environment and
may not be removed — a non-positive value falls back, and each is clamped to
its own ceiling, so "raise the number" is not a way to delete the bound.

Routing: an honest absence report still writes `test_report.json` and still
passes the schema, so the graph had to be told about it. The `test` step now
carries an edge keyed on `repo_gate_absent` — a flag the tool sets on the
report and ALSO returns in the step's own flags, so the edge matches it as a
`{field: repo_gate_absent, value: true}` FLAG with no file reader at all —
leading to `test_gate_absent`, a loop-EXTERNAL gate that is deliberately absent
from `end_conditions`. Reaching it parks a still-running run instead of
completing or failing it, so the absence costs neither an implement lap nor
the run. Its one transition (r2) is `test_gate_absent -> test`: when the wait
runs out, the verdict is re-acquired and `implement` stays unreachable.

The edge is a FLAG and not a `from_file` read, and that is a measurement, not
a preference. `from_file` is resolved against the EVALUATING step's own output
dir: on `test_evidence` — a json_schema validator that writes no file — every
evaluation raised `FileNotFoundError: Output file not found:
test_report.json`, the engine recorded `transition file_reader failed`, and
the edge counted as UNMATCHED. That one placement is the root cause of three
round-5 verdicts: the absence fell through to `all_passed: false`, spent all
three implement laps, and the run died on `Cycle limit exceeded` for a gate
that never spoke. Moving the same `from_file` match onto `test` does not fix
it either: the engine's `_flags_match` calls `data.get(field)` on whatever the
file parses to, so a report parsing to a LIST raises
`AttributeError: 'list' object has no attribute 'get'` and takes the resolver
down instead of routing. The flag therefore travels on the tool's RETURN,
which the engine merges into the step's flags.

## The tests that hold this down

Every mutation claim below is backed by a test that fails when the mutation
is applied; the kill count is per-test, from the suite, and a mutation whose
test does not fail is not listed here.

The `run_tests` protocol (round 4, carried as a regression guard):

| mutation | killed by |
|---|---|
| M2 delete the `output_truncated` branch | `test_run_tests_unmeasured_declaration.py::test_a_bounded_fragment_is_not_a_record_on_its_own`; `test_run_tests_known_red_baseline.py::test_unreliable_repo_gate_identity_never_seeds_or_changes_baseline` |
| M4 delete `result["measured"]` | `test_run_tests_unmeasured_declaration.py::test_every_repo_gate_run_carries_its_measurement` |
| M8 unmeasured branch: `repo_gate_cases=None` → `set()` | `test_run_tests_unmeasured_declaration.py::test_an_unmeasured_repo_gate_may_not_prune_its_known_red` |
| M18 measured-pass branch: `set()` → `None` | `test_run_tests_unmeasured_declaration.py::test_a_repo_gate_that_measured_green_may_prune_its_known_red` |
| M19 invert the truncation branch | `test_run_tests_unmeasured_declaration.py::test_a_truncated_gate_with_no_record_at_all_is_a_red` |
| M17 add `"failed"`/`"red"` to the state set | `test_run_tests_unmeasured_declaration.py::test_a_declaration_that_says_failed_is_not_an_absence[failed]`; `::test_the_unmeasured_state_set_holds_no_red_word` |
| ABS let a declared absence seed a baseline or pass relatively | `test_run_tests_unmeasured_declaration.py::test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative`; `test_tree_level_accounting_witnesses.py::test_ABS_the_absence_branch_is_ahead_of_the_seed_in_the_real_source`; `::test_ABS_the_unmeasured_state_is_not_a_measurement` |

## A declared absence is not a baseline, in either direction (round 6)

`_apply_baseline` is where an absence could be laundered into a measurement,
because it is the one place that reads `failures[]` and writes a durable
record. Two rules, both measured end to end through the REAL tool:

* an absence may not **SEED** a baseline. The seed is `known = set(keys)`
  taken from this run's `failures[]`, and an absence's only entry says
  `repo_gate:run_tests.sh was NOT measured`. Writing that in records a gate
  that never spoke as the repo's standing known-red — and the NEXT real red
  from that gate is then forgiven by it;
* an absence may not report `passed_relative: true`, even when a baseline
  already exists. That field is the claim "this red was already here"; with no
  verdict there is no such claim to make.

So an absence that finds no baseline is given its own fourth state,
`unmeasured`, which is in neither `BASELINE_MEASURED` nor the write path.
Absence is declared at most once per report and is read from the report's own
`repo_gate_absent` / `repo_gate_unmeasured` flags.

The absence accounting (round 5), in
`tests/unit/test_gate_deferral_is_accounted.py`:

| mutation | killed by |
|---|---|
| treat any red report as an absence | `test_a_report_that_graded_the_code_states_no_absence` |
| expire the absence on the first tick | `test_a_declared_absence_is_silent_inside_its_wait`; `test_a_zero_ceiling_cannot_expire_a_run_instantly` |
| restart the episode from the last tick | `test_the_episode_does_not_restart_on_every_tick` |
| end with `Cycle limit exceeded` (or any red word) | `test_an_expired_absence_names_the_absence_and_never_the_code` |
| widen the wait to 24h / 1e9 | `test_the_wait_cannot_be_widened_until_the_ceiling_disappears` |
| remove the episode ceiling | `test_the_episode_ceiling_cannot_be_removed` |
| force `hold_remaining` to 0 | `test_hold_remaining_is_not_constant_zero` |

## The M21 family (rounds 8-10)

The card's literal M21 (`startswith` -> `in`, the `line[len(prefix):]` slice
UNCHANGED) IS observable, and the r7 claim that it was inert was wrong. It is
inert only for the echo shape used in r7 (`noise + PREFIX + body`), where the
fixed slice cuts into the prefix itself. With a header exactly `len(prefix)`
long and the prefix quoted INSIDE the JSON, `line[len(prefix):]` lands on the
JSON and a real red is rewritten into an absence.
`test_the_r4_shaped_witness_flips_under_the_literal_m21` asserts both readings
of that shape; `test_the_r4_shaped_witness_flips_under_the_literal_m21b` does
the same on the case channel. Both re-run the REAL reader with the card's
literal single-line edit applied.

The mutation catalog (`tools/mutation_catalog/mutations.py`) carries `M21` and
`M21b` verbatim, and `run_mutations.py` applies exactly those edits to a
detached `git worktree` of the committed tree, one worktree per mutation. The
`in` + `line.index(...)` PAIR is a SECOND, blunter mutation (`M21PAIR` /
`M21BPAIR`, applied in-process as `_apply_m21_pair`), not the card's M21 — the
name says which one it is.



`measured` is not a dead key: the fold in `run_tests` and `_acquire_repo_gate`
both read it, and it is what decides whether a gate's cases may be pruned or
its red forgiven.

## What this card did not touch

* `evidence/gate-cycle-accounting-20260921/` — not one byte.
* The game repository's `tools/godot_gate.py` — another card.
* `max_loop: 3` on `test_outcome → implement` — unchanged in value. Round 5
  routes the ABSENCE to a loop-external gate instead of reclassifying it, so
  the bound the four witnesses exhausted still governs every real red. The
  `test`/`test_evidence` edges did gain one transition each; that is the
  minimum the honest report shape needs, it touches nothing the four witnesses
  read, and it is stated at the edge itself.

## A gate the engine did not admit (r2, 2026-09-24)

Node `harness.a-gate-that-could-not-run-is-not-a-failing-test`. Three runs
(`751e9e26`, `attempt-d3d14632`, `f6e21064`) died of `Cycle limit exceeded`
because a render lock held by someone else made the game gate exit `2`
(`godot-builder unreachable ... HTTP Error 409: Conflict -- gate NOT run`), and
`2` was a red. The game gate does not declare, so sources 1 and 2 above never
fired for it.

**The relay.** `_run_repo_gate` starts an `AdmissionRelay`
(`aitelier/gate_admission.py`) for each gate run and hands the gate the
relay's URL as `GODOT_BUILDER_URL`. The relay forwards every request to the
real builder. On the render routes (`/playtest`, `/script`,
`/x11_input_smoke`) it adds `render_wait_timeout_sec`
(`AITELIER_REPO_GATE_RENDER_WAIT_SECONDS`, default 1500) when the gate sent
none, so the request waits in the harness render queue behind a live owner
instead of getting a 409. For each request it records the route, the status
and the owner fields of a 409 payload, as `answered`, `not_admitted`,
`engine_error`, `unreachable` or `abandoned`. The summary (from the LAST
request) is written to `<ticket>/admission.json` and to
`repo_gate.admission`.

**Three outcomes, three sets of fields** (`test_report.json`):

| outcome | fields |
|---|---|
| passed | `passed: true`, `repo_gate.measured: measured_pass` |
| ran and red | `passed: false`, `repo_gate.measured: measured_fail`, `release_evidence: known_failure`, per-case `new_failures` |
| not run | `passed: false`, `repo_gate.measured: unmeasured`, `repo_gate_absent: true`, `repo_gate_unmeasured: true`, `repo_gate_admission` / `repo_gate.admission.state` (`not_admitted`, `unreachable`, `abandoned`, `undelivered`), `evidence_state: not_run` when nothing else failed, `release_evidence: unresolved` |

**Identities.** Each gate run has a ticket (`rt-<UTC>-<8 hex>`) and its own
`GATE_REPORT_DIR` (`$AITELIER_HOME/gate-reports/<ticket>`). A red gate's
identities are read from the findings it retained there
(`_report_dir_failure_cases`), one per finding, keyed
`<stage>/<sha256[:12]>`, never from the bounded tail. Only the report whose
manifest names the repository the gate ran for is read: the real gate's
python stage runs the gate's own tests, and they retain reports for their
fixture repository `/repo` under the same ticket.

**The loop.** `test_gate_absent -> test` (`max_loop: 100`). The deferral
ledger holds the run for one wait per silent report (keyed by report path and
mtime), then the tick logs `gate_deferral_reacquire` and advances, which
re-runs the `test` step alone. The wall-clock ceiling still ends an absence
that never clears, with the absence sentence.

The tests that hold this down (candidate mutations, one throwaway copy each,
run against the 12 files of the gate and deferral suites):

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
