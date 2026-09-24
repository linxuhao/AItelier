# Round 10 delivery notes — 2026-09-24

Base: `0409d8618ceb6ed8d7555f2e4e6138a561fd3ed4` (the r9 candidate itself).
Scope: items 1–4 of the round-10 goal. Item 4's rule is followed: every edit
below was made with V4A patches, no `from_col`/`to_col`.

Log for every number on this page: `logs/round10_probes.txt`. The probe run id
is `8ccd2c15-2b0a-4495-b9f5-cc50d2f1b3c7` (step `implement`); each probe's
command, cwd, 300 s deadline and bare exit code are recorded there.

## 1. Read-path inventory (one extra column on the r9 list)

Searches, with the counts returned in this tree (in `logs/round10_probes.txt`):
`semantic_search` 2 queries; `search` over
`tests/unit/test_prose_corruption_catalog.py` 31 hits; `search` over
`tests/unit/test_truncation_is_not_a_formatting_mistake.py` 47 hits.

### tests/unit/test_truncation_is_not_a_formatting_mistake.py

| test | byte paths it reads | path class |
|---|---|---|
| test_each_known_corruption_is_caught_by_name[K*] | `_head_read`→`_git_file("HEAD",rel)`→`git show` in `REPO_ROOT`; `_git_file(rev,rel)` for the r2/r3 entries | git, module constant `REPO_ROOT` |
| test_the_corpus_is_derived_from_the_filesystem_not_a_list | `_prose_corpus(_head_read)`; `(REPO_ROOT/"templates").glob("*.md")`; `(REPO_ROOT/"agent_configs").glob("*.yaml")` | git + `REPO_ROOT` glob |
| test_a_duplicate_line_planted_in_any_surface_fires_by_name | `_prose_corpus(_head_read)` | git |
| test_both_guidances_carry_the_split_advice | `_git_guidance("HEAD",label)`→`_git_file` | git |
| test_the_agent_facing_prose_is_intact | `_prose_corpus()`→`_read(rel)`→`(REPO_ROOT/rel).read_text`; the `templates/` and `agent_configs/` globs; `_constant_text`→`_read` | live disk + `REPO_ROOT` glob |
| test_every_prose_constant_in_core_is_accounted_for | `_core_module_paths(root=REPO_ROOT)`→`(root/"core").glob("*.py")`; `py.read_text` | live disk + `REPO_ROOT` glob (default bound at def time) |
| test_stale_hunk_remedy_matches_whole_words_not_substrings | inline strings | none |
| test_every_surface_driven_test_stays_green_on_a_damaged_live_tree[K*] (r9 pole) | stubs `_read` only | stub, not disk |
| test_every_surface_driven_test_stays_green_on_a_real_damaged_tree[K*] (this round) | `_RUNNER.damage_tree`→`_copy_tree` (`git archive`/`_copy_worktree` + `git ls-files`) + `_apply`; then `REPO_ROOT`→the tree: `_read`, `_head_read`, `_git_file`, both globs, `_core_module_paths` | live disk of a REAL damaged tree |
| test_apply_patch_* (2) | `core.agents` import | import-time module constant |

### tests/unit/test_prose_corruption_catalog.py

