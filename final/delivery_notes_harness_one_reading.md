# harness.a-scenario-has-one-reading, rev 1: delivery note

- Branch `director/onereading-r1-20260924`, based on AItelier main `8b084c208dfaf688d862f806ba0a82d96670501d`.
- Code commit: `5183a2edb24e9cf95bfd3c309c9f833edd4d91d8`. It changes `docker/godot/godot_harness.py` and adds `tests/unit/test_playtest_one_reading.py`. Every run below was measured on that commit. The commit after it adds only `final/` and `logs/`.
- Logs are under `logs/`. Each one records its command, its tree sha, its dirty-file count and a bare exit code written as `BARE_RC=`, with no pipe.
- No State DAG writes were made. The production `aitelier-godot` container and its lock were not touched. Nothing ran in the `aitelier` container. The game repo was read only with `git archive`.

## What changed

1. Unknown keys. `_SPEC_KEYS` = {scene, frames, scenarios, actions, surface} and `_SCENARIO_KEYS` = {name, timeline, scene, repeatability} (`docker/godot/godot_harness.py:1465-1466`).
   - `_playtest_spec` checks the spec's top level (`:1724-1728`) and each scenario (`:1733-1745`).
   - An unknown key is a spec error that names the scenario, the key and the allowed set.
   - A refused scenario is not run. It gets a behaviour row with `ran: false, passed: false, asserts: []`. For an unknown top-level key, no scenario runs.
2. Timeline order. `_normalize_timeline` (`:1484`, `:1520-1527`) reports an entry whose `at` is lower than an earlier entry's `at`. The error names both entries by index and frame. Equal frames are legal. The entry stays in the timeline, and the spec error hard-fails the gate.
3. Path targets. In the probe, `_resolve` (`:1092-1106`) resolves a `/` name relative to the current scene, or returns null. The `find_child(leaf)` fallback is removed.
   - `_point_of` (click/hover, `:893-895`) and `_eval_assert` (`:1032-1034`) record a scene-relative path that does not resolve through `_refuse_path` (`:1114`). That writes into the probe's `spec_errors`, which `_finish` puts in its JSON.
   - `_playtest_spec` (`:1808-1809`) copies them into the response's `spec_errors`, prefixed with the scenario name.
   - Bare names still go through `find_child(name)`, and absolute `/root`/`res:` paths still go through `get_node_or_null`, as before.

**Which response field carries a refusal:** `spec_errors`, a list of strings. `passed` is `false`. `summary` starts with `Playtest HARD-failed: N spec violation(s) -- <first error>`. A refusal is never put into `errors` (runtime), never raised, and never turned into an HTTP 500. The engine run below shows `errors: []` next to the two path refusals.

## Decision for the director: `description:` has no reader, so it is refused

165 of the 181 corpus scenarios carry a scenario-level `description:` (`logs/corpus_impact.txt`).
- The criterion requires every key in the allowed set to have a real reader. I found none for a scenario's `description` (`logs/description_readers.txt`):
  - CMD1 greps every AItelier file that reads a play-test spec. Its one hit is a GDScript comment at `godot_harness.py:931`.
  - CMD2 greps the game repo's `tests/` and `tools/`. All three hits read a combat technique's `description`, not a scenario's (`tests/test_original_faction_identity.py:275,305,324`).
- So `description` is not in `_SCENARIO_KEYS`. Deploying this candidate as it stands makes the wuxia gate hard-fail those 165 scenarios until a wuxia card removes the key or turns it into YAML comments.
- If the director instead admits `description`, it needs a reader and a type rule. A non-string `description` can carry assert blocks the same way `reviewer_notes` did. With `description` admitted, the corpus impact would drop to the 2 timeline-order scenarios listed below.
- I did not make that call. It changes the product contract.

## Criteria

### a-path-that-does-not-resolve-never-falls-back-to-a-leaf

