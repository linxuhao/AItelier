# harness.a-scenario-has-one-reading, rev 1: delivery note

- Branch `director/onereading-r1-20260924`, based on AItelier main `8b084c208dfaf688d862f806ba0a82d96670501d`.
- Two code commits:
  - `5183a2edb24e9cf95bfd3c309c9f833edd4d91d8`: the three original checks.
  - `4b1baff3f0bc7d8819e84bcadce5faa942152f24`: the director's ruling of 2026-09-24 ~13:45Z. `description` is allowed at the scenario level as a plain string only, and a non-integer `at` is refused.
- Both commits change only `docker/godot/godot_harness.py` and `tests/unit/test_playtest_one_reading.py`. Every run below was measured on `4b1baff3` unless a row names `5183a2ed`. The commits after `4b1baff3` add or change only `final/` and `logs/`.
- Logs are under `logs/`. Each one records its command, its tree sha, its dirty-file count and a bare exit code written as `BARE_RC=`, with no pipe.
- No State DAG writes were made. The production `aitelier-godot` container and its lock were not touched: it still runs image `758e8115…`, started 2026-09-20. Nothing ran in the `aitelier` container. The game repo was read only with `git archive`.

## What changed (line numbers in `docker/godot/godot_harness.py` at `4b1baff3`)

1. **Unknown keys.**
   - `_SPEC_KEYS` = {scene, frames, scenarios, actions, surface} (`:1467`).
   - `_SCENARIO_KEYS` = {name, timeline, scene, repeatability, description} (`:1468`).
   - `_playtest_spec` checks the spec's top level (`:1734`) and each scenario (`:1743`).
   - An unknown key is a spec error that names the scenario, the key and the allowed set.
   - A refused scenario is not run. It gets a behaviour row with `ran: false, passed: false, asserts: []` (`:1754-1761`). For an unknown top-level key, no scenario runs.
2. **`description` only as a plain string** (`:1748-1753`). Any other type is a spec error that names the scenario, the key and the type, and the scenario is not run. That covers a mapping, a list, an int, a float, a bool and null. `description` is not allowed at the spec level.
3. **Timeline order** (`:1486`, `:1530-1537`). An entry whose `at` is lower than an earlier entry's `at` is a spec error. The error names both entries by index and frame. Equal frames are legal. The entry stays in the timeline, and the spec error hard-fails the gate.
4. **A frame is a whole number** (`:1517-1521`).
   - A float `at` that is not a whole number is a spec error naming the entry and the value, and the entry is dropped like any other malformed `at`.
   - Before this change, `int()` ran `185.5` at frame 185.
   - `at` values that were already ints work as before, and whole-number floats (`200.0`) still run as 200.
   - Bool and string `at` were already refused as non-numeric, and still are.
5. **Path targets.**
   - In the probe, `_resolve` (`:1092-1107`) resolves a `/` name relative to the current scene, or returns null. The `find_child(leaf)` fallback is removed.
   - `_point_of` (click/hover, `:893-895`) and `_eval_assert` (`:1032-1034`) record a scene-relative path that does not resolve through `_refuse_path` (`:1114`). That writes into the probe's `spec_errors`, which `_finish` puts in its JSON.
   - `_playtest_spec` (`:1824-1825`) copies them into the response's `spec_errors`, prefixed with the scenario name.
   - Bare names still go through `find_child(name)`, and absolute `/root`/`res:` paths still go through `get_node_or_null`, as before.

**Which response field carries a refusal:** `spec_errors`, a list of strings. `passed` is `false`. `summary` starts with `Playtest HARD-failed: N spec violation(s) -- <first error>`. A refusal is never put into `errors` (runtime), never raised, and never turned into an HTTP 500. The engine run below shows `errors: []` next to the two path refusals.

## Criteria

### a-path-that-does-not-resolve-never-falls-back-to-a-leaf

Engine: ONE throwaway container at a time, from the image tagged `aitelier-godot:latest`, with the harness under test mounted over `/srv/godot_harness.py`.
- Flags: `--rm --init --network none`, and its own `/tmp/ctl` lock and owner paths. No port, no HTTP, and not the production container, lock DB or network alias.
- The harness is called in-process through `playtest_project`.
- Recipe: `final/engine_path_probe/run_engine.sh`, driver `final/engine_path_probe/drive.py`, project `final/engine_path_probe/proj/`.
- The project's scene has one button, `Panel/PlanEditKind1`, whose script counts presses.
- Scenario 1 clicks `X/PlanEditKind1` at frame 10 and asserts `Panel/PlanEditKind1.presses == 1` and `X/PlanEditKind1.presses == 1` at frame 20. The path `X/…` does not exist; the leaf does.
- Scenario 2 uses the bare `PlanEditKind1`.
- The driver's exit code is 0 when the report is `passed`, 1 when it is not.

