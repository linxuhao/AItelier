# harness.a-scenario-has-one-reading, rev 2: delivery note

- Branch `director/onereading-r2-20260924`, based on the r1 candidate `290d7908d389ce648ac2d1be4c68af489c4cc54a` (itself based on AItelier main `8b084c20`). Not rebased onto main `d9aa467e`.
- Code commit: `5fbe7c24f90a87c10fb244d31b310be1b596e538`. It changes `docker/godot/godot_harness.py`, `aitelier/tools/godot_playtest/impl.py`, `aitelier/tools/godot_playtest_scenario/impl.py`, `tests/unit/test_playtest_one_reading.py` and `tests/unit/test_godot_playtest_spec.py`. The commits after it add or change only `final/` and `logs/`.
- This round's logs are `logs/r2_*.txt`. Each records its command, its tree or harness sha1, and a bare exit code (`BARE_RC=`), with no pipe on the measured command. The r1 logs stay in `logs/` without the prefix; they are cited as `290d7908:logs/<name>`. Two r1 logs were replaced inside r1 and are only at `2076a291:logs/engine_path_cand.txt` (the `5183a2ed` harness on image `758e8115…`) and `2076a291:logs/corpus_impact.txt` (the first corpus run, 165 of 181).
- "Base" below is this round's base, the r1 candidate `290d7908` (harness sha1 `a3421523…`), unless a row names `8b084c20` (harness sha1 `0b937675…`, the r1 base). The candidate harness is sha1 `96d10e0f…`.
- The review this round answers: `~/.AItelier/director/reports/onereading-r1-review-20260924/review.md`, sha256 `d1d6f39b1299638cdaf7a0a38a12f31dc634bcd938caaea47e8a71dc7c38ca81`, written R below.
- No State DAG writes. Nothing ran in the `aitelier` container and nothing called the production `aitelier-godot`. The game repo was read only with `git archive` and `git show`.

## The production sidecar

`logs/r2_prod_container_inspect.txt` (read-only `docker inspect`, taken 2026-09-24T14:35:31Z, BARE_RC=0):

```
Name=/aitelier-godot Image=sha256:e9a6237c84c54863d5e388f2c1aecd729caeb3bc5ec49976d674929b51fad4b2 Created=2026-09-24T13:48:35.082473711Z StartedAt=2026-09-24T13:48:51.456322914Z Status=running
bind /home/linxuhao/.AItelier -> /home/linxuhao/.AItelier rw=false
bind /home/linxuhao/.AItelier/godot-control -> /var/lib/aitelier-godot rw=true
```

- No mount covers `/srv/godot_harness.py`, so the sidecar runs the harness baked into image `e9a6237c…`. This candidate reaches it only through an image rebuild and a recreate, which the director does.
- The r1 note's two sentences about production (lines 9 and 162 of `290d7908:final/delivery_notes_harness_one_reading.md`) had no inspect behind them and were wrong; this note replaces them.

## What changed (line numbers at `5fbe7c24`)

`docker/godot/godot_harness.py`:

1. **A keyed spec never reaches the smoke test.**
   - `playtest_project` (`:2150`) sends every truthy spec to `_playtest_spec`. The canned smoke test runs only for `spec=None` or `{}`.
   - `_playtest_spec` (`:1818-1830`) refuses a spec that is not a mapping, a spec with no `scenarios` key, and any header key of the wrong type. `scenarios` must be a non-empty list. Nothing runs, and each refusal names the key.
2. **One type per key** (`_SPEC_KEY_TYPES` `:1477`, `_SCENARIO_KEY_TYPES` `:1486`, `_key_type_errors` `:1497`).
   - spec: `scene` str; `frames` int, not bool; `scenarios` non-empty list; `actions` list of str; `surface` a mapping of str to a list of str.
   - scenario: `name` str; `timeline` list; `scene` str; `repeatability` bool; `description` str (the r1 ruling, now one row of the table).
   - A scenario that is not a mapping is refused (`:1852`).
   - `_SPEC_KEYS` and `_SCENARIO_KEYS` are the tables' key sets, so the allowed sets are unchanged.
3. **A timeline entry needs `at`** (`:1591`). Before, the order check and `max_at` read a missing `at` as 0, the rebuilt `actions:`/`clicks:`/`hovers:` entries ran at frame 0, and `press:`/`click:`/`assert:` entries kept no `at` and never ran.
4. **List-form assert items** (`_ASSERT_ITEM_KEYS` `:1505`, `_assert_errors` `:1508`, called at `:1627`). Keys are limited to `name, node, expr, mode, attr`, the ones `_eval_assert` reads. `mode` with `expr` is refused. An item that is not a mapping, and an `assert:` that is neither a mapping nor a list, are refused.
5. **One offset and one button per aim** (`_aim_errors` `:1533`, called at `:1627` for `click`, `clicks`, `hover`, `hovers`).
6. **The frame cap covers every entry** (`:1889`). Before, only asserts past 3000 were refused.
7. **Every unresolved path is a spec error.** The probe's `_is_path` (`:1111`) is `"/" in name`, so an absolute `/root/…` path is refused through `_refuse_path` (`:1115`) like a relative one, at `_point_of` (`:893`) and `_eval_assert` (`:1032`). The message now says `does not resolve in the scene tree`. `_resolve` (`:1092`) is unchanged from r1.