Engine: ONE throwaway container at a time, run from the sidecar image (`sha256:758e8115…`), with the harness under test mounted over `/srv/godot_harness.py`.
- Flags: `--rm --init --network none`, and its own `/tmp/ctl` lock and owner paths. No port, no HTTP, and not the production container, lock DB or network alias.
- The harness is called in-process through `playtest_project`.
- Recipe: `final/engine_path_probe/run_engine.sh`, driver `final/engine_path_probe/drive.py`, project `final/engine_path_probe/proj/`.
- The project's scene has one button, `Panel/PlanEditKind1`, whose script counts presses.
- Scenario 1 clicks `X/PlanEditKind1` at frame 10 and asserts `Panel/PlanEditKind1.presses == 1` and `X/PlanEditKind1.presses == 1` at frame 20. The path `X/…` does not exist; the leaf does.
- Scenario 2 uses the bare `PlanEditKind1`.
- The driver's exit code is 0 when the report is `passed`, 1 when it is not.

| polarity | harness | BARE_RC | what the probe reported | log |
|---|---|---|---|---|
| base | `8b084c20` file, sha1 `0b937675…` | 0 | `passed: True`, `spec_errors: []`, `errors: []`. The `X/PlanEditKind1` click reached the leaf: `Panel/PlanEditKind1.presses == 1` passed. The `X/…` assert was evaluated against the leaf and passed. | `logs/engine_path_base.txt` |
| candidate | `5183a2ed` file, sha1 `ab99c3c3…` | 1 | `passed: False`, `errors: []`, 2 `spec_errors`: `frame 10: aim target X/PlanEditKind1 is a path and does not resolve …` and `frame 20: assert target X/PlanEditKind1 …`. The click was not delivered (`Panel/PlanEditKind1.presses` observed 0). The `X/…` assert row carries `error: "path does not resolve: X/PlanEditKind1"`. Scenario 2 (bare name) passed. | `logs/engine_path_cand.txt` |

- `summary` on the candidate: `Playtest HARD-failed: 2 spec violations -- scenario 'qualified_path_that_does_not_resolve': frame 10: …`.
- The candidate also marks scenario 1 `input_dead: True`: its only input was refused, so its end state equals the no-input control.
- Godot-free tests in the suite:
  - `test_a_path_the_probe_refused_comes_back_as_a_spec_error`: the probe's `spec_errors` reach the response's `spec_errors` and not `errors`. Mutant M4, which drops that copy step, turns it red (below).
  - `test_the_probe_resolves_a_path_by_path_only` and `test_the_probe_reports_an_unresolved_path_as_a_spec_error` are source checks on the probe text.

### an-unknown-key-at-any-level-is-an-error

- Tests (`tests/unit/test_playtest_one_reading.py`) call the real `_playtest_spec` with the engine replaced by a recorder.
  - BY1 as the review wrote it: the five geometry lines under the scenario's `reviewer_notes:`, copied from `~/.AItelier/director/reports/p48-r10-review-20260924/logs/03_ondisk_BY1-cand_edit.log`. It gives one spec error naming `'plan_route_event_reroll_budget'`, `reviewer_notes` and the allowed set. The engine is called 0 times, and the row reads `ran: False`.
  - The same notes as a spec top-level key are refused: `spec has unknown top-level key(s) reviewer_notes - allowed: …`, and nothing runs.
  - The corpus's own keys (spec `scene/frames/actions/surface`, scenario `name/scene/repeatability/timeline`) run clean.
- Candidate: 11 of 11 new tests pass, BARE_RC=0 (`logs/new_tests_cand.txt`).
- Both polarities. Each mutant below deletes one check in a detached worktree of `5183a2ed`, writes an ignition line where the check was, and runs the 17-file harness family:

| mutant | ignition | BARE_RC | red test(s) | log |
|---|---|---|---|---|
| M1 scenario-key check deleted | 44 | 1 | `test_by1_reviewer_notes_in_a_scenario_is_refused_and_the_scenario_does_not_run` | `logs/mut_M1_scenario_key_check_deleted.txt` |
| M2 spec top-level key check deleted | 35 | 1 | `test_an_unknown_top_level_spec_key_is_refused_and_nothing_runs` | `logs/mut_M2_spec_key_check_deleted.txt` |

