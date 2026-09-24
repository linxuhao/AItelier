# Delivery notes: a gate that could not run is not a failing test (r2)

State DAG project `aitelier`, node
`harness.a-gate-that-could-not-run-is-not-a-failing-test`, revision 2, external
attempt `attempt-b102d5ed043b4b0185d6ba7ca0693242`. Branch
`director/gatenotrun-r2-20260924`, base `658ee8df35e45f4e42bc2f9a006225490f377e96`.
This worker made no State DAG writes.

All logs are in `logs/gate_not_run/`. Every number below sits beside the log
it comes from. Each log's own header names its tree: `tree: … head: …` for the
evidence runs, `git status` lines for the pytest runs. Read-back diffs:
`logs/gate_not_run/worddiff_<path, / as __>.txt`, one per changed file that
is not a log, each headed by the command that produced it.

## Commits

| commit | what |
|---|---|
| `8dcd2624` | admission relay, three outcomes, identities from the ticket's report dir, re-acquire edge, tests, evidence script |
| `59f3252e` | read only the report whose manifest names the gate's own repository (the real wuxia gate left 3 manifests under one ticket) |
| `6b5949f1` | keep the literal `scrubbed_env()` call; keep the tool.yaml sentences the N9 witness reads; protocol doc r2 section |
| `d41c0a4b` | one comment word in `configs/coding_impl.yaml` |
| last | this note, logs, word-diffs |

The mutation and evidence runs used tree `59f3252e`. After it, the code changed
in one place only: `_run_node_cmd` builds the same environment in two
statements, the literal `env_scrub.scrubbed_env()` and then `update` with the
overrides. `scrubbed_env(**overrides)` itself applies overrides after the filter
(`core/env_scrub.py:34-44`). The rest is prose in tool.yaml, the protocol doc
and one YAML comment. The final whole suite ran on `d41c0a4b`.

## What changed

- **`aitelier/gate_admission.py` (new): the admission relay.** `_run_repo_gate`
  starts one relay per gate run on 127.0.0.1 and hands the gate
  `GODOT_BUILDER_URL=<relay>`. The relay forwards to the real builder
  (`$GODOT_BUILDER_URL`, default `http://godot-builder:8080`).
  - On the render routes (`/playtest`, `/script`, `/x11_input_smoke`) it adds
    `render_wait_timeout_sec` if the gate sent none. The value is
    `AITELIER_REPO_GATE_RENDER_WAIT_SECONDS`, default 1500, which is under the
    game gate's own 1800 s client timeout. So a render request waits in the
    DEPLOYED harness render queue (`acquire_render_owner_waiting`) instead of
    being refused.
  - It records the integer HTTP status of every request, plus the owner
    fields of a 409 payload. Each record is one of: `answered`,
    `not_admitted`, `engine_error`, `unreachable`, `abandoned`.
- **`aitelier/tools/run_tests/impl.py`.**
  - Each gate run gets a ticket `rt-<UTC>-<8 hex>` and its own
    `GATE_REPORT_DIR=$AITELIER_HOME/gate-reports/<ticket>`, with
    `admission.json`.
  - `_repo_gate_outcome` has one new rule. A gate that exits neither 0 nor 1,
    while the LAST engine request was `not_admitted`, `unreachable`,
    `abandoned` or `undelivered`, is `unmeasured`. Exit 1 is always a
    measured red.
  - Failure identities are read from the gate's per-stage findings in the
    ticket dir (`_report_dir_failure_cases`), and only from the manifest that
    names the gate's repository.
- **`configs/coding_impl.yaml`.** `test_gate_absent` now has one edge:
  `test_gate_absent -> test` (`max_loop: 100`). It had none (`to: null`).
