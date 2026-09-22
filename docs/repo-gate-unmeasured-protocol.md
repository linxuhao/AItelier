# A gate that never ran is not a failing gate — the declaration protocol

2026-09-21. This is the protocol card. It fixes ONE thing: how `run_tests`
decides that a repository gate did not measure anything, and what it does with
that reading. The game repository's own gate is the other half of the pair and
is **another card** (see "The other card" below).

## The rule

> `unmeasured` is **declared**, never inferred.

The only things that produce `unmeasured` are the two the framework can see for
itself:

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

Everything else is measured. `rc=0` is `measured_pass`; **every other exit
code is `measured_fail`**, including `1`, `2`, `124`, `137`, `255`, `-1` and
`-9`. No exit code is ever read as an absence, and that is the whole point:
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

It is **re-acquired**, not spent. `run_tests` runs the gate again — up to
`REPO_GATE_UNMEASURED_ATTEMPTS` runs, `REPO_GATE_RETRY_DELAY_SECONDS` apart —
and folds in only the verdict it finally gets. `repo_gate.attempts` records
how many runs the reading cost. A gate that stayed silent through every
attempt ends the story honestly: `repo_gate_unmeasured: true`, `passed:
false`, no case list (so nothing of that gate's known-red is pruned) and no
failure invented out of it.

Why it has to happen inside the step: `configs/coding_impl.yaml` is frozen
(`max_loop: 3` included), and its `test` step routes a report with no usable
evidence to `test_evidence_missing` — a loop-external **terminal** gate. Left
to the graph, a declared absence would either end the run or spend an
implement cycle. Re-acquiring inside the tool buys neither: the first
contention costs no implement cycle and does not end the run.

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
  Until it does, a broken authored contract is a `measured_fail` here — by
  design, because "an empty one is not a pass" is a defect the round is
  supposed to fix. The `manifest` line (`{0: passed, 1: failed}.get(code,
  "incomplete")`) is where that split has to become visible.

This card only makes the protocol fixed, documented (here and in
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
* `core/scheduler.py:31` `_MAX_CLAIMS_PER_INSTANCE` — its own comment states
  the premise: one instance is re-claimed only when something reset a
  completed row back to `pending`, which a deferral does ON PURPOSE. Left
  unguarded, that valve kills the run with a message blaming the step, so the
  valve is bypassed while an episode is live and the EPISODE's ceiling is the
  bound that applies instead.

Both knobs are bounded: `GATE_DEFERRAL_WAIT_SECONDS` and
`GATE_DEFERRAL_EPISODE_MAX_SECONDS` may be tuned through the environment and
may not be removed — a non-positive value falls back, and each is clamped to
its own ceiling, so "raise the number" is not a way to delete the bound.

Routing: an honest absence report still writes `test_report.json` and still
passes the schema, so the graph had to be told about it. The `test` step now
carries an edge keyed on `repo_gate_absent` — a flag the tool sets on the
report itself, read with `from_file` so it survives any validator — leading to
`test_gate_absent`, a loop-EXTERNAL gate that is deliberately absent from
`end_conditions`. Reaching it parks a still-running run instead of completing
or failing it, so the absence costs neither an implement lap nor the run.

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
| delete the per-instance valve guard | `test_the_per_instance_valve_does_not_fire_while_an_episode_is_live`; `test_the_episode_ceiling_replaces_the_valve_that_it_disarmed` |
| delete the host `advance_run` hold | `test_the_host_refuses_to_advance_a_run_whose_gate_is_silent` |

A previous revision of this file carried a row claiming M21 named in it.
That row has been **removed**, not reworded, and what replaced it is the
measurement below rather than another sentence: Round 5 MEASURED why the row
could never have reproduced.


`startswith(PREFIX)` → `PREFIX in line` **alone is not observable at all**.
The slice is still `line[len(PREFIX):]`, so on an echoed line the reader starts
at index 30 — inside the echo — and `json.loads` fails exactly as it does for
the candidate. No test can kill a mutation with no observable effect, and a
table row claiming one does is a false claim by construction, not a
fixture-length problem.

The observable mutation is the careless REFACTOR that travels with it: keep the
substring test AND seek the prefix where it was found
(`line.index(PREFIX) + len(PREFIX)`). That pair turns one echoed log line into a
declaration — and, on the case channel, into a prunable known-red identity.
`tests/unit/test_run_tests_unmeasured_declaration.py` now applies that mutation
IN-SUITE (`_apply_m21`) and asserts both halves: the candidate reads `None`,
the mutant reads `blocked` / fabricates case `A`. The two witnesses the removed
row named are kept and now carry the exactly-prefix-length noise header their
comment documents. A row returns to this table only with that measurement
behind it.


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

