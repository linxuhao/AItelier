# Director-defined prose corruptions, both poles

Deliverable of this round: a catalog of the corruptions the director named,
applied byte-for-byte by a runner that runs the whole suite in a throwaway
worktree and records the BARE exit code.

## What is in the tree

| path | role |
|---|---|
| `tools/prose_corruptions/catalog.py` | the corruption bytes, one entry per corruption; the only place they live |
| `tools/prose_corruptions/run_corruptions.py` | applies each entry to a throwaway worktree and runs the suite there |
| `tools/prose_corruptions/scan_joined_lines.py` | scans a range's added lines for the three splice shapes |

`tests/unit/test_truncation_is_not_a_formatting_mistake.py` derives its
corruption fixtures from the catalog (`GIT_CORRUPTIONS`), so the bytes that
measure the checker are the bytes the runner applies.
`tests/unit/test_prose_corruption_catalog.py` proves the catalog is complete,
that an edit entry refuses an `old` that does not occur exactly once, and that
a new prompt module is caught by the accounting check naming its own module.

## The corruptions

| id | file | change | rule that must fire |
|---|---|---|---|
| K1 | `templates/fix_tests.md` | the stale-hunk sentence becomes `send a smaller hunk.` | `stale_hunk_remedy` |
| K2 | `templates/fix_tests.md` (after K1) | `quote the \`sha\` from its \`citation\`` drops both nouns | `reference_advice_missing` |
| K3 | `core/output_migration.py` | the ZH sentence announcing the reference example becomes a newline | `unannounced_reference_example` |
| K4 | `core/zz_new_prompt.py` (new) | a plain `SYSTEM_PROMPT` constant | `unaccounted_prose_constant` |
| K5 | `core/zz_new_prompt.py` (new) | the same prompt as an f-string | `unaccounted_prose_constant` |
| K6a-K6e | r2/r3 bytes | EN pair-repeat, ZH corruption, duplicate banner, r3 trailing backtick, clipped block | `repeated_run`, `repeated_run`, `adjacent_duplicate_line`, `odd_backtick_count`, `severed_clause` |

## Candidate tree - every corruption goes red

Log: `logs/prose_corruptions_candidate.txt`. Suite:
`python -m pytest -q -p no:cacheprovider` over
`tests/unit/test_prose_corruption_catalog.py` and
`tests/unit/test_truncation_is_not_a_formatting_mistake.py`.

| corruption id | bare RC | a failing test that is this card's own |
|---|---|---|
| K1 | 1 | `test_each_known_corruption_is_caught_by_name[K1]` |
| K2 | 1 | `test_each_known_corruption_is_caught_by_name[K2]` |
| K3 | 1 | `test_each_known_corruption_is_caught_by_name[K3]` |
| K4 | 1 | `test_every_prose_constant_in_core_is_accounted_for` |
| K5 | 1 | `test_every_prose_constant_in_core_is_accounted_for` |
| K6a | 1 | `test_each_known_corruption_is_caught_by_name[K6a]` |
| K6b | 1 | `test_each_known_corruption_is_caught_by_name[K6b]` |
| K6c | 1 | `test_each_known_corruption_is_caught_by_name[K6c]` |
| K6d | 1 | `test_each_known_corruption_is_caught_by_name[K6d]` |
| K6e | 1 | `test_each_known_corruption_is_caught_by_name[K6e]` |

Ten corruptions, ten bare RC 1, ten named tests - the cross-product is 10/10,
and each row's test name carries its own surface.

## Base tree - the other pole

Log: `logs/prose_corruptions_base.txt`. Suite: the base commit's own
`tests/unit/test_truncation_is_not_a_formatting_mistake.py`, at
`527beafaf08a48bd2c3678bbc771e0208ccaa101`.

| corruption id | bare RC on base |
|---|---|
| K1 | 0 |
| K2 | 0 |
| K3 | 0 |
| K4 | 0 |
| K5 | 0 |
| K6a | 1 |
| K6b | 1 |
| K6c | 1 |
| K6d | 1 |
| K6e | 1 |

K1, K3, K4 and K5 leave the base suite green: those four had no reader before
this branch, and the catalog now goes red on each of them. K6a-K6e were
already caught on the base.

## The three splice shapes on this branch's added lines

Log: `logs/prose_corruptions_scan.txt`. Command:
`python tools/prose_corruptions/scan_joined_lines.py 527beafaf08a48bd2c3678bbc771e0208ccaa101`,
which scans the added lines of `git diff --unified=0 <base>`.
Tracked file with added lines: 1. Added lines: 320. Hits: 0.

Scope, stated so the number is not read as broader than it is: `git diff`
covers TRACKED changes, so the four files this branch ADDS
(`tools/prose_corruptions/{catalog,run_corruptions,scan_joined_lines}.py` and
`tests/unit/test_prose_corruption_catalog.py`) are outside that diff. Their
content is measured through the same scanner by that test file's own
self-proof checks. The 320 lines are the edited-and-tracked region the earlier
rounds' splices lived in.

The scanner itself is measured:
`test_the_spliced_line_scanner_fires_on_the_splice_shapes` plants
two-statements-on-one-line and sentence-split samples and requires the scanner
to name them; `test_the_spliced_line_scanner_is_silent_on_clean_lines`
requires silence on intact equivalents.

## Repairs this round

* `tests/unit/_tmp_git_probe.py` deleted.
* `tests/unit/test_truncation_is_not_a_formatting_mistake.py:618` - the two
  `subprocess.run(...)` lines were rejoined into two lines.
* `:731` - the grant-boundary banner regained its trailing rule and its own
  line.
* `:640-642` - the r5 `_sha_advice_replaced` fixture and its docstring were
  deleted outright (rule and fixture together), so the `"sha" in text`
  one-word rule is gone.

## Exemption table and prompt surfaces

`PROSE_CONSTANT_EXEMPTIONS` is keyed by `(module path, constant name)`. The
five constants an agent reads (`core/meta_agent.py:SYSTEM_PROMPT`,
`REVISION_SYSTEM_PROMPT`, `_INTENT_SYSTEM_PROMPT`, `_SPEC_HEADER`,
`STATE_DRIVER_GUIDE`) moved out of the exemptions into
`PROSE_PROMPT_CONSTANTS`, so they are corpus surfaces. The three
`agent_configs/coding_task.yaml` `system_prompt` blocks are corpus surfaces
too, read by glob. `ast.JoinedStr` f-strings are read the same way as
`ast.Constant` strings. The four rules that must fire on the catalog's bytes
are local to the sentence that teaches them: `stale_hunk_remedy`,
`unannounced_reference_example`, `reference_advice_missing` all read the
paragraph the rule lives in, so a word occurring elsewhere in the file cannot
satisfy any of them.