- **`core/gate_deferral.py`.**
  - The run is held for one wait per silent report. The report is keyed by
    path + mtime.
  - When the wait runs out, `observe_run` returns `due`, and the scheduler
    logs `gate_deferral_reacquire` and advances. That re-runs `test` alone.
  - `hold_blocks_advance(…, sf=)` notes the latest report first, so a
    re-acquire that came back silent starts a new wait.
  - The wall-clock ceiling and the absence sentence are unchanged.
- **Prose updated to match the code:** `aitelier/tools/run_tests/tool.yaml`
  and `docs/repo-gate-unmeasured-protocol.md`. The doc gets a third source of
  `unmeasured`, the re-acquire edge, an r2 section with the report fields of
  the three outcomes, and the mutation table.

### Three outcomes, and the field that carries each (`test_report.json`)

| outcome | fields |
|---|---|
| passed | `passed: true`, `repo_gate.measured: measured_pass` |
| ran and red | `passed: false`, `repo_gate.measured: measured_fail`, `release_evidence: known_failure`, per-case `new_failures` |
| not run | `passed: false`, `repo_gate.measured: unmeasured`, `repo_gate_absent: true`, `repo_gate_unmeasured: true`, `repo_gate_admission` = `repo_gate.admission.state` (`not_admitted` / `unreachable` / `abandoned` / `undelivered`), `evidence_state: not_run` (when nothing else in the report failed), `release_evidence: unresolved` |

"Not run" is never a pass: `passed` stays false, and release routing reads it
as `unresolved`, the same shape as `infrastructure_unavailable`. It never goes
back to implement, because the only edge out of `test_gate_absent` is `test`.

## The busy engine used for criteria 2 and 4

I used no engine container. `tests/gate_fixture.py:HarnessRig` loads the REAL
`docker/godot/godot_harness.py` of the tree under test, with a temp lifecycle
DB, and serves it over real HTTP on 127.0.0.1. Only the Godot render call is
stubbed.

The lock is taken by a real owner (`op-holder`) through the harness's own
acquire path, and the rig holds it until `let_go()`. "Owner lost" is the
harness's own reconciliation state.

So the 409 payloads, the queue wait and the `render_owner_waited_for_owner_ids`
field all come from the same handler and lifecycle code the `aitelier-godot`
container runs. The repository gate is a script with the game gate's transport
and exit-code contract (exit 2 on an unreachable engine), run by the real
`run_tests`.

I did not call the live `aitelier-godot` container and did not touch its lock.

## Criterion `coding-impl-stops-using-409-as-control-flow`

**The grep** (`logs/gate_not_run/c1_grep.txt`):
- Candidate: `grep -n "gate_queue\|ticket\|/queue" aitelier/tools/run_tests/impl.py`
  gives 14 hits, BARE_RC=0 (lines 686-720 are the ticket and relay; the rest
  are the ticket report reader). The grep was run at `d41c0a4b`.
- Base `658ee8df`: 0 hits, BARE_RC=1.

**Where the queue semantics live:**
- `aitelier/gate_admission.py:172-181` adds the wait to render requests.
- `impl.py:679-723` routes the gate through the relay and records the
  summary, and `:853-857` is the outcome rule.
- The queue itself is the deployed harness's
  `docker/godot/godot_harness.py:258` (`acquire_render_owner_waiting`) and
  `:2919` (reads `render_wait_timeout_sec`).
- There is no client retry loop and the gate is not narrowed. Every stage the
  gate runs still runs.

**Integration test:**
`tests/unit/test_repo_gate_admission.py::test_two_concurrent_repo_gates_are_each_admitted_by_the_queue`.
- Gate A renders for 1 s. Gate B arrives while A holds the lock.
- Both get HTTP 200 (`answered`), and neither sees a 409 (`409 not in statuses`).
- B's `queued_behind` equals A's owner id, and B's `render_owner_wait_sec > 0.3`.
- The harness rendered `gate-a` then `gate-b`.
- It passes in `logs/gate_not_run/new_tests_cand.txt` (19 passed, BARE_RC=0)
  and in `logs/gate_not_run/targeted_control_cand.txt` (204 passed, BARE_RC=0).
  That control run covers 12 files, the render-queue suite
  `test_harness_render_queue.py` among them.