| polarity | harness | image | BARE_RC | what the probe reported | log |
|---|---|---|---|---|---|
| base | `8b084c20` file, sha1 `0b937675…` | `e9a6237c…` | 0 | `passed: True`, `spec_errors: []`, `errors: []`. The `X/PlanEditKind1` click reached the leaf: `Panel/PlanEditKind1.presses == 1` passed. The `X/…` assert was evaluated against the leaf and passed. | `logs/engine_path_base.txt` |
| candidate | `4b1baff3` file, sha1 `a3421523…` | `e9a6237c…` | 1 | `passed: False`, `errors: []`, 2 `spec_errors`: `frame 10: aim target X/PlanEditKind1 is a path and does not resolve …` and `frame 20: assert target X/PlanEditKind1 …`. The click was not delivered (`Panel/PlanEditKind1.presses` observed 0). The `X/…` assert row carries `error: "path does not resolve: X/PlanEditKind1"`. Scenario 2 (bare name) passed. | `logs/engine_path_cand.txt` |

- The `aitelier-godot:latest` tag moved from `758e8115…` to `e9a6237c…` during this round; someone else rebuilt the image.
- Both rows above ran on `e9a6237c…`.
- An earlier base run on `758e8115…` gave the same result: BARE_RC=0, with the leaf clicked (`logs/engine_path_base_image758e.txt`).
- An earlier candidate run of the `5183a2ed` file on `758e8115…` gave BARE_RC=1 with the same two spec errors. Its log was overwritten by the run above.
- `summary` on the candidate: `Playtest HARD-failed: 2 spec violations -- scenario 'qualified_path_that_does_not_resolve': frame 10: …`.
- The candidate also marks scenario 1 `input_dead: True`: its only input was refused, so its end state equals the no-input control.
- Godot-free tests:
  - `test_a_path_the_probe_refused_comes_back_as_a_spec_error`: the probe's `spec_errors` reach the response's `spec_errors` and not `errors`. Mutant M4 turns it red (below).
  - `test_the_probe_resolves_a_path_by_path_only` and `test_the_probe_reports_an_unresolved_path_as_a_spec_error` are source checks on the probe text.

### an-unknown-key-at-any-level-is-an-error

- Tests (`tests/unit/test_playtest_one_reading.py`) call the real `_playtest_spec` with the engine replaced by a recorder.
  - BY1 as the review wrote it: the five geometry lines under the scenario's `reviewer_notes:`, copied from `~/.AItelier/director/reports/p48-r10-review-20260924/logs/03_ondisk_BY1-cand_edit.log`. It gives one spec error naming `'plan_route_event_reroll_budget'`, `reviewer_notes` and the allowed set. The engine is called 0 times, and the row reads `ran: False`.
  - The same notes as a spec top-level key are refused: `spec has unknown top-level key(s) reviewer_notes - allowed: …`, and nothing runs.
  - `description` as a plain string runs clean: `test_a_plain_string_description_is_allowed_and_runs`.
  - `description` of any other type is refused, and the scenario never reaches the engine: `test_a_description_that_is_not_a_string_is_refused`, with 6 cases.
    - `list`: BY1's notes block moved under `description:`.
    - `mapping`: one BY1 assert block.
    - `int`, `float`, `bool` and `null`.
    - Each case gives one spec error naming `'plan_route_event_reroll_budget'` and `key description of type <type>`.
  - The corpus's own keys (spec `scene/frames/actions/surface`, scenario `name/scene/repeatability/timeline`) run clean.
- Candidate: 24 of 24 tests in the new file pass, BARE_RC=0 (`logs/new_tests_cand.txt`).
- Both polarities. Each mutant below deletes one check in a detached worktree of `4b1baff3`, writes an ignition line where the check was, and runs the 17-file harness family (driver `final/scripts/mutate.py`):

| mutant | ignition | BARE_RC | red test(s) | log |
|---|---|---|---|---|
| M1 scenario-key check deleted | 52 | 1 | `test_by1_reviewer_notes_in_a_scenario_is_refused_and_the_scenario_does_not_run` | `logs/mut_M1_scenario_key_check_deleted.txt` |
| M2 spec top-level key check deleted | 43 | 1 | `test_an_unknown_top_level_spec_key_is_refused_and_nothing_runs` | `logs/mut_M2_spec_key_check_deleted.txt` |
| M5 `description` type check deleted | 52 | 1 | `test_a_description_that_is_not_a_string_is_refused` [list], [mapping], [int], [float], [bool], [null] | `logs/mut_M5_description_type_check_deleted.txt` |

