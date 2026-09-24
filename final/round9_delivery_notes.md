# Round 9 delivery notes — 2026-09-24

Scope: rev 9 items 1–4 of the card. Criteria 1–3 and 5 were already green on
the r8 candidate and were re-run on this tree unchanged (numbers below).

## 1. The list: every test that applies a catalog corruption to clean bytes
or builds a fixture out of a prose surface

Searches used (counts are matches returned, `search`, this tree):
1. `apply_to_text|apply_edits|surface_text|_prose_corpus|read_text|_head_read|_live_read`
   over `tests/` — 50 hits (output capped), all outside the two card files
   being unrelated fixtures (integration harnesses, contract readers); the two
   card files were then searched individually.
2. same identifier set plus `def test_`, `_read(`, `Path(__file__)` over
   `tests/unit/test_prose_corruption_catalog.py` — 44 hits.
3. same over `tests/unit/test_truncation_is_not_a_formatting_mistake.py` —
   40 hits.
4. follow-up greps: `copytree`, `_prose_corpus()[surface]`,
   `STRICT_PATCH_GUIDANCE_(EN|ZH)` usage — 4 + 1 + 3 hits.

### tests/unit/test_truncation_is_not_a_formatting_mistake.py

| test | source this round | change |
|---|---|---|
| test_each_known_corruption_is_caught_by_name[K*] | HEAD (clean via `CATALOG.clean_surface_text(_head_read)`, corrupted from the git rev or HEAD+edits) | unchanged, already HEAD |
| test_the_corpus_is_derived_from_the_filesystem_not_a_list | was live tree → HEAD (`_prose_corpus(_head_read)`) | changed |
| test_a_duplicate_line_planted_in_any_surface_fires_by_name | was live tree → HEAD (`_prose_corpus(_head_read)`) | changed |
| test_both_guidances_carry_the_split_advice | was the live import of `STRICT_PATCH_GUIDANCE_*` → HEAD (`_git_guidance("HEAD", label)`) | changed |
| test_the_agent_facing_prose_is_intact | live tree | kept: this IS the live-tree tripwire |
| test_every_prose_constant_in_core_is_accounted_for | live tree (`py.read_text`) | kept: live-tree tripwire |
| test_stale_hunk_remedy_matches_whole_words_not_substrings | inline strings, no surface | unchanged |

### tests/unit/test_prose_corruption_catalog.py

| test | source this round | change |
|---|---|---|
| test_each_edit_entry_applies_to_its_own_surface | HEAD | unchanged |
| test_k6c_duplicates_exactly_one_line_and_overwrites_nothing | HEAD | unchanged |
| test_a_rule_name_is_catalog_data_not_a_substring_of_the_prose | was live `_prose_corpus()[surface]` → HEAD (`MOD._head_read(surface)`) | changed |
| test_the_runner_copies_the_tree_and_does_not_mutate_the_source | dest half was live → HEAD (compared to `CATALOG.apply_to_text(MOD._head_read(...))`); the unchanged-source half stays live because the live tree is the thing it must see unchanged | changed (split by assertion) |
| test_a_new_prompt_module_is_unaccounted_and_names_itself | fixture `core/` was a live `copytree` → HEAD bytes (`_head_core_tree`, names and bytes from `git ls-tree HEAD` + `_head_read`) | changed |
| test_an_exempt_name_in_another_module_is_still_red | same | changed |
| test_the_new_module_fixture_is_green_and_the_scan_names_the_module | same | changed |
| test_only_selects_one_corruption_and_applies_only_that_one | dest bytes from HEAD | unchanged |
| test_the_empty_mutation_leaves_every_surface_clean | live tree | kept: tripwire |
| test_the_live_tree_scan_names_the_rule_when_the_tree_is_damaged | live tree by design | kept: the pole itself |
| test_re_exempting_a_corpus_prompt_is_an_assertion_error_naming_it | runs the real live check | kept: tripwire half |