**Real game gate through the relay:** in
`logs/gate_not_run/ev_wuxia_copy-py_cand_none.txt` the admission record is
`/compile` 200 answered, then `/playtest` 200 answered with
`render_wait_timeout_sec: 1500.0` injected (that ticket's `admission.json`:
`logs/gate_not_run/ev_wuxia_copy-py_cand_none_admission.txt`).

**Mutation:** removing the injection turns the concurrent test red
(`logs/gate_not_run/mut_M4_wait_injection.txt`, ignition 17, 3 red).

## Criterion `contention-is-a-distinct-outcome`

The evidence script is `final/scripts/gate_not_run_evidence.py`, 4 scenarios × 2
trees. Each log's own `BARE_RC=0` is the script's exit.
`repo_gate.returncode` is the gate's own bare exit status (the Popen
returncode). The distinction is made on the integer HTTP status and the exit
code, never on output text.

| scenario | tree | gate rc | measured | admission (last request) | repo_gate_absent | evidence_state | passed | release | identity error | log |
|---|---|---|---|---|---|---|---|---|---|---|
| pole 1, live owner holds the lock | cand | 2 | unmeasured | not_admitted, /script 409, owner_kind active, needs_reconciliation false, wait 0.5 injected | true | not_run | false | unresolved | none | `logs/gate_not_run/ev_pole1-live_cand.txt` |
| pole 1, live owner | base | 2 | measured_fail | (no relay) | false | – | false | known_failure | "did not emit per-case identities" | `logs/gate_not_run/ev_pole1-live_base.txt` |
| pole 1, owner lost | cand | 2 | unmeasured | not_admitted, /script 409, owner_kind owner_lost, needs_reconciliation true | true | not_run | false | unresolved | none | `logs/gate_not_run/ev_pole1-lost_cand.txt` |
| pole 1, owner lost | base | 2 | measured_fail | – | false | – | false | known_failure | "did not emit per-case identities" | `logs/gate_not_run/ev_pole1-lost_base.txt` |
| pole 2, ran and red | cand | 1 | measured_fail | answered, /script 200 | false | – | false | known_failure | none, 3 identities | `logs/gate_not_run/ev_pole2_cand.txt` |
| pole 2, ran and red | base | 1 | measured_fail | – | false | – | false | known_failure | "did not emit per-case identities", 1 entry | `logs/gate_not_run/ev_pole2_base.txt` |

In pole 1 with a live owner, the harness rendered only `op-holder`: the gate
was never rendered, and the report does not say it ran.

**Tests:**
- `test_pole_1_a_gate_the_engine_did_not_admit_is_not_run[live_render|owner_lost]`
- `test_pole_2_a_gate_that_ran_and_went_red_is_a_code_red`
- `test_an_unreachable_engine_is_not_run`
- `test_an_answered_gate_that_exits_nonzero_stays_a_measured_failure[2|3|124]`
  (a contract error at exit 2 stays red)
- `test_exit_1_is_a_red_whatever_the_relay_saw`

The existing `test_run_tests_unmeasured_declaration.py::test_no_exit_code_alone_is_ever_unmeasured`
(9 exit codes) still passes.

**Mutations** (ignition = times the mutated line ran):

| mutation | ignition | red | log |
|---|---|---|---|
| delete the admission clause | 61 | 7 | `logs/gate_not_run/mut_M1_admission_clause.txt` |
| let exit 1 become not-run | 63 | 1 | `logs/gate_not_run/mut_M2_rc1_guard.txt` |
| drop `evidence_state = not_run` | 87 | 2 | `logs/gate_not_run/mut_M7_not_run_state.txt` |

## Criterion `failure-identity-survives-a-long-output`