- The driver summary is `logs/mut_driver.txt` (DRIVER_BARE_RC=0). After each mutant the tree was clean again (`CLEAN_AFTER=True`).
- Readers of each allowed key (`logs/description_readers.txt` for the grep evidence):
  - spec `scene`: `docker/godot/godot_harness.py:1718`.
  - spec `frames`: `:1719`.
  - spec `scenarios`: `:1720`.
  - scenario `name`: `:1740`.
  - scenario `timeline`: `:1741`.
  - scenario `scene`: `:1795`.
  - scenario `description`: its reader is the type check itself, at `:1748`. That check accepts a plain string only, and a string cannot carry assert blocks. Neither AItelier nor the game repo has any other reader. The grep hits are a GDScript comment at `godot_harness.py:931` and a combat technique's `description` in the game repo's `tests/test_original_faction_identity.py:275,305,324`.
  - spec `actions`: game repo `5603e3c8` `tests/test_playtest_contract_smoke.py:1226`.
  - spec `surface`: game repo `tests/test_coop_ui_isolation.py:117`.
  - scenario `repeatability`: game repo `tools/godot_gate.py:149` and `:162`, where the gate launcher replays the scenario inside the same request.
  - `actions`, `surface` and `repeatability` have no reader in AItelier code. Their readers are in the game repo.
  - `logs/description_readers.txt` was recorded on `4b1baff3`.

### the-timeline-runs-in-the-order-it-is-written

- BY2b as the review wrote it: the `at: 195` block written above the real f185 entry, copied from `…/logs/03_ondisk_BY2b-cand_edit.log`, with the f185 block and the f170/f200 entries as they stand in wuxia master `5603e3c8` (the file last changed in `73f01262`).
  - `_normalize_timeline` gives `timeline entry 2 (at: 185) is written after entry 1 (at: 195) …`.
  - `_playtest_spec` carries that error with the scenario name, and `passed: False`.
- Equal frames are legal: `test_the_same_frame_written_twice_is_legal`. A drop below an earlier maximum names the entry that holds that maximum: `test_a_drop_below_an_earlier_maximum_names_the_maximum`.
- Y4b (a non-integer `at`) as the review wrote it: the f185 block of `playtest/event_travel_plan_effects.yaml`, then the f200 click moved to `at: 185.5`, copied from `…/logs/03_ondisk_Y4b-cand_edit.log`.
  - `_normalize_timeline` gives `timeline entry 1 has a non-integer \`at\`: 185.5`, and no click is scheduled (`test_y4b_a_fractional_frame_is_refused`).
  - `_playtest_spec` carries that error with `'event_travel_plan_effects'` (`test_y4b_through_playtest_spec_is_a_spec_error`).
  - `at: 185` and `at: 200.0` run at 185 and 200 (`test_whole_frames_still_run_as_written`).
  - `at: true`, `"185"` and `"3..15"` are still refused as non-numeric (`test_a_bool_or_string_frame_is_still_refused`).

| mutant | ignition | BARE_RC | red tests | log |
|---|---|---|---|---|
| M3 order check deleted | 110 | 1 | `test_by2b_a_frame_written_after_a_later_frame_is_refused`, `test_by2b_through_playtest_spec_is_a_spec_error`, `test_a_drop_below_an_earlier_maximum_names_the_maximum` | `logs/mut_M3_order_check_deleted.txt` |
| M6 non-integer `at` check deleted | 112 | 1 | `test_y4b_a_fractional_frame_is_refused`, `test_y4b_through_playtest_spec_is_a_spec_error` | `logs/mut_M6_non_integer_at_check_deleted.txt` |
| M4 probe `spec_errors` not copied into the response | 44 | 1 | `test_a_path_the_probe_refused_comes_back_as_a_spec_error` | `logs/mut_M4_probe_spec_errors_not_lifted.txt` |

### the-corpus-impact-is-counted

- Command: `final/scripts/run_corpus.sh <log>`, which runs `final/corpus_impact.py` on the host python with the `4b1baff3` harness file (sha1 `a3421523…`). Log: `logs/corpus_impact.txt`, BARE_RC=0.
- Corpus: game repo master `5603e3c85b410e872130dfee2bf223613add0cef`, extracted with `git archive master playtest`.
- The script loads the corpus with AItelier's `read_spec` and passes it to the real `_playtest_spec` of both harness files, with the engine stubbed. An error is new when the candidate reports it and the base does not.
- Scenarios checked: 181. Spec top-level keys: `actions, scenarios, scene, surface`, 0 top-level errors.
- 2 of the 181 checked scenarios gain a new error. Both are `at` values that decrease in file order:
  - `consequence_work_income_inline`: entry 10 (at 220) comes after entry 9 (at 230).
  - `shelter_plan_kept_battle_support_is_attributed`: entry 32 (at 435) comes after entry 31 (at 480).