`aitelier/tools/godot_playtest/impl.py:_read_monolith` (`:98-105`): a `playtest_spec.yaml` with keys but no non-empty `scenarios` list, or one that is not a mapping, is a load error naming the file and its top-level keys. An empty file is still "no contract".

`aitelier/tools/godot_playtest_scenario/impl.py`: the `{scenarios: [...]}` inline wrapper's other keys go to the harness with the scenarios (`:90`, `:138`), and the tool returns the harness's `spec_errors` (`:177`).

`tests/unit/test_godot_playtest_spec.py`: `test_read_spec_no_scenarios_is_none` asserted that `scene: res://main.tscn` with no scenarios reads as "no contract". That is shape L1's route to the smoke test, so it is now `test_read_spec_keys_without_scenarios_is_an_error`; the empty-file case moved to `test_read_spec_empty_file_is_none`.

**The field that carries a refusal:** `spec_errors`, a list of strings, with `passed: false` and `summary` starting `Playtest HARD-failed: N spec violation(s) -- <first>`. The engine runs below show `errors: []` next to the refusals. A refused scenario key or type also leaves the scenario unrun (`ran: false`). A refused timeline entry is left out of the probe's timeline while the rest of the scenario runs, and the spec error fails the gate.

## Criteria

### a-path-that-does-not-resolve-never-falls-back-to-a-leaf

**Tests that see G1 and G2.** The probe is GDScript and this suite has no Godot, so the tests read `_PROBE_GD`:

- `test_every_tree_lookup_in_the_probe_is_one_of_these` lists every non-comment line of the probe that calls `find_child`, `find_children`, `get_node_or_null`, `get_node`, `has_node`, `get_children` or `get_child`, with the function it sits in. It must be exactly four lines: three in `_resolve` (absolute path, scene-relative path, bare name) and the state walk in `_walk`.
  - Why it sees G1: a leaf fallback has to look a node up by name or walk the tree, and the test lists every call of the seven Node methods above. G1's `_leaf_of` adds `find_child(name.get_file(), true, false)`, a fifth line. A fallback written with some other lookup API would pass this test; the path-branch test below and the engine probe are the other two guards.
- `test_the_path_branch_returns_the_path_lookup_and_nothing_else` pins the code lines of `_resolve`'s `"/" in name` branch. G1 rewrites its return.
- `test_any_name_with_a_slash_is_a_path_the_probe_refuses` pins `_is_path` to `return "/" in name` and checks both callers use it. G2 (`return false`) changes that line.

| mutant (applied to `5fbe7c24`) | family (17 files) | red tests | engine: does it fire? | logs |
|---|---|---|---|---|
| G1, the review's leaf fallback through `_leaf_of`, text from R/mutate.py | 2 failed, 287 passed, BARE_RC=1 | the two `_resolve` tests above | yes: the `X/PlanEditKind1` click reaches the leaf, `passed: True`, `spec_errors: []`, BARE_RC=0 | `logs/r2_mut_G1_leaf_fallback_via_helper.txt`, `logs/r2_engine_path_cand_G1.txt` |
| G2, `_is_path` returns false | 1 failed, 288 passed, BARE_RC=1 | `test_any_name_with_a_slash_is_a_path_the_probe_refuses` | yes: the unresolved paths of E08 and E14 come back as runtime `push_error` (`aim: node not found: /root/X/PlanEditKind1`), E03/E08b/E15 lose their spec errors, BARE_RC=1 | `logs/r2_mut_G2_no_name_is_a_path.txt`, `logs/r2_engine_battery_cand_G2.txt` |

**Engine.** One throwaway container at a time from `aitelier-godot:latest` = `sha256:e9a6237c…` (the production image), started with `--rm --init --network none -m 3g`, with its own `/tmp/ctl` lifecycle DB and lock paths and the harness under test mounted read-only over `/srv/godot_harness.py`. There was no port, no HTTP, and nothing shared with production. Every log shows `ENGINE_CONTAINERS_AT_LAUNCH(excluding prod): 0`. Recipe: `final/scripts/run_engine.sh`.