Identities come from the gate's structured findings under its ticket
(`python-findings.json`, `playtest-findings.json`, `script-findings.json`, and
`compile.json` `errors[]`). They never come from stdout. The 2000-character
tail was not raised: `OUTPUT_TAIL_CHARS` is still 2000, and it is overridden
only in the before/after runs.

**Real wuxia output.** I ran the game repository's own `run_tests.sh` /
`tools/godot_gate.py`, from a `git archive` of `43aff480` (git-initialised copy,
game repo not touched), through the real `run_tests` and relay.
- Engine replies: the real `compile.json` / `playtest.json` / `script.json` that
  passing gate `wuxia-godot-gate-cac48n7p` retained for that head. They are
  served by the real harness code, with 2 authored assertions turned red:
  `prologue_intercept_redirects_damage / GameManager.current_state` and
  `prologue_ward_bleeds_if_ignored / GameManager.current_state`.
- The engine path needs the python stage green. On the untouched copy, 7 game
  python test files were red: some read game git history that an archive
  lacks, and one is rejected by the current AItelier timeline normalizer. So
  the copy `gn2-wuxia-copy-py` drops those 7 files.

| run | tail | output kept / whole | truncated | new_failures | identities | identity error | log |
|---|---|---|---|---|---|---|---|
| cand, copy-py | 2000 | 2000 | true | 5 | `playtest/8fc2ce2c748a`, `playtest/7a7897ddcd00`, `playtest/cb5c60ec12b2`, `playtest/faa4f4c493dd`, `playtest/2e140e237389` | none | `logs/gate_not_run/ev_wuxia_copy-py_cand_none.txt` |
| cand, copy-py | 1e9 | 3271 | false | 5 | the same 5, same order, same text | none | `logs/gate_not_run/ev_wuxia_copy-py_cand_1000000000.txt` |
| base, copy-py | 2000 | 2000 | true | 1 (`repo_gate:run_tests.sh`) | none | "repository gate output was truncated" | `logs/gate_not_run/ev_wuxia_copy-py_base_none.txt` |
| cand, untouched copy | 2000 | 2000 | true | 24 (23 AItelier pytest + `python/3d959bcbf887`) | the gate's own single python finding | none | `logs/gate_not_run/ev_wuxia_copy_cand_none.txt` |
| cand, untouched copy | 1e9 | 2445 | false | 24, the same | the same | none | `logs/gate_not_run/ev_wuxia_copy_cand_1000000000.txt` |
| base, untouched copy | 2000 | 2000 | true | 24 (23 + `repo_gate:run_tests.sh`) | none | "repository gate output was truncated" | `logs/gate_not_run/ev_wuxia_copy_base_none.txt` |

**Three manifests under one ticket.** The real gate's python stage runs the
gate's own tests, which inherit `GATE_REPORT_DIR`. So one ticket held 3
manifests: 1 for the copy, 2 for the fixture repository `/repo`
(`rt-20260924T225857Z-082cbc32`, `rt-20260924T225253Z-78bd6abf`). The first
candidate read that as an identity error:
`logs/gate_not_run/ev_wuxia_copy_cand_none_before_manifest_fix.txt`, "retained 3
reports under one ticket". Commit `59f3252e` reads only the manifest whose
`repo` is the gate's repository.

**Fixture, 6000 dots of output** (`logs/gate_not_run/ev_long-fixture_{cand,base}.txt`):
- Candidate: truncated at 2000, 3 identities `script/a6414c8a715c`,
  `script/bef7e278faf2`, `script/012be646d084`, no error.
- Base: truncated at 2000, 1 entry, "repository gate output was truncated".

**Tests:**
- `test_repo_gate_identity_from_report_dir.py::test_a_long_red_output_still_names_every_failure`
- `::test_the_identities_do_not_depend_on_the_retained_tail`: 2000 vs 10^9
  tail, identical lists.
