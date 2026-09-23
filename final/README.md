# Delivery — gate never ran is not a failing gate (rev 8)

Date: 2026-09-23. Base: `9f599d0ff13d72c17c00391cda4d420ba36313ce` (the r7
candidate itself). Every bare exit code below is read straight from a pytest
invocation (no `| tail`, no `| tee`). Every delivery log is a `.txt` file:
`*.log` is git-ignored (`.gitignore:67`), so a `.log` would not be delivered.
The commands, cwd and bare exit codes live in the named log.

## What changed (code)

* Fixed the three real verbatim-repeat defects the director named and the
  r7 exemption hid: `aitelier/tools/run_tests/impl.py` (a duplicated
  `_FAILED_RE`), `core/git_ops.py` (`import os` twice),
  `core/workspace_manager.py` (`import os` twice).
* Replaced the "one statement repeated verbatim is not a defect signal"
  exemption with an actual reader. `tests/unit/test_no_verbatim_previous_line_dup.py`
  runs the rule — a non-blank, non-comment line identical to the previous
  non-blank line — over the WHOLE tree with ZERO exemption for non-test code.
  Non-test hits: 0 (asserted). `tests/`: a NAMED list of the intentional
  two-call repeats (`file`, `statement` text, never a line number), its length
  pinned to 13; an unlisted repeat is red. The AST self-call collapse guard
  moved to `tests/unit/test_no_self_call_collapse.py`. The old
  `tests/unit/test_source_lines_are_not_mangled.py` is deleted (it carried the
  230/93 derived exemption and the "not a defect signal" header). Evidence:
  `logs/reader_green.txt` (bare rc 0).
* The mutation catalog is the first deliverable: `tools/mutation_catalog/mutations.py`
  lists every named mutation as one concrete anchored edit; the 7 goal-table
  ones (N9, M21, M21b, G2, G2b, DUPIMPL, RESTART) are written VERBATIM per the
  card. `run_mutations.py` applies each to a copy of the tree, checks the
  anchor hits exactly once, runs targeted AND full (`tests/unit tests/skillflow`),
  reads the bare rc, records the red test names, and discards the copy. A
  zero-hit anchor is reported ANCHOR-ERROR, never a kill.
  `tests/unit/test_mutation_catalog_anchors.py` asserts, on the delivered tree,
  that every anchor matches exactly once and that all 7 goal-table names are
  present. Evidence: `logs/mutation_catalog.txt` (bare rc 0).
* The deferral now has readers of its own: a test that does NOT touch the
  constants it guards reads the module AS LOADED and asserts the effective
  episode ceiling is 10800 s
  (`test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded`),
  and a real-graph test asserts the `test` step row is `completed` with
  `retry_count == 0`, `release_count == 0`, `claim_epoch == 1`
  (`test_the_deferral_hold_leaves_real_step_rows_untouched`).
* The M21 family is stated honestly. The card's LITERAL M21 (`startswith` ->
  `in`, the slice UNCHANGED) IS observable against the r4-shaped witness, and
  two new BEHAVIOUR tests measure the flip both ways
  (`test_the_r4_shaped_witness_flips_under_the_literal_m21`,
  `..._m21b`). The `in`+`line.index` PAIR is a SECOND, blunter mutation, named
  `M21PAIR`/`M21BPAIR` and applied in-process as `_apply_m21_pair` — the name
  says which one it is, which is the rename the card asked for.
* The corrupted contract documents were fixed and read back: the mashed
  sentence at `docs/repo-gate-unmeasured-protocol.md:78` and the duplicated
  sentence on the next line; the mashed fragment at `:195`; the stale M21
  section (rewritten; the old "behaviourally inert" / "killed in-suite" claims
  are gone, and the table's missing blank line is restored); and
  `core/gate_deferral.py`'s header ("one execution point" -> two: the tick AND
  `skillflow_host.advance_run`).

## Two-pole evidence on THIS tree (bare rc)

Every mutation below was applied to the REAL file with the declared write tool,
the named test was run, the process return code was read, and the file was
restored and re-run green. Full transcript: `logs/two_pole_mutations.txt`.

| mutation | applied as | reddened test | bare rc |
|---|---|---|---|
| N9 | delete `tool.yaml:52-56` | `test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop` | 1 |
| G2 | `GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9` | effective-ceiling reader (names 21600 != 10800) | 1 |
| G2b | `GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9` | effective-ceiling reader (names 1e9 != 21600) | 1 |
| DUPIMPL | duplicate `_ERROR_RE` line | non-test verbatim-dup checker (names impl.py) | 1 |
| M21 (literal) | `in`, slice UNCHANGED | `..._flips_under_the_literal_m21` (behaviour) | 1 |
| M21b (literal) | `in`, slice UNCHANGED | `..._flips_under_the_literal_m21b` (behaviour) | 1 |
| RESTART | persist the deferral ledger | `..._restart_re_measures` (1 != 0) | 1 |
| ATTEMPTS3 | `REPO_GATE_UNMEASURED_ATTEMPTS = 3` | `test_one_step_holds_the_scheduler_for_at_most_one_gate_run` (names "gate runs per step moved: 3") | 1 |
| TIMEOUTBIG | `REPO_GATE_TIMEOUT = 1e9` | `test_one_step_holds_the_scheduler_for_at_most_one_gate_run` (names "worst-case hold=1000000000.0s > 5400.0s") | 1 |

After every restore the same command returned bare rc 0; the criterion files on
the delivered tree are 82 passed, bare rc 0 (`logs/criteria_green.txt`).

## ABSUNMEAS / `the-declaration-survives` hand-off

`ABSUNMEAS` (a single absence-suppression branch) was removed in r7; its job is
taken by three separate, individually-killed behaviours now pinned in the
catalog and the suite: `ABSREL` (a declared absence must never set
`passed_relative`), `ABSSTATE` (an absence with no baseline gets state
`unmeasured`, never `seeded`/`compared`), and `ABSWRITE` (no baseline file is
written for an unmeasured absence). Each is anchored in
`tools/mutation_catalog/mutations.py` and guarded by
`tests/unit/test_run_tests_unmeasured_declaration.py`.

## Not touched

`evidence/gate-cycle-accounting-20260921/` and `configs/coding_impl.yaml` are
byte-for-byte unchanged (no diff); `max_loop: 3` is unchanged.

## tool.yaml alignment (before / after)

| segment | before (r7) | after (this round) |
|---|---|---|
| `:41-51` UNMEASURED declaration contract | present | unchanged |
| `:52-56` no-verdict-does-not-loop-back | present, pinned only as adjacency prose | present AND asserted verbatim by `test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop` (N9 deletes it -> bare rc 1) |
| `:57-63` the FLAG-not-from_file note | present | unchanged |
| `:69-72` REPO_GATE_UNMEASURED_ATTEMPTS = 1 | present | unchanged |

The only edit to `tool.yaml` this round was a revert-safe apply/revert of N9
during evidence collection; the delivered `tool.yaml` equals the base.