- `final/engine_path_probe/` (r1's probe, unchanged): scenario 1 clicks `X/PlanEditKind1` at frame 10 and asserts `Panel/PlanEditKind1.presses == 1` and `X/PlanEditKind1.presses == 1` at frame 20. Only the leaf exists. Scenario 2 uses the bare name.
- `final/engine_review_battery/` is R/engine/ copied byte for byte (drive.py sha1 in each log). `crit` is the BY3 spelling `X/PlanEditKind1 +0,20`. `battery` holds E03–E15.

| run | harness | BARE_RC | probe output | log |
|---|---|---|---|---|
| r1 probe | `8b084c20` | 0 | `passed: True`, `spec_errors: []`: the leaf was clicked through `X/…` and both asserts passed | `logs/r2_engine_path_8b084c20.txt` |
| r1 probe | `290d7908` | 1 | 2 spec errors, `… is a path and does not resolve under the current scene …`, `errors: []` | `logs/r2_engine_path_290d7908.txt` |
| r1 probe | candidate | 1 | `passed: False`, `errors: []`, spec errors `frame 10: aim target X/PlanEditKind1 is a path and does not resolve in the scene tree (spec: X/PlanEditKind1)` and `frame 20: assert target X/PlanEditKind1 …`. `Panel/PlanEditKind1.presses` observed 0; the bare-name scenario passed | `logs/r2_engine_path_cand.txt` |
| crit (BY3) | `8b084c20` | 0 | `passed: True`, the leaf was clicked | `logs/r2_engine_crit_8b084c20.txt` |
| crit (BY3) | candidate | 1 | 1 spec error, `frame 10: aim target X/PlanEditKind1 is a path and does not resolve in the scene tree (spec: X/PlanEditKind1 +0,20)`, `errors: []`; the bare-name control passed | `logs/r2_engine_crit_cand.txt` |
| battery | `290d7908` | 1 | E08 is a runtime error (`aim: node not found: /root/X/PlanEditKind1`) and E08b is only an advisory `node not found`; 3 spec errors (E03, E14, E15) | `logs/r2_engine_battery_290d7908.txt` |
| battery | candidate | 1 | 7 spec errors, `errors: []`: E03, **E08** `frame 10: aim target /root/X/PlanEditKind1 is a path and does not resolve in the scene tree (spec: /root/X/PlanEditKind1)`, **E08b** `frame 20: assert target /root/X/PlanEditKind1 …`, E10, E12, E14, E15. E05–E07 and E09 resolve by path and pass | `logs/r2_engine_battery_cand.txt` |

Both battery runs exit 1 because their spec holds refused shapes; the poles differ in where the E08 refusal lands (`errors` on the base, `spec_errors` on the candidate).

### an-unknown-key-at-any-level-is-an-error

- The review's L1–L3 and monolith shapes, copied from R/dispatch.py into `test_a_keyed_spec_is_read_as_a_spec_or_refused` and `test_the_monolith_reader_names_a_scenario_typo_and_a_scenarios_mapping`, go through the real `playtest_project` with `_copy_project`, `_inject_probe`, `_import_resources` and `_playtest_legacy` stubbed, and the probe replaced by a recorder.
- R/dispatch.py itself (copied into `final/review_scripts/dispatch.py`, sha1 `e6f3dc78…`):
  - base `290d7908`, `logs/r2_dispatch_inprocess_base.txt`, BARE_RC=0: L1, L2, L3 each `-> ['legacy'] passed=True spec_used=False spec_errors=None`. The monolith typo gives `spec=None` with `errors: []`, and the monolith mapping is returned as a spec with `errors: []`.
  - candidate, `logs/r2_dispatch_inprocess_cand.txt`, BARE_RC=0: L1, L2, L3 each `-> ['spec'] … spec_used=True`. The monolith gives `playtest_spec.yaml has no non-empty `scenarios` list (`scenarios` is absent; top-level keys: scenario, scene)` and `… (`scenarios` is dict; top-level keys: scenarios)`.
- K17/K18: the item's unknown key and `mode` with `expr` are refused (table below).
- P7: `test_p7_a_non_string_top_level_key_is_refused` puts the int key `1` at the spec's top level. The review's P7 mutant (D23 below) turns it red.
- Readers of the allowed keys: harness `:1828` scene, `:1829` frames, `:1830` scenarios, `:1856` name, `:1857` timeline, `:1909` scenario scene; `description` is read by the type check (`:1865`). In wuxia `43aff480`, read with `git show`: `tests/test_playtest_contract_smoke.py:1226` reads `actions`, `tests/test_coop_ui_isolation.py:117` reads `surface`, and `tools/godot_gate.py:149,162` read `repeatability` as `is True`.

### every-shape-the-r1-review-listed-has-one-reading

**Row by row against R's "Two-reading attempts" table.**
- "8b084c20" quotes the review's base column. "290d7908" and "candidate" are this round's in-process runs of R/battery.py (`logs/r2_battery_inprocess.txt`, BARE_RC=0; the base and candidate harness in one run) and R/dispatch.py, plus the engine runs above.
- "Test" is the test in `tests/unit/test_playtest_one_reading.py` that holds the shape. Its bare RC on each pole is in `logs/r2_new_tests_on_base_290d7908.txt` and `logs/r2_new_tests_cand.txt`: one pytest process per test id, and each id's `BARE_RC=` is on its own `PER_TEST` line.
- "Mutant" is the deletion mutant that turns that test red (table in the last criterion).

| id | shape | 8b084c20 (R) | 290d7908 | candidate: reading, and the refusal text | test | mutant |
|---|---|---|---|---|---|---|
| K1 | BY1: geometry under scenario `reviewer_notes:` | runs green | refused | refused, `scenario 's' has unknown key(s) reviewer_notes - allowed: …`, 0 engine calls | `test_by1_…` | R1 |
| K2 | top-level `reviewer_notes` | runs green | refused | refused, `spec has unknown top-level key(s) reviewer_notes - allowed: …` | `test_an_unknown_top_level_spec_key_…` | R2 |
| K3 | `reviewer_notes` injected with `<<:` | runs green | refused | refused, same text as K1 | covered by K1's check | R1 |
| K4 | duplicate `timeline:` | loader refuses | same | same (`aitelier/strict_yaml.py`, unchanged) | - | - |
| K5 / K6 | `description: \|`, `!!str 5` | runs | runs | runs; a str has one reading | `test_a_plain_string_description_…` | - |
| K7 / K8 | `description` as `!!binary` / a mapping | runs | refused | refused, `scenario 's' has key description of type bytes` / `dict` `- it must be a plain string. The scenario was not run.` | `test_a_description_that_is_not_a_string_…` (6 cases) | D13 |
| K9 | `description` at spec level | runs | refused | refused, unknown top-level key | `test_an_unknown_top_level_…` | R2 |
| K10 | `repeatability:` as a mapping of the BY1 asserts | runs green | runs green | **refused**, `scenario 's' has key repeatability of type dict - it must be true or false. The scenario was not run.` | `test_k10_…` | D12 |
| K11 | `repeatability: 'yes'` | runs | runs | **refused**, `… key repeatability of type str - it must be true or false. …`; `true` and `false` both run (`test_repeatability_true_and_false_both_run`) | `test_k11_…` | D12 |
| K12 | `name:` as a mapping of asserts | runs | runs | **refused**, `scenario "{'at': 1, 'assert': {'A.b': 1}}" has key name of type dict - it must be a string. The scenario was not run.` | `test_k12_…` | D09 |
| K13 | spec `actions:` as a mapping of asserts | runs | runs | **refused**, `spec has key actions of type dict - it must be a list of strings. No scenario was run.` | `test_k13_…` | D07 |
| K14 | spec `surface:` carrying a `timeline` | runs | runs | **refused**, `spec has key surface of type dict - it must be a mapping of node name to a list of strings. No scenario was run.` | `test_k14_…` | D08 |
| K15 | scenario-level `frames` | runs | refused | refused, unknown key | covered by K1's check | R1 |
| K16 | unknown key in a timeline entry | refused | refused | refused (`_TIMELINE_KEYS`, unchanged) | existing | - |
| K17 | list-form assert item with `precondition` | runs | runs | **refused**, `scenario 's': timeline entry 0 (at: 185): assert item 0 has unknown key(s) precondition - allowed: attr, expr, mode, name, node` | `test_k17_…` | D18 |
| K18 | list-form item with `mode` and `expr` | runs | runs | **refused**, `… assert item 0 has both `mode` and `expr`; a `mode` assert compares `attr` with frame 0 and never reads `expr`. Write two items.` | `test_k18_…` | D19 |
| K19 | int key `1` in a scenario | runs | refused | refused, `scenario 's' has unknown key(s) 1 - …`; at spec level: `test_p7_…` | `test_p7_…` | D23 |
| L1 | `scenario:` typo plus an unknown key | legacy, passed | legacy, passed | **refused**, `spec has no `scenarios` key (top-level keys: scenario, scene). No scenario was run.` and `spec has unknown top-level key(s) scenario - …`; `spec_used: true`, `passed: false`, smoke not called | `test_a_keyed_spec_…[L1-scenario-typo]` | D01, D02 |
| L2 | `scenarios` as a mapping | legacy, passed | legacy, passed | **refused**, `spec has key scenarios of type dict - it must be a non-empty list. No scenario was run.`; the monolith reader: `playtest_spec.yaml has no non-empty `scenarios` list (`scenarios` is dict; top-level keys: scenarios)` | `…[L2-scenarios-mapping]`, `test_the_monolith_reader_…` | D01, D06, D25 |
| L3 | `scenarios: []` plus `reviewer_notes` | legacy, passed | legacy, passed | **refused**, `spec has key scenarios of type list - it must be a non-empty list. …` and the unknown key | `…[L3-empty-scenarios-plus-notes]` | D01, D06 |
| A1 | BY2b | green | refused | refused, `timeline entry 2 (at: 185) is written after entry 1 (at: 195); …` | `test_by2b_…` (2) | R3 |
| A2 | Y4b `185.5` | ran at 185 | refused | refused, `timeline entry 1 has a non-integer `at`: 185.5. …` | `test_y4b_…` (2) | R6 |
| A3 / A4 / A6 | `'185'`, `1.85e2`, `true` | refused | refused | refused, non-numeric `at` (unchanged) | `test_a_bool_or_string_frame_…` | - |
| A5 / A18 | `1.85e+2`, `185.0` | frame 185 | frame 185 | frame 185, one reading | `test_whole_frames_still_run_as_written` | - |
| A7 | `-1` | refused | refused | refused (unchanged) | - | - |
| A8 / A9 | `.nan`, `.inf` | HTTP 500 | refused | refused, `non-integer `at`: nan` / `inf` | the check A2's tests hold | R6 |
| A10 | click at 999999, past the cap | accepted, never fires | accepted, never fires | **refused**, `scenario 's': timeline entry(ies) scheduled at frame(s) 999999, past the 3000-frame cap - they would never run. …` | `test_a10_…` | D22 |
| A11 | equal `at` twice | legal | legal | legal | `test_the_same_frame_written_twice_is_legal` | - |
| A12 / E10 | assert entry with no `at` | never evaluated, green | never evaluated, green (engine) | **refused**, `timeline entry 0 has no `at`. Every entry runs on the frame its `at` names; write it (`at: 0` is the first frame).`; in the engine `logs/r2_engine_battery_cand.txt` carries it for `'E10_atless_assert_first'` | `test_a12_…`, `test_e10_…` | D15 |
| A13 | no `at`, after at 185 | never runs | refused as "(at: 0)" | refused, `timeline entry 1 has no `at`. …`; no message says `(at: 0)` | `test_a13_…` | D15 |
| A14 | `press:` with no `at` | never fires | never fires | **refused**, same text as A12 | `test_a14_a15_…[a14-press]` | D15 |
| A15 | `actions:` with no `at` | fires at f0 | fires at f0 | **refused**, the same text as A14, and nothing is scheduled for it | `test_a14_a15_…[a15-actions]` | D15 |
| A16 | `actions:` f200 then `press` f190 | runs | refused | refused (order) | covered by R3's tests | R3 |
| A17 | one entry: `assert` above `clicks` | click, then assert on the pre-click state | same | same, **left alone**: reason below | - | - |
| A19 | `<<: *f195` overriding to 185 | runs | refused | refused (order) | covered by R3's tests | R3 |
| BY3 / crit | click `X/PlanEditKind1 +0,20` | leaf clicked | spec error | spec error, engine `logs/r2_engine_crit_cand.txt` | G1/G2 tests | G1, G2 |
| E03 / E14 / E15 | assert, hover, delta assert through `X/…` | against the leaf | spec error | spec error, `logs/r2_engine_battery_cand.txt` | `test_a_path_the_probe_refused_comes_back_as_a_spec_error`, G1/G2 tests | R4, G2 |
| E05 / E06 / E07 | `Panel/PlanEditKind1/`, `./…`, `../Main/…` | by path | by path | by path (engine, passed) | - | - |
| E08 / E08b | `/root/X/PlanEditKind1` click / assert | runtime / advisory | runtime / advisory | **spec error**, `frame 10: aim target /root/X/PlanEditKind1 is a path and does not resolve in the scene tree (spec: /root/X/PlanEditKind1)` / `frame 20: assert target /root/X/PlanEditKind1 …` | `test_any_name_with_a_slash_…` | G2 |
| E09 | absolute existing path | clicked | clicked | clicked (engine, passed) | - | - |
| E12 | `Panel/PlanEditKind1 +0,500 +0,0` | last wins | last wins | **refused**, `timeline entry 0 (at: 10): clicks 'Panel/PlanEditKind1 +0,500 +0,0' has 2 offsets (+0,500, +0,0); an aim takes one offset`; two offsets on `click`/`hovers` and two buttons are refused the same way | `test_e12_…`, `test_every_aim_takes_one_offset_…` (3) | D20, D21 |
| E13 | bare `Dup`, two nodes | first in tree order | same | same, **left alone**: reason below | - | - |
| routes | `/script`, `/x11_input_smoke` | no scenario | same | same (they read no scenario) | - | - |
| routes | `godot_playtest_scenario` inline wrapper keys | dropped | dropped | **reach the harness**: `scene: res://probe.tscn` beside `scenarios:` is the scene handed to the engine; `reviewer_notes:` there comes back in the tool's `spec_errors` as `spec has unknown top-level key(s) reviewer_notes - …` and nothing runs | `test_inline_wrapper_keys_reach_the_harness` | D26 |

Shapes left alone, for the reviewer to rule on:

- **A17** (an `assert` written above `clicks` in one entry). Mapping keys carry no order in YAML, and the harness has one order for an entry whatever the key order: the rebuilt `clicks:`/`actions:`/`hovers:` entries come first, then the rest of the entry in `_apply_entry` order (hover, click, press, release, assert). An assert in the same entry as an input sees the state before that input is handled. The engine shows this on both poles (E11: the same-frame `presses == 1` fails with observed 0, and the f20 assert passes). The shapes it could be read as are one: no key order changes what runs. Refusing input and assert in one entry is possible; this round did not measure how many corpus entries hold a `press:` or `actions:` next to an `assert:` (R counted aim-plus-assert entries: 0).
- **E13** (a bare name held by two nodes). The criterion text says a bare name's behaviour does not change ("不含 / 的裸名行为不变"). A bare name resolves through `find_child(name)`, the first match in tree order, on every pole.

Extra shapes of the same kinds, added with their own tests and mutants: a spec that is not a mapping (D03), spec `scene`/`frames` and scenario `scene`/`timeline` of the wrong type (D04, D05, D10, D11), a scenario that is not a mapping (D14), `assert:` as a string or a list of strings (D16, D17), and a monolith file that is not a mapping (D24).

### the-corpus-impact-is-counted

- Corpus: wuxia master `43aff480877806bbc483a834a6c81af12098912e` (`git rev-parse master` gave that sha at run time), extracted with `git -C ~/.AItelier/projects/wuxia-commercial-batch-next archive 43aff480… playtest tools | tar -x -C <tools>/corpus`. Loaded with the candidate's `read_spec` (`errors=0`), then run through each harness's real `_playtest_spec` with the engine stubbed. Script: `final/corpus_impact.py`, wrapper `final/scripts/run_corpus.sh`.
- Against `8b084c20` (all r1 and r2 checks): 2 of the 181 checked scenarios gain a spec error, both for an `at` that decreases in file order: `consequence_work_income_inline` (entry 10 at 220 after entry 9 at 230) and `shelter_plan_kept_battle_support_is_attributed` (entry 32 at 435 after entry 31 at 480). 0 of 181 are not run. `logs/r2_corpus_impact_43aff480_vs_8b084c20.txt`, BARE_RC=0.
- Against `290d7908` (only this round's checks): 0 of the 181 checked scenarios gain a spec error, and 0 of 181 are not run. `logs/r2_corpus_impact_43aff480_vs_290d7908.txt`, BARE_RC=0.
- Spec top-level keys in the corpus: `actions, scene, surface` (plus `scenarios` from the reader).
- Targets containing `/`: 0 among the 4776 targets scanned from the raw timelines (4327 assert targets, 444 `clicks`, 5 `hovers`, 0 `click`/`hover`), in 0 scenarios. Engine resolution: 本轮未量 (not measured this round).
- Not covered: the launcher-expanded contract (`tools/godot_gate.py:with_repeatability_runs`, 185 scenarios) was not re-run this round. R ran it at `43aff480` against the r1 checks: 2 new errors, 0 of 185 not run.

### the-suite-stays-green-and-the-note-carries-every-number

SUITE_TABLE_PLACEHOLDER

### the-timeline-runs-in-the-order-it-is-written

Re-measured on the candidate tree, one pytest process per test id (`logs/r2_new_tests_cand.txt`, `PER_TEST` lines; base in `logs/r2_new_tests_on_base_290d7908.txt`):

- BY2b: `test_by2b_a_frame_written_after_a_later_frame_is_refused` and `test_by2b_through_playtest_spec_is_a_spec_error`.
- Equal frames legal: `test_the_same_frame_written_twice_is_legal`.
- Non-integer `at`: `test_y4b_a_fractional_frame_is_refused`, `test_y4b_through_playtest_spec_is_a_spec_error`, `test_whole_frames_still_run_as_written`, `test_a_bool_or_string_frame_is_still_refused` (3 cases).
- Deletion mutants on `5fbe7c24`: R3 (order check deleted, ignition 154, BARE_RC=1, both BY2b tests and `test_a_drop_below_an_earlier_maximum_names_the_maximum` red) and R6 (non-integer check deleted, ignition 156, BARE_RC=1, both Y4b tests red).

**The new test file on both poles**, one pytest process per test id, in one throwaway container per pole (`final/scripts/per_test_rc.sh`):

| pole | tree | ids | per-id BARE_RC | whole file | log |
|---|---|---|---|---|---|
| base | `290d7908` plus this round's `tests/unit/test_playtest_one_reading.py` (sha1 `d3b38b6b…`), nothing else changed | 65 | 32 ids 0, 33 ids 1 | 33 failed, 32 passed, WHOLE_FILE_BARE_RC=1 | `logs/r2_new_tests_on_base_290d7908.txt` |
| candidate | `5fbe7c24` (the dirty files in the log header are `final/` only) | 65 | 65 ids 0 | 65 passed, WHOLE_FILE_BARE_RC=0 | `logs/r2_new_tests_cand.txt` |

The 32 ids that pass on the base are r1's tests (BY1, BY2b, Y4b, `description`, equal frames, the path tests r1 already satisfied) plus the controls that must hold on both poles (`test_repeatability_true_and_false_both_run`, `test_list_form_items_the_probe_reads_still_run`, `test_one_offset_and_one_button_still_run`, the smoke test with no spec, P7's non-string key, which the r1 check already caught). Every shape test from the table above is among the 33 that fail on the base.

## Mutants

Each mutant deletes one check in a detached worktree of `5fbe7c24`, or puts back the behaviour it replaced. The Python mutants write one ignition line per execution of the mutated line. Each mutant runs the 17-file harness family in a throwaway container. Driver: `final/scripts/mutate.py`, summary `logs/r2_mut_driver.txt` (DRIVER_BARE_RC=0), one log per mutant `logs/r2_mut_<tag>.txt` with its diff. The worktree was clean after every mutant.

All 33 mutants: BARE_RC=1, ignition > 0 for every Python mutant (the GDScript pair G1/G2 fire in the engine runs above), worktree clean afterwards. Red tests are in `tests/unit/test_playtest_one_reading.py` unless a file is named.

| mutant | ignition | BARE_RC | red tests | log |
|---|---|---|---|---|
| D01_keyed_spec_without_list_goes_to_smoke | 8 | 1 | `test_a_keyed_spec_is_read_as_a_spec_or_refused[L1-scenario-typo]`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[L2-scenarios-mapping]`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[L3-empty-scenarios-plus-notes]`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[spec-not-a-mapping]` | `logs/r2_mut_D01_keyed_spec_without_list_goes_to_smoke.txt` |
| D02_no_scenarios_key_check_deleted | 68 | 1 | `test_a_keyed_spec_is_read_as_a_spec_or_refused[L1-scenario-typo]` | `logs/r2_mut_D02_no_scenarios_key_check_deleted.txt` |
| D03_spec_mapping_check_deleted | 69 | 1 | `test_a_keyed_spec_is_read_as_a_spec_or_refused[spec-not-a-mapping]` | `logs/r2_mut_D03_spec_mapping_check_deleted.txt` |
| D04_spec_scene_type_deleted | 29 | 1 | `test_the_other_keys_have_one_type_too[spec-scene]` | `logs/r2_mut_D04_spec_scene_type_deleted.txt` |
| D05_spec_frames_type_deleted | 3 | 1 | `test_the_other_keys_have_one_type_too[spec-frames-str]`<br>`test_the_other_keys_have_one_type_too[spec-frames-bool]` | `logs/r2_mut_D05_spec_frames_type_deleted.txt` |
| D06_spec_scenarios_type_deleted | 67 | 1 | `test_a_keyed_spec_is_read_as_a_spec_or_refused[L2-scenarios-mapping]`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[L3-empty-scenarios-plus-notes]` | `logs/r2_mut_D06_spec_scenarios_type_deleted.txt` |
| D07_spec_actions_type_deleted | 2 | 1 | `test_k13_spec_actions_as_a_mapping_is_refused` | `logs/r2_mut_D07_spec_actions_type_deleted.txt` |
| D08_spec_surface_type_deleted | 2 | 1 | `test_k14_spec_surface_carrying_a_timeline_is_refused` | `logs/r2_mut_D08_spec_surface_type_deleted.txt` |
| D09_scenario_name_type_deleted | 73 | 1 | `test_k12_name_as_a_mapping_is_refused` | `logs/r2_mut_D09_scenario_name_type_deleted.txt` |
| D10_scenario_timeline_type_deleted | 73 | 1 | `test_the_other_keys_have_one_type_too[scenario-timeline]` | `logs/r2_mut_D10_scenario_timeline_type_deleted.txt` |
| D11_scenario_scene_type_deleted | 11 | 1 | `test_the_other_keys_have_one_type_too[scenario-scene]` | `logs/r2_mut_D11_scenario_scene_type_deleted.txt` |
| D12_repeatability_type_deleted | 5 | 1 | `test_k10_repeatability_as_a_mapping_is_refused`<br>`test_k11_repeatability_yes_is_refused` | `logs/r2_mut_D12_repeatability_type_deleted.txt` |
| D13_description_type_deleted | 7 | 1 | `test_a_description_that_is_not_a_string_is_refused[list]`<br>`test_a_description_that_is_not_a_string_is_refused[mapping]`<br>`test_a_description_that_is_not_a_string_is_refused[int]`<br>`test_a_description_that_is_not_a_string_is_refused[float]`<br>`test_a_description_that_is_not_a_string_is_refused[bool]`<br>`test_a_description_that_is_not_a_string_is_refused[null]` | `logs/r2_mut_D13_description_type_deleted.txt` |
| D14_scenario_mapping_check_deleted | 74 | 1 | `test_a_scenario_that_is_not_a_mapping_is_refused` | `logs/r2_mut_D14_scenario_mapping_check_deleted.txt` |
| D15_atless_entry_defaults_to_0_again | 164 | 1 | `test_a12_an_assert_entry_with_no_at_is_refused`<br>`test_e10_the_engine_shape_is_refused_before_the_engine`<br>`test_a13_an_entry_with_no_at_is_not_called_frame_0`<br>`test_a14_a15_press_and_actions_with_no_at_are_refused_alike[a14-press]`<br>`test_a14_a15_press_and_actions_with_no_at_are_refused_alike[a15-actions]` | `logs/r2_mut_D15_atless_entry_defaults_to_0_again.txt` |
| D16_assert_shape_check_deleted | 1 | 1 | `test_an_assert_the_probe_cannot_read_is_refused[assert-string]` | `logs/r2_mut_D16_assert_shape_check_deleted.txt` |
| D17_assert_item_mapping_check_deleted | 1 | 1 | `test_an_assert_the_probe_cannot_read_is_refused[assert-list-of-strings]` | `logs/r2_mut_D17_assert_item_mapping_check_deleted.txt` |
| D18_assert_item_unknown_key_check_deleted | 31 | 1 | `test_k17_an_unknown_key_in_a_list_form_assert_item_is_refused` | `logs/r2_mut_D18_assert_item_unknown_key_check_deleted.txt` |
| D19_mode_with_expr_check_deleted | 31 | 1 | `test_k18_mode_and_expr_in_one_assert_item_is_refused` | `logs/r2_mut_D19_mode_with_expr_check_deleted.txt` |
| D20_two_offsets_check_deleted | 47 | 1 | `test_e12_a_click_with_two_offsets_is_refused`<br>`test_every_aim_takes_one_offset_and_one_button[click]`<br>`test_every_aim_takes_one_offset_and_one_button[hovers]` | `logs/r2_mut_D20_two_offsets_check_deleted.txt` |
| D21_two_buttons_check_deleted | 47 | 1 | `test_every_aim_takes_one_offset_and_one_button[two-buttons]` | `logs/r2_mut_D21_two_buttons_check_deleted.txt` |
| D22_cap_check_covers_asserts_only_again | 2 | 1 | `test_a10_a_click_past_the_frame_cap_is_refused` | `logs/r2_mut_D22_cap_check_covers_asserts_only_again.txt` |
| D23_P7_top_level_check_ignores_non_string_keys | 108 | 1 | `test_p7_a_non_string_top_level_key_is_refused` | `logs/r2_mut_D23_P7_top_level_check_ignores_non_string_keys.txt` |
| D24_monolith_mapping_check_deleted | 1 | 1 | `test_the_monolith_reader_names_a_file_that_is_not_a_mapping` | `logs/r2_mut_D24_monolith_mapping_check_deleted.txt` |
| D25_monolith_scenarios_check_deleted | 6 | 1 | `test_the_monolith_reader_names_a_scenario_typo_and_a_scenarios_mapping`<br>`test_godot_playtest_spec.py::test_read_spec_keys_without_scenarios_is_an_error` | `logs/r2_mut_D25_monolith_scenarios_check_deleted.txt` |
| D26_inline_wrapper_keys_dropped_again | 3 | 1 | `test_inline_wrapper_keys_reach_the_harness` | `logs/r2_mut_D26_inline_wrapper_keys_dropped_again.txt` |
| R1_scenario_key_check_deleted | 74 | 1 | `test_by1_reviewer_notes_in_a_scenario_is_refused_and_the_scenario_does_not_run` | `logs/r2_mut_R1_scenario_key_check_deleted.txt` |
| R2_spec_key_check_deleted | 69 | 1 | `test_an_unknown_top_level_spec_key_is_refused_and_nothing_runs`<br>`test_p7_a_non_string_top_level_key_is_refused`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[L1-scenario-typo]`<br>`test_a_keyed_spec_is_read_as_a_spec_or_refused[L3-empty-scenarios-plus-notes]`<br>`test_inline_wrapper_keys_reach_the_harness` | `logs/r2_mut_R2_spec_key_check_deleted.txt` |
| R3_order_check_deleted | 154 | 1 | `test_by2b_a_frame_written_after_a_later_frame_is_refused`<br>`test_by2b_through_playtest_spec_is_a_spec_error`<br>`test_a_drop_below_an_earlier_maximum_names_the_maximum` | `logs/r2_mut_R3_order_check_deleted.txt` |
| R4_probe_spec_errors_not_lifted | 53 | 1 | `test_a_path_the_probe_refused_comes_back_as_a_spec_error` | `logs/r2_mut_R4_probe_spec_errors_not_lifted.txt` |
| R6_non_integer_at_check_deleted | 156 | 1 | `test_y4b_a_fractional_frame_is_refused`<br>`test_y4b_through_playtest_spec_is_a_spec_error` | `logs/r2_mut_R6_non_integer_at_check_deleted.txt` |
| G1_leaf_fallback_via_helper | engine (see the path criterion) | 1 | `test_every_tree_lookup_in_the_probe_is_one_of_these`<br>`test_the_path_branch_returns_the_path_lookup_and_nothing_else` | `logs/r2_mut_G1_leaf_fallback_via_helper.txt` |
| G2_no_name_is_a_path | engine (see the path criterion) | 1 | `test_any_name_with_a_slash_is_a_path_the_probe_refuses` | `logs/r2_mut_G2_no_name_is_a_path.txt` |

## Process

- Throwaway containers: every launch waited until fewer than 4 non-long-running containers ran server-wide (`final/scripts/runsuite.sh`, `final/scripts/run_engine.sh`); each log records the count and names at launch. At most one engine container of mine ran at a time.
- Word-diff read-backs of every changed non-log file against `290d7908`: `logs/r2_worddiff_*.txt`.