- `::test_reports_the_repositorys_own_tests_retained_are_not_the_gates`
- A red gate whose report names nothing is an identity error, never a pass.

**Mutations:**

| mutation | ignition | red | log |
|---|---|---|---|
| skip the report dir | 32 | 2 | `logs/gate_not_run/mut_M3_report_dir_branch.txt` |
| read every manifest | 11 | 1 | `logs/gate_not_run/mut_M8_any_manifest.txt` |

## Criterion `no-implement-loop-on-an-unrunnable-gate`

`tests/skillflow/test_coding_impl_busy_gate.py` drives `configs/coding_impl.yaml`
through the real SkillFlow engine in-process, with the absence suite's
`_wire`/`_drive`. Pytest is green, and the repository gate meets the real
harness with its lock held by `op-holder`.
- Phase 1 is 12 ticks with the lock held.
- Phase 2 is 12 ticks after `let_go()`.

The inventory printed is the equivalent of `relay_inventory`: run status, node
and error, plus the commits on the run's code path beyond the fixture commit.

| | base `658ee8df` (`logs/gate_not_run/busy_gate_base.txt`) | candidate (`logs/gate_not_run/busy_gate_cand.txt`) |
|---|---|---|
| implement commits while busy | 4 × `step: implement [p]` (1 + 3 loop-backs) | 1 (the round's own) |
| implement commits after release | – (run already failed) | 0 |
| gate calls | 4 | 5 while busy, 6 total |
| run while busy | `failed`, `Cycle limit exceeded`, last `repo_gate` rc 2 → `measured_fail` | `running` at `test_gate_absent` |
| tick outcomes while busy | 9 × `none` | `none`×3, then `silent`/`reacquire` alternating |
| after release | – | `completed`, last `repo_gate` rc 0 → `measured_pass`, admission `answered` |
| pytest BARE_RC | 1 (the assert `implement_runs_while_busy == 1` fails at 4) | 0 |

- The baseline in the brief was 3 implement commits after the 409. Here: 3
  loop-back commits plus the round's first.
- At base the harness queued the gate (no wait sent), and the gate's 3 s client
  timeout turned that into exit 2, "unreachable". That is the same exit-2 shape
  as the 409 in the field runs.

**Mutations:**

| mutation | ignition | red | log |
|---|---|---|---|
| `test_gate_absent` back to `to: null` | 9 | 10 (9 without the probe) | `logs/gate_not_run/mut_M5_absent_edge.txt` |
| hold for the whole episode | 182 | 2 | `logs/gate_not_run/mut_M6_hold_forever.txt` |

- In the probed M5 run, 1 of the 10 reds is the probe itself
  (`test_the_host_refuses_to_advance_before_reaching_the_framework` builds the
  host without `__init__`, and the probe calls `get_run`). The unprobed run is
  `logs/gate_not_run/mut_M5_absent_edge_noprobe.txt`, 9 red.
- The existing absence suite's two tick-alone / host-alone tests pinned
  "silent forever" (`count("silent") >= 10`). They now assert
  silent/re-acquire, with exactly one gate call per re-acquire.
  `logs/gate_not_run/absence_tests_before_update.txt` shows both red before
  the update (BARE_RC=1).

## Mutations: summary

8 mutations × the same 12 test files (204 tests), each in its own `rsync`
copy of tree `59f3252e`. All 8 turned at least one test red (BARE_RC=1 each).
- Control on the unmutated tree: 204 passed, BARE_RC=0
  (`logs/gate_not_run/targeted_control_cand.txt`).
- Ignition is the byte count of a probe file that the mutated line appends to
  on every execution (`logs/gate_not_run/mut_*.txt`, first lines).

## Whole suite

Every run used a throwaway container with `--init` and `-m 3g`, and each log
checks `git status` inside the container first (`gitrc=0`).

| tree | result | BARE_RC | log |
|---|---|---|---|
| base `658ee8df` (the 3 new test files `--ignore`d; they exist only as untracked copies there) | 5444 passed, 10 skipped, 11 deselected | 0 | `logs/gate_not_run/suite_base.txt` |
| candidate `59f3252e` | 2 failed, 5463 passed, 10 skipped, 11 deselected | 1 | `logs/gate_not_run/suite_cand_59f3252e.txt` |
| candidate `d41c0a4b` | 5465 passed, 10 skipped, 11 deselected | 0 | `logs/gate_not_run/suite_cand.txt` |

The 2 reds on `59f3252e`, and the fixes in `6b5949f1`:
- `test_public_read_hardening.py::test_every_subprocess_in_run_tests_scrubs_the_environment`
  counts literal `scrubbed_env()` calls. The overrides now go on after the
  literal call.
- `test_tree_level_accounting_witnesses.py::test_N9_the_protocol_text_has_a_reader`
  reads tool.yaml for "DECLARED and never". The text now says what holds: an
  absence is declared, or observed by the harness (a timeout, or an engine
  refusal the relay recorded). It is never inferred from what the gate prints,
  and no exit code produces it on its own.

The known flake
`test_isolated_recoverable_execution.py::test_parallel_runs_progress_but_competing_controllers_admit_one_owner`
did not go red in any of the 3 whole-suite runs, so it was not rerun alone.

## Not done

- **No live Godot render.**
  - The busy engine is the real harness code with its render stubbed, not an
    `aitelier-godot` sidecar container.
  - The wuxia engine replies are the real replies a passing gate retained,
    replayed by that harness code.
- **The wuxia run that reaches the engine uses a copy without 7 python test
  files.** They were red on the untouched archive copy. On the untouched copy
  the gate stops at its python stage, and its only retained finding is "the
  python suite exited 1": one identity. That is the game gate's own
  granularity. Per-test names for that stage exist only in its stdout, which
  this card does not parse.
- **The inventory for criterion 4 is not StateService's `relay_inventory`.** It
  is the in-process equivalent: the run's status, node and error, and its
  commits.
- **Gate-report tickets are not swept.** `$AITELIER_HOME/gate-reports/rt-*`
  directories accumulate.
- **The stdout `AITELIER_REPO_GATE_CASE` path still depends on the tail.** When
  a gate retains no report for its repository, that path is still refused on a
  truncated tail. The whole-output scan exists only for the
  `AITELIER_REPO_GATE_UNMEASURED` declaration.
- **A pytest red plus an absent gate still sets `repo_gate_absent`.** The run
  goes to `test_gate_absent` and re-acquires, not to implement.
  `evidence_state` is not set to `not_run` in that case.
- **The in-memory deferral ledger keeps an entry** for a run that completed
  through the gate. Nothing ticks it again.
- **`max_loop: 100` on `test_gate_absent -> test` can run out before the
  ceiling if the wait is tuned very low.** The wait is
  `AITELIER_GATE_DEFERRAL_WAIT_SECONDS`, whose floor is only "positive". At the
  default of 300 s, 100 waits is 30000 s, past the 10800 s default ceiling and
  the 6 h absolute one.
- **The game gate still sends no `render_wait_timeout_sec` of its own**
  (wuxia issue `iss-f85149f0954d4baa`). That fix belongs to the game repo and is
  out of scope. The relay keeps a wait the gate sends
  (`test_a_gate_that_sends_its_own_wait_keeps_it`).
- **Not deployed.** The `aitelier` container was not restarted, nothing was
  merged into main, and nothing was published.
- **I edited one pre-existing phrase in each of two files this card changes.**
  I removed one word from the comment above `require_completed` in
  `configs/coding_impl.yaml`, and two words from the "The other card" bullet in
  the protocol doc. Each was one of the brief's listed self-defence words, and
  the brief's guard-shape scan reads the files a card changes. The word-diffs
  show both edits.