- Driver: `final/scripts/mutate.py`, summary in `logs/mut_driver.txt` (DRIVER_BARE_RC=0). After each mutant the tree was clean again (`CLEAN_AFTER=True`).
- Readers of each allowed key (`logs/description_readers.txt`):
  - spec `scene`: `docker/godot/godot_harness.py:1708`.
  - spec `frames`: `:1709`.
  - spec `scenarios`: `:1710` (and `:2018`).
  - scenario `name`: `:1730`.
  - scenario `timeline`: `:1731`.
  - scenario `scene`: `:1779`.
  - spec `actions`: game repo `5603e3c8` `tests/test_playtest_contract_smoke.py:1226`.
  - spec `surface`: game repo `tests/test_coop_ui_isolation.py:117`.
  - scenario `repeatability`: game repo `tools/godot_gate.py:149` and `:162`, where the gate launcher replays the scenario inside the same request.
  - `actions`, `surface` and `repeatability` have no reader in AItelier code. Their readers are in the game repo.

### the-timeline-runs-in-the-order-it-is-written

- BY2b as the review wrote it: the `at: 195` block written above the real f185 entry, copied from `…/logs/03_ondisk_BY2b-cand_edit.log`, with the f185 block and the f170/f200 entries as they stand in wuxia master `5603e3c8` (the file last changed in `73f01262`).
  - `_normalize_timeline` gives `timeline entry 2 (at: 185) is written after entry 1 (at: 195) …`.
  - `_playtest_spec` carries that error with the scenario name, and `passed: False`.
- Equal frames are legal: `test_the_same_frame_written_twice_is_legal`. A drop below an earlier maximum names the entry that holds that maximum: `test_a_drop_below_an_earlier_maximum_names_the_maximum`.

| mutant | ignition | BARE_RC | red tests | log |
|---|---|---|---|---|
| M3 order check deleted | 93 | 1 | `test_by2b_a_frame_written_after_a_later_frame_is_refused`, `test_by2b_through_playtest_spec_is_a_spec_error`, `test_a_drop_below_an_earlier_maximum_names_the_maximum` | `logs/mut_M3_order_check_deleted.txt` |
| M4 probe `spec_errors` not copied into the response | 42 | 1 | `test_a_path_the_probe_refused_comes_back_as_a_spec_error` | `logs/mut_M4_probe_spec_errors_not_lifted.txt` |

### the-corpus-impact-is-counted

- Command: `final/scripts/run_corpus.sh <log>`, which runs `final/corpus_impact.py` on the host python. Log: `logs/corpus_impact.txt`, BARE_RC=0.
- Corpus: game repo master `5603e3c85b410e872130dfee2bf223613add0cef`, extracted with `git archive master playtest`.
- The script loads the corpus with AItelier's `read_spec` and passes it to the real `_playtest_spec` of both harness files, with the engine stubbed. An error is new when the candidate reports it and the base does not.
- Scenarios checked: 181. Spec top-level keys: `actions, scenarios, scene, surface`, 0 top-level errors.
- 165 of the 181 checked scenarios gain a new error, and all 165 would not run.
  - Reason 1: 165 of 181 carry `unknown key(s) description`.
  - Reason 2: 2 of 181 also have an `at` that decreases in file order:
    - `consequence_work_income_inline`: entry 10 (at 220) comes after entry 9 (at 230).
    - `shelter_plan_kept_battle_support_is_attributed`: entry 32 (at 435) comes after entry 31 (at 480).
  - Every scenario name is in the log, one per `NEW_ERROR_SCENARIO` line.
  - The base harness reports 0 spec errors on this corpus. The candidate reports 167: 165 unknown-key errors plus 2 order errors.