- The other rules add nothing on this corpus:
  - Unknown keys: 0 scenarios.
  - Non-string `description`: 0 scenarios. All 165 scenarios that carry one carry a string.
  - Non-integer `at`: 0 scenarios.
- The base harness reports 0 spec errors on this corpus and the candidate reports 2. `CAND_SCENARIOS_NOT_RUN 0 of 181`: an order error does not stop a scenario from running.
- This counts the 181 authored scenarios. The gate launcher's `__repeatability` replays copy 4 of them. The count does not include those copies.
- `/`-qualified click, hover or assert targets in the corpus: 0 targets, found by scanning the 181 scenarios' normalized timelines (`SLASH_TARGETS 0 in 0 scenarios`). Whether a target resolves by path or by leaf in the real engine was not measured this round (本轮未量).
- The first corpus run, on `5183a2ed` without the ruling, found 165 of 181, all from `unknown key(s) description`. It is superseded by this one.

### the-suite-stays-green-and-the-note-carries-every-number

- Every run below used `docker run --rm --init -m 3g` on `aitelier:latest`, through `final/scripts/runsuite.sh`. Inside each container, `git status` returned `IN_CONTAINER_GIT_STATUS_RC: 0`.

| run | tree | result | BARE_RC | log |
|---|---|---|---|---|
| whole suite, base | `8b084c20`, DIRTY_FILES 0 | 5345 passed, 10 skipped, 11 deselected | 0 | `logs/suite_base.txt` |
| whole suite, candidate | `4b1baff3`, DIRTY_FILES 0 | 5393 passed, 10 skipped, 11 deselected | 0 | `logs/suite_cand2.txt` |
| whole suite, first code commit | `5183a2ed`, DIRTY_FILES 0 | 5358 passed, 10 skipped, 11 deselected | 0 | `logs/suite_cand.txt` |
| harness family (16 files), base | `8b084c20` | 223 passed, 8 skipped | 0 | `logs/family_base.txt` |
| harness family (the same 16 plus the new file), candidate | `4b1baff3` | 247 passed, 8 skipped | 0 | `logs/family_cand.txt` |
| new test file only, candidate | `4b1baff3` | 24 passed | 0 | `logs/new_tests_cand.txt` |

- Collection on `4b1baff3` gives 5403 ids against 5355 on the base (`logs/collect_diff.txt`). The +48 is 24 ids from the new test file plus 24 new parameters of `test_a_round_file_carries_no_lie_keeping_phrase`, one for each file this round has changed so far, `final/` and `logs/` included.
- The two changed `test_fake_id_factory` ids are random UUID parameters. The count is the same in both collections.
- The known-flaky `test_parallel_runs_progress_but_competing_controllers_admit_one_owner` is collected in every whole-suite run. It was green in all three runs (0 `FAILED` lines in each log), so no 20x rerun was needed.
- The whole-suite run on `4b1baff3` (+48 against the base, the same +48 as the collection) launched clean. DIRTY_FILES 1 on the `4b1baff3` family, new-test and collection runs is `final/corpus_impact.py`, edited for the new error kinds and committed after those runs. It is outside `tests/`. On the mutant runs, the dirty file is the mutated harness file.
- Container cap:
  - My own throwaway containers never exceeded 3 at once.
  - The first round of mutants on `5183a2ed` launched with `SLOTS_IN_USE_AT_LAUNCH: 4` (my 2 whole-suite runs plus 2 other sessions' containers). So the server had 5 throwaway containers up while those mutants ran.
  - The `4b1baff3` mutants launched with 2 in use.
  - Engine containers: 1 at a time, and `ENGINE_CONTAINERS_RUNNING_AT_LAUNCH: 0` every time.
- Read-back diffs:
  - `logs/worddiff_docker_godot_godot_harness.py.txt` and `logs/worddiff_tests_unit_test_playtest_one_reading.py.txt` (base `8b084c20` to `4b1baff3`).
  - The `final/` files, this note included, are read back in `logs/worddiff_final_files.txt`, refreshed in the last commit.

## Not done

- **Deployment.** The `aitelier-godot` restart is not done; it is the director's, with the owner's consent. The production sidecar still runs the base harness on image `758e8115…`.
- **Engine resolution of `/` targets in the wuxia corpus.** Not measured. The corpus has 0 such targets.
- **Timeline errors still run the scenario.** A scenario with an order error, a dropped non-integer entry, or any pre-existing timeline error is still run, as before; the spec error hard-fails the gate. Only unknown keys and a non-string `description` stop a scenario from running.
