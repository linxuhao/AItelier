# Delivery — gate never ran is not a failing gate (rev 8)

Date: 2026-09-23. Base: `9f599d0ff13d72c17c00391cda4d420ba36313ce` (the r7
candidate itself). Every bare exit code below is read straight from a pytest
invocation (no `| tail`, no `| tee`); the commands, cwd and exit codes are in
the matching file under `final/logs/`. The green 6 criteria were not rebuilt;
each was re-run once on this tree (see `logs/poles_green6.log`).

## What changed (code)

* Fixed the three real verbatim-repeat defects the director named and the
  r7 exemption hid: `aitelier/tools/run_tests/impl.py` (a duplicated
  `_FAILED_RE`), `core/git_ops.py` (`import os` twice), `core/workspace_manager.py`
  (`import os` twice).
* Replaced the "one statement repeated verbatim is not a defect signal"
  exemption with an actual reader. `tests/unit/`
  `test_no_verbatim_previous_line_dup.py` runs the rule — a non-blank,
  non-comment line identical to the previous non-blank line — over the whole
  tree. Non-test code: ZERO hits (asserted). tests/: a NAMED list of the 13
  intentional double-calls, length pinned to 13. The AST self-call collapse
  guard moved to `tests/unit/test_no_self_call_collapse.py`. The old
  `tests/unit/test_source_lines_are_not_mangled.py` is deleted (it carried the
  230/93 derived exemption and the "not a defect signal" header).
* The mutation catalog is the first deliverable: `tools/mutation_catalog/`
  `mutations.py` lists every named mutation as a concrete anchored edit; the
  7 goal-table ones (N9, M21, M21b, G2, G2b, DUPIMPL, RESTART) are written
  VERBATIM per the card. `run_mutations.py` applies each to a copy of the tree,
  checks the anchor hits exactly once, runs targeted and full
  (`tests/unit tests/skillflow`), reads the bare rc, records the red tests, and
  discards the copy. `tests/unit/test_mutation_catalog_anchors.py` asserts, on
  the delivered tree, that every anchor matches exactly once.
* The deferral now has readers of its own: a test that does NOT touch the
  constants it guards reads the module as-loaded and asserts the effective
  episode ceiling is 10800s (`test_the_effective_episode_ceiling_is_ten_`
  `thousand_eight_hundred_as_loaded`), and a real-graph test asserts the `test`
  step row is completed with `retry_count == 0`, `release_count == 0`,
  `claim_epoch == 1` (`test_the_deferral_hold_leaves_real_step_rows_untouched`).
  The stub test whose docstring claimed to assert retry/release was corrected:
  it measures `claims == 0` and the hold, and points at the real-row test for
  the counters.
* The corrupted contract documents were fixed and read back: the mashed
  sentence at `docs/repo-gate-unmeasured-protocol.md:78` and the duplicated
  sentence on the next line; the mashed fragment at `:195`; the M21 section
  (now states, honestly, that literal M21 is behaviourally inert and the
  observable mutation is the `in`+`line.index` pair, and no longer claims
  `_apply_m21` "kills M21"); and `core/gate_deferral.py`'s header ("one
  execution point" -> two: the tick AND `skillflow_host.advance_run`).

## Two-pole evidence (bare rc)

| mutation | applied as | targeted test | rc |
|---|---|---|---|
| G2 | `GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9` | effective-ceiling reader | 1 (21600 != 10800) |
| G2b | `GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9` | effective-ceiling reader | 1 (ceiling const != 21600) |
| N9 | delete `tool.yaml` 52-56 | N9 witness on the segment | 1 |
| DUPIMPL | duplicate `_ERROR_RE` line | non-test verbatim-dup checker | 1 (names impl.py) |
| RESTART | persist the deferral ledger | restart re-measure test | 1 (count 1 != 0) |
| M21 (literal) | `in`, slice UNCHANGED | echo witnesses | 0 (INERT — see below) |

Reverted each after recording; the tree is back to green (`logs/poles_green6.log`
and `logs/reader_green.log`, rc 0).

## Honest limitation on M21 / M21b

The card's literal M21 (`startswith` -> `in`, `line[len(prefix):]` UNCHANGED)
has NO observable effect: the fixed 30-character slice can only land on the
JSON when the prefix already begins the line, which is the `startswith` case.
I verified this with the real reader: with literal M21 applied, the mid-line
echo witnesses all stay green (rc 0). The observable mutation is the PAIR
`in` + `line.index(prefix) + len(prefix)`; the suite reproduces that in-process
(`_apply_m21_pair`, formerly `_apply_m21`) and its witnesses go red. The catalog
therefore lists BOTH `M21`/`M21b` (verbatim, which the runner reports as
SURVIVED — not a kill) and `M21PAIR`/`M21BPAIR` (the pair, which is killed).
Renaming and this paragraph are the "把 `_apply_m21` 改名为它真正施加的东西"
the card asked for.

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
| `:52-56` no-verdict-does-not-loop-back | present, but pinned only as an adjacency comment | present AND asserted verbatim by `test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop` (N9 deletes it -> red) |
| `:57-63` the FLAG-not-from_file note | present | unchanged |
| `:69-72` REPO_GATE_UNMEASURED_ATTEMPTS = 1 | present | unchanged |

The only edit to `tool.yaml` this round was a revert-safe apply/revert of N9
during evidence collection; the delivered `tool.yaml` equals the base.