- This counts the 181 authored scenarios. The gate launcher's `__repeatability` replays copy 4 of them. The count does not include those copies.
- `/`-qualified click, hover or assert targets in the corpus: 0 targets, found by scanning the 181 scenarios' normalized timelines (`SLASH_TARGETS 0 in 0 scenarios`). With none to list, the path-versus-leaf question has no corpus instance. Whether a target resolves by path or by leaf in the real engine was not measured this round (本轮未量).

### the-suite-stays-green-and-the-note-carries-every-number

- Every run below used `docker run --rm --init -m 3g` on `aitelier:latest`, through `final/scripts/runsuite.sh`. Inside each container, `git status` returned `IN_CONTAINER_GIT_STATUS_RC: 0`.

| run | tree | result | BARE_RC | log |
|---|---|---|---|---|
| whole suite, base | `8b084c20`, DIRTY_FILES 0 | 5345 passed, 10 skipped, 11 deselected | 0 | `logs/suite_base.txt` |
| whole suite, candidate | `5183a2ed`, DIRTY_FILES 0 | 5358 passed, 10 skipped, 11 deselected | 0 | `logs/suite_cand.txt` |
| harness family (16 files), base | `8b084c20` | 223 passed, 8 skipped | 0 | `logs/family_base.txt` |
| harness family (the same 16 plus the new file), candidate | `5183a2ed` | 234 passed, 8 skipped | 0 | `logs/family_cand.txt` |
| new tests only, candidate | `5183a2ed` | 11 passed | 0 | `logs/new_tests_cand.txt` |

- The +13 on the candidate suite is 11 new tests plus 2 new parameters of `test_a_round_file_carries_no_lie_keeping_phrase`, one for each changed file (`logs/collect_diff.txt`).
- In that collection diff, the two changed `test_fake_id_factory` ids are random UUID parameters. The count is the same in both collections.
- The known-flaky `test_parallel_runs_progress_but_competing_controllers_admit_one_owner` was green in both whole-suite runs, so no 20x rerun was needed.
- DIRTY_FILES 1 on the family, new-test, collection and mutant candidate runs:
  - On the family, new-test and collection runs, the dirty file is the untracked `final/corpus_impact.py`, outside `tests/`.
  - On the mutant runs, it is the mutated harness file.
- Container cap:
  - Every launch of mine had at most 3 of my own throwaway containers up.
  - The four mutant launches recorded `SLOTS_IN_USE_AT_LAUNCH: 4`. That counts every `aitelier:latest` container other than `aitelier`: my 2 whole-suite runs plus 2 other sessions' containers (`lqmerge-suite`, `wuxia-gate-growth-merge-…`). So server-wide there were 5 while each mutant ran.
  - Engine containers: 1 at a time, and `ENGINE_CONTAINERS_RUNNING_AT_LAUNCH: 0` both times.
- Read-back diffs:
  - `logs/worddiff_docker_godot_godot_harness.py.txt`
  - `logs/worddiff_tests_unit_test_playtest_one_reading.py.txt`
  - The files under `final/`, this note included, are read back in `logs/worddiff_final_files.txt`, added in the next commit.

## Not done

- **`description` decision.** Refusing `description` (above) breaks 165 wuxia scenarios once this is deployed. Who owns the fix, a wuxia card or a change to the allowed set, is the director's call.
- **Deployment.** The `aitelier-godot` restart is not done; it is the director's, with the owner's consent. The production sidecar still runs the base harness.
- **Engine proof of the assert path in the wuxia corpus.** Not measured. There are no `/` targets in the corpus to measure.
- **Scenario-level engine resolution** of the wuxia corpus: not measured this round, and it needs the whole gate.
- **Timeline errors still run the scenario.** A scenario whose timeline has an order error (or any pre-existing timeline error) is still run, as before; the spec error hard-fails the gate. Only unknown spec-level or scenario-level keys stop a scenario from running.
- **Y4b: a non-integer `at`.** The review's `185.5` still passes: the harness truncates it to frame 185 while a static rule reads 185.5. This round's criteria do not cover it, so it is not rejected.
