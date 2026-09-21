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

## The tests that hold this down

Each row is a mutation of this candidate, and the test that turns red when it
is applied. The mutations are the seven that survived the previous attempt's
19 new tests.

| mutation | killed by |
|---|---|
| M2 delete the `output_truncated` branch | `test_run_tests_unmeasured_declaration.py::test_a_bounded_fragment_is_not_a_record_on_its_own`; `test_run_tests_known_red_baseline.py::test_unreliable_repo_gate_identity_never_seeds_or_changes_baseline` |
| M19 invert it (`return False` on truncation) | `test_run_tests_unmeasured_declaration.py::test_a_truncated_gate_with_no_record_at_all_is_a_red` |
| M4 delete `result["measured"]` | `test_run_tests_unmeasured_declaration.py::test_every_repo_gate_run_carries_its_measurement` (the fold reads the field; without the writer the fold raises) |
| M8 unmeasured branch: `repo_gate_cases=None` → `set()` | `test_run_tests_unmeasured_declaration.py::test_an_unmeasured_repo_gate_may_not_prune_its_known_red` |
| M18 computed-pass branch: `set()` → `None` | `test_run_tests_unmeasured_declaration.py::test_a_repo_gate_that_measured_green_may_prune_its_known_red` |
| M17 add `"failed"`/`"red"` to the unmeasured state set | `test_run_tests_unmeasured_declaration.py::test_a_declaration_that_says_failed_is_not_an_absence[failed]`; `::test_the_unmeasured_state_set_holds_no_red_word` |
| M21 `line.startswith(PREFIX)` → `PREFIX in line` | `test_run_tests_unmeasured_declaration.py::test_a_log_echo_of_the_prefix_mid_line_is_not_a_declaration`; `::test_a_log_echo_of_the_case_prefix_mid_line_is_not_a_case_record` |

`measured` is not a dead key: the fold in `run_tests` and `_acquire_repo_gate`
both read it, and it is what decides whether a gate's cases may be pruned or
its red forgiven.

## What this card did not touch

* `configs/` — no diff. `max_loop: 3` on the `test_outcome → implement` edge is
  the loop bound the four witnesses exhausted, and it stays.
* `evidence/gate-cycle-accounting-20260921/` — not one byte.
* The game repository's `tools/godot_gate.py` — another card.