| test | byte paths it reads | path class |
|---|---|---|
| module import | `_load()` of the test module, `catalog.py`, `run_corruptions.py`, `scan_joined_lines.py` under `REPO_ROOT` | import-time module constants (`MOD`, `CATALOG`, `RUNNER`, `SCAN`) |
| test_each_edit_entry_applies_to_its_own_surface | `MOD._head_read` | git |
| test_k6c_duplicates_exactly_one_line_and_overwrites_nothing | `MOD._head_read("core/dpe_pipeline.py")` | git |
| test_a_rule_name_is_catalog_data_not_a_substring_of_the_prose | `MOD._head_read(surface)` | git |
| test_the_runner_copies_the_tree_and_does_not_mutate_the_source | `RUNNER._copy_tree(dest, rev="HEAD")` → `git archive` + `_link_history`; `MOD._head_read`; `(dest/…).read_text`; `(REPO_ROOT/…).read_text` for the unchanged-source half | git + `REPO_ROOT` disk |
| test_a_new_prompt_module_is_unaccounted_and_names_itself | `_head_core_tree`: `git ls-tree --name-only HEAD core` + `MOD._head_read` | git |
| test_an_exempt_name_in_another_module_is_still_red | same | git |
| test_the_new_module_fixture_is_green_and_the_scan_names_the_module | same | git |
| test_every_exemption_is_keyed_by_module_and_name_with_a_reason | `(REPO_ROOT/module).exists()` | `REPO_ROOT` disk |
| test_only_selects_one_corruption_and_applies_only_that_one | `MOD._head_read` for the tree bytes, then `dest` disk | git + temp disk |
| test_the_empty_mutation_leaves_every_surface_clean | `MOD._prose_corpus()`→`MOD._read`→`REPO_ROOT/rel` | live disk |
| test_the_live_tree_scan_names_the_rule_when_the_tree_is_damaged[K*] (r9) | stubs `MOD._read` only | stub, not disk |
| test_the_card_leaves_no_stray_prompt_module_or_probe | `(REPO_ROOT/core/…).exists()` | `REPO_ROOT` disk |
| test_the_catalog_list_stays_green_on_a_damaged_live_tree[K*] (r9 pole) | stubs `MOD._read`; the r9 copy half called `RUNNER._copy_tree` WITHOUT `rev`, i.e. `_copy_worktree` → `git ls-files` + live disk `shutil.copy` | stub + live disk (the gap) |
| test_the_catalog_list_stays_green_on_a_real_damaged_tree[K*] (this round) | `RUNNER.damage_tree` (real `_copy_tree`/`_apply` on disk) with `REPO_ROOT` here, `MOD.REPO_ROOT` and `RUNNER.REPO_ROOT` all pointed at the tree | live disk of a REAL damaged tree |

### The gap the r9 pole left, named from this table

`RUNNER._copy_tree(dest)` without `rev` reaches `_copy_worktree` →
`_tracked_paths()` (`git ls-files`) → `shutil.copy(REPO_ROOT/rel, …)`: live
worktree bytes, no `_read` in that path. The r9 pole stubbed `_read` only, so
that path was never damaged and the pole was green for the wrong reason.

## 2. `test_the_runner_copies_the_tree_and_does_not_mutate_the_source`

Changed (V4A, one region): the copy half now calls
`RUNNER._copy_tree(dest, rev="HEAD")`, whose bytes come from `git archive HEAD`
and never from the live worktree; the unchanged-source half still compares
`(REPO_ROOT/entry["path"]).read_text()` against the live bytes it captured, and
the assertion against `CATALOG.apply_to_text(MOD._head_read(...))` stays.

The docstring's false sentence ("The dest half … stays meaningful while a
corruption run has the live tree damaged") is replaced by a statement of the
source each half reads. In the real damaged tree built below, the file
`templates/fix_tests.md` already carries K1, so the old no-`rev` form finds the
entry's `old` zero times and this test goes red on a precondition; with
`rev="HEAD"` it is green (probes [2], [3]).

## 3. The poles are now real disk damage

`tools/prose_corruptions/run_corruptions.py` gained `damage_tree(dest, entry,
catalog, rev="HEAD")` — `_copy_tree(dest, rev)` then `_apply(entry, dest,
catalog)`: HEAD's bytes committed, the corruption written into the working
tree and left uncommitted, the same shape `run_all` produces. Two tests use it:

* `test_prose_corruption_catalog.py::test_the_catalog_list_stays_green_on_a_real_damaged_tree[K1..K6e]`
  — `REPO_ROOT` here, `MOD.REPO_ROOT` and `RUNNER.REPO_ROOT` point at the tree;
  the listed tests are called and must stay green; the live-tree tripwire
  `test_the_empty_mutation_leaves_every_surface_clean` must raise, and the rule
  and surface are named from the tree's own disk bytes. For K4/K5 the
  accounting scan over the tree names `core/zz_new_prompt.py`.
* `test_truncation_is_not_a_formatting_mistake.py::test_every_surface_driven_test_stays_green_on_a_real_damaged_tree[K1..K6e]`
  — same construction; `test_the_agent_facing_prose_is_intact` must raise and
  the rule is named from the disk corpus.