New in this round (the pole over the whole list):
- `test_truncation_is_not_a_formatting_mistake.py::test_every_surface_driven_test_stays_green_on_a_damaged_live_tree[K1..K6e]` — `_read` returns the runner's damaged bytes for the entry's file; every listed test above is called and must stay green for all ten K entries, and `test_the_agent_facing_prose_is_intact` must go red, with the catalog's rule and the surface named (named via `_prose_violations` on the same damaged bytes, because the tripwire's own AssertionError message is repr-truncated by pytest at ~240 chars and cannot carry the name).
- `test_prose_corruption_catalog.py::test_the_catalog_list_stays_green_on_a_damaged_live_tree[K1..K6e]` — same mechanism for this file's list, new-module entries included.

## 2. The control command is now honest

Option 1 of the card: `run_corruptions.py` gained `--empty-control`
(`run_all(..., control=True)`). It runs the SAME selection with NO corruption
applied: one plan row with id `CONTROL`, `_apply` never called, so no path in
the throwaway tree is touched. The old `--targets <two files>` line was never
an empty control (without `--only` it applies all ten entries); the honest
empty-control command is now:

    python tools/prose_corruptions/run_corruptions.py --empty-control \
        --targets tests/unit/test_truncation_is_not_a_formatting_mistake.py \
                  tests/unit/test_prose_corruption_catalog.py

Test: `test_prose_corruption_catalog.py::test_the_empty_control_applies_no_corruption`
makes `_apply` fail the run if it is ever reached, stubs the copy/run/git
edges, and asserts the single `CONTROL` row, rc 0, empty `changed`, and the
recorded selection. This round ran only this unit-level proof (bare RC 0,
below); the runner's whole-suite runs belong to the reviewer.

## 3. Evidence, bare exit codes (focused_check probes of this step)

Each row: command, worktree, bare RC. Logs: `logs/prose_r9_focused_checks.txt`
(this step's captured probe output; run ids in the log). No run in this round
was a whole-suite run; the suite × 10 corruptions is the reviewer's run.

| what | command (focused_check) | bare RC |
|---|---|---|
| catalog file, all 56 tests | `python -m pytest -q tests/unit/test_prose_corruption_catalog.py` | 0 |
| truncation file, first run (new pole red on [K2], repaired) | `python -m pytest -q tests/unit/test_truncation_is_not_a_formatting_mistake.py` | 1 |
| truncation file, after repair, 55 tests | same | 0 |
| temp probe (rule-name ground truth), deleted after | `python -m pytest -q -s tests/unit/_tmp_probe_r9.py` | 0 |

Per criterion (this tree):
- `a-truncated-completion-is-told-apart-from-a-format-mistake`: pass — the
  m1/m2/m3 fixture tests are unchanged and green in the truncation-file run
  (RC 0); the corruption side is green in the catalog-file run (RC 0).
- `a-truncation-does-not-end-the-round`: pass — same two runs, r2 `3b3d6560`
  fixture unchanged.
- `the-message-tells-the-agent-to-split-not-to-reformat`: pass — m6 (prompt)
  and m7 (SSE) tests unchanged and green in the truncation-file run; the
  split-advice test now reads HEAD and still carries both markers.
- `the-named-corruptions-are-caught-as-written`: pass — catalog unchanged
  (no entry edited, K6c still the single repeated banner via `duplicate_line`),
  both damaged-tree poles green 10/10 on the list.
- `the-preflight-advice-cannot-push-past-the-ceiling`: pass — x1, the
  (module, name)-keyed exemptions, and the f-string handling are untouched and
  green; `tests/fixtures/` untouched this round.

## 4. Not done here, on purpose

- Whole-suite runs, single-tree-per-corruption runs, and the base-revision
  (`527beafa`) control runs are the reviewer's measurements; nothing in these
  notes claims them for this round.