Parameters: all ten catalog entries (K1, K2, K3, K4, K5, K6a, K6b, K6c, K6d,
K6e) in each file; none sampled. The 300 s focused_check deadline was enough
for the whole ten in one probe per file (probe [3]: 10 passed; probe [2]
includes the catalog file's ten). The r9 stub poles are kept as regression
guards, not deleted.

## 4. Bare exit codes (focused_check probes of this step)

Each row: what, command, bare exit code. Full text in
`logs/round10_probes.txt`, probe run id `8ccd2c15-…:implement`.

| # | what | command (focused_check, `kind=pytest`) | bare RC |
|---|---|---|---|
| 1 | real-damage pole, first run ([K1],[K4]) | `-m pytest -q -s tests/unit/test_prose_corruption_catalog.py::test_the_catalog_list_stays_green_on_a_real_damaged_tree[K1] [K4]` | 1 |
| 2 | real-damage pole after repair, catalog file, 66 tests | `-m pytest -q -s tests/unit/test_prose_corruption_catalog.py` | 0 |
| 3 | real-damage pole, truncation file, 10 parameters | `-m pytest -q -s tests/unit/test_truncation_is_not_a_formatting_mistake.py::test_every_surface_driven_test_stays_green_on_a_real_damaged_tree` | 0 |
| 4 | truncation file, 65 tests (r9 poles included) | `-m pytest -q -s tests/unit/test_truncation_is_not_a_formatting_mistake.py` | 0 |
| 5 | both card files together, 131 tests | `-m pytest -q -s tests/unit/test_prose_corruption_catalog.py tests/unit/test_truncation_is_not_a_formatting_mistake.py` | 0 |

Row 1 is the pole's own two poles in miniature: `[K1]` passed while `[K4]`
failed, on the accounting scan's def-time `root=REPO_ROOT` default, which the
redirected module attribute cannot reach; the fix passes the tree explicitly.

## 5. Per criterion

Criterion 1, `a-truncated-completion-is-told-apart-from-a-format-mistake`:
pass — probe [4], bare RC 0, 65 passed; the m1/m2/m3 fixtures
(`TestTheTwoCausesAreToldApart`) are unchanged this round and green there.
The catalog side is probe [2], bare RC 0.

Criterion 2, `a-truncation-does-not-end-the-round`: pass — probe [4], bare
RC 0; `TestATruncationDoesNotEndTheRound` unchanged, the r2 `3b3d6560`
fixture unchanged.

Criterion 3, `the-message-tells-the-agent-to-split-not-to-reformat`: pass —
probe [4], bare RC 0; the prompt assertion and the SSE assertion are
unchanged and green, and `test_both_guidances_carry_the_split_advice` reads
HEAD and still carries both markers.

Criterion 4, `the-named-corruptions-are-caught-as-written`: pass on the
measures of this step — `tools/prose_corruptions/catalog.py` is unmodified
this round (K6c is still the single repeated banner via `duplicate_line`, no
entry rewritten, weakened or replaced), and both real-damage poles are green
over all ten entries against the catalog's own rules (probes [2], [3], bare
RC 0). The per-corruption bare-RC table over the whole suite is the
reviewer's measurement.

Criterion 5, `the-preflight-advice-cannot-push-past-the-ceiling`: pass —
probes [2] and [4], bare RC 0; the (module, name)-keyed exemptions, the
f-string handling and the rule-name-as-data test are untouched this round,
and the new pole exercises the accounting scan on a real tree.

The mutation poles for criteria 1–3 and 5 are the round-9 ones kept in this
tree (m1/m2/m3, m5, m6/m7, x1); this round re-ran the two files that carry
them rather than editing the tree to plant mutations, as the discipline
requires.

## 6. Not measured here, stated as not measured

No whole-suite run, no `run_corruptions.py` execution, no base-revision
(`527beafa`) run, and no `git diff -- tests/fixtures/` was taken in this
step: the executor has only `focused_check`. Those are the reviewer's
measurements and no line above claims them. The corpus is untouched by this
round's two patched files, which are `tools/prose_corruptions/run_corruptions.py`
and the two test files.
