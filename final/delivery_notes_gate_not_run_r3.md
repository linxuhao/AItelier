# Delivery notes: a gate that could not run is not a failing test (r3)

State DAG project `aitelier`, node
`harness.a-gate-that-could-not-run-is-not-a-failing-test`, revision 3, external
attempt `attempt-d39243252a7d4145ac5d660ed639b78a`. Branch
`director/gatenotrun-r3-20260925`, base `aa88cb54293161be92508324d2235f05a618c268`
(the r2 candidate). This worker made no State DAG writes.

The r3 logs are in `logs/gate_not_run_r3/`. Each log's header names its tree
(`tree: … head: …`, and `in-container git head … status-lines`) and ends
with `BARE_RC=`. "base" logs ran on a detached worktree at `aa88cb54`;
"cand" logs ran on `34a9bee5` (code) or `7cdf6004` (code plus the identity
probe fix). After `7cdf6004` only the protocol doc, logs and this note
changed. The whole suite result for the delivered sha is not in the tree: a
log committed after the run would change the sha it names.

## Commits

| commit | what |
|---|---|
| `34a9bee5` | the r3 code, tests, doc rule and evidence scripts |
| `7cdf6004` | `identity_probe.py`: a local name shadowed a function; count red gates per assertion |
| last | protocol doc r3 section, r3 logs, the r2 word-diff log replaced by its digest, this note |

## 1. A red beats absence

`_run_repo_gate` counts the findings of the gate's retained report
(`repo_gate.retained_findings`) and `_repo_gate_outcome` reads that count
before the three sources of `unmeasured`.

Pole 3, the gnr2 reviewer's probe (`final/scripts/gate_not_run_r3/attack1_hidden_failures.py`,
four fields added), real harness admission code, harness held:

| scenario | tree | gate rc | measured | admission | retained | `repo_gate_absent` | `repo_gate_unmeasured` | `evidence_state` | probe BARE_RC | log |
|---|---|---|---|---|---|---|---|---|---|---|
| python red, then `/script` refused | base | 2 | unmeasured | not_admitted | (none) | true | true | not_run | 0 | `a1_red_then_refused_base.txt` |
| python red, then `/script` refused | cand | 2 | measured_fail | not_admitted | 1 | (absent) | (absent) | (absent) | 0 | `a1_red_then_refused_cand.txt` |
| `/compile` answered red, then `/script` refused | base | 2 | unmeasured | not_admitted | (none) | true | true | not_run | 0 | `a1_answered_red_then_refused_base.txt` |
| `/compile` answered red, then `/script` refused | cand | 2 | measured_fail | not_admitted | 1 | (absent) | (absent) | (absent) | 0 | `a1_answered_red_then_refused_cand.txt` |

## 2. `repo_gate_absent` only when nothing else failed

`repo_gate_absent` is `not failures[]`, read before the gate's own entry.

| scenario | tree | gate rc | measured | retained | `repo_gate_absent` | `repo_gate_unmeasured` | probe BARE_RC | log |
|---|---|---|---|---|---|---|---|---|
| pole 4: pytest red, `/script` refused | base | 2 | unmeasured | (none) | true | true | 0 | `a1_pytest_red_refused_base.txt` |
| pole 4: pytest red, `/script` refused | cand | 2 | unmeasured | 0 | false | true | 0 | `a1_pytest_red_refused_cand.txt` |

Pole 4 driven on the real `coding_impl` graph and tick (`drives_7cdf6004.txt`,
`POLE4`): `implement_runs` 4, `failed`, `Cycle limit exceeded` (the pytest
red's own implement loop), 4 gate calls, report `repo_gate_absent: false`,
`repo_gate_unmeasured: true`, no `silent`/`expired` outcome. The r2 reviewer's
D3 on `aa88cb54`: 1 implement run, "gate did not run" (review log
`a2_drive_cand.txt`). A pytest killed at its wall beside a refused gate is
also `repo_gate_absent: false`
(`test_a_pytest_killed_at_its_wall_beside_a_refused_gate_is_not_an_absence`).

The other scenarios of the same probe, cand vs base:

| scenario | base | cand | logs |
|---|---|---|---|
| refused, then the gate crashes | rc 1, measured_fail | rc 1, measured_fail | `a1_refused_then_crash_{base,cand}.txt` |
| refused, then SIGKILL | rc -9, unmeasured, absent true, not_run | same | `a1_refused_then_sigkill_{base,cand}.txt` |
| client timeout (1 s) during a 4 s red render | rc 2, unmeasured, abandoned, absent true | rc 2, unmeasured, abandoned, retained 0, absent true | `a1_client_timeout_during_red_render_{base,cand}.txt` |
| harness wait expiry, fixture gate | rc 2, unmeasured, not_admitted, absent true, not_run | same | `a1_wait_expiry_payload_base.txt`, `a1_wait_expiry_payload_cand_rerun.txt` |

`a1_wait_expiry_payload_cand.txt` (first run) printed the same fields and then
exited 1 in the probe's teardown (`OSError: [Errno 39] Directory not empty`
removing the harness temp dir). The rerun alone: BARE_RC=0.

## 3. The docs

- `docs/repo-gate-unmeasured-protocol.md`, "The rule": one sentence naming the
  three sources and both premises. Sections "First premise" and "Second
  premise" follow it; the r3 section at the end covers identities, the
  keepalive, the lap limit and the mutations.
- `aitelier/tools/run_tests/tool.yaml`: the sentence that began "DECLARED and
  never" is gone. The paragraph says `unmeasured` has exactly three sources,
  that an exit code is never a source by itself, and that none applies when
  the retained report names a failure.
- N9 pin (`tests/unit/test_tree_level_accounting_witnesses.py::test_N9_the_protocol_text_has_a_reader`):
  asserts the new tool.yaml and doc phrases on whitespace-normalised text,
  and that "DECLARED and never" and "never inferred" are absent from both.
  `_N9_SEGMENT` in `tests/unit/test_run_tests_unmeasured_declaration.py`
  is the exact new 9-line segment.
- Banned-phrase scan (the five phrases of
  `tests/integration/test_no_unfalsifiable_guarantees.py`, case-sensitive):
  no hit in `logs/`, `final/` or any file changed since `aa88cb54`.
  `logs/gate_not_run/worddiff_configs__coding_impl.yaml.txt` is replaced by
  its command, the output's sha256 (`fca237bb…bc3b`) and line count (34).

## 4. Reservations

**Identity stable across runs** (`identity_cand.txt`, BARE_RC=0). Part 1,
every retained red wuxia gate under `~/.AItelier/gate-reports` (32 red
gates when the probe ran): 125 findings, 125 distinct ids, 0 identity errors,
0 gates where records != findings. `ns47o858` (442 MB `playtest.json`, read
from its tail): 19 findings, 19 ids (r2: 14). `body_motion_probe /
MotionProbe.failures`: 1 id (`playtest/body_motion_probe/MotionProbe.failures@1190`)
over 18 red gates. `CultivationScreen.phase == "CARD_PICK"`: 3 ids (frames
830, 1200, 1550) over 2 red gates. Part 2, the game gate's own
`verify_playtest` (from `gn2-wuxia-copy/tools/godot_gate.py`) writes the
findings of a real report and of the same report with every failing row's
`observed` and the summary changed:

| report | findings | finding texts changed | identity lists equal |
|---|---|---|---|
| `xejan5yo` | 6 / 6 | 3 | true |
| `4bo8111t` | 1 / 1 | 1 | true |
| `v7onurjj` | 1 / 1 | 1 | true |
| `6eskoo9v` | 1 / 1 | 1 | true |

The line-by-line comparison is in the log (`==` per line). These are 4 reports
x 1 injection each; the 442 MB reports were not injected (a whole parse needs
more than the 3 GB container). `identity_cand_first_attempt.txt` is the run
that crashed on the shadowed name fixed in `7cdf6004`.

**Lap limit at a 60 s wait** (`laps_wait60_{base,cand}.txt`, BARE_RC=0 both;
wait 60 s, ceiling 10,800 s, clock 30 s per tick, harness held throughout):

| tree | driver | status | reason | edge count | gate calls | re-acquires |
|---|---|---|---|---|---|---|
| base | tick only | failed | `Gate 'test_gate_absent': cycle limit exceeded` | 100/100 | 101 | 101 |
| base | tick + second driver | failed | `Gate 'test_gate_absent': cycle limit exceeded` | 100/100 | 101 | 101 |
| cand | tick only | failed | `gate did not run: no verdict was measured (run_tests.sh, 503 attempt(s))` | 100/100 | 101 | 100 |
| cand | tick + second driver | failed | `gate did not run: no verdict was measured (run_tests.sh, 705 attempt(s))` | 100/100 | 101 | 100 |

`lap_trace_pre_commit.txt` is the per-tick trace that showed the first r3
version ending one lap early (it read the counter while the run stood at
`test`); `absence_laps_spent` now reads it only at `test_gate_absent`.

**Client timeout counted from admission** (`queue1400_{base,cand}.txt`,
`final/scripts/gate_not_run_r3/queue_after_1400s.py`): holder 1400 s, render
450 s, gate client timeout 1800 s, relay defaults (wait 1500 s, keepalive
20 s).

| tree | elapsed | gate rc | measured | admission | `admitted_after_sec` | keepalives | rendered for |
|---|---|---|---|---|---|---|---|
| base | 1800.4 s | 2 | unmeasured | abandoned | (absent) | (absent) | holder, repo |
| cand | 1850.4 s | 0 | measured_pass | answered | 1401.166 | 70 | holder, repo |

On base the harness rendered for the repo and nobody read the answer. Both
logs carry two `BrokenPipeError` tracebacks from the harness writing to the
holder's client, which gives up at its own 180 s.

**R5 mutant** (the host hold's `sf=` branch): N11 below, 2 red.

**`relay_inventory`: not obtained.** StateService `_relay_inventory`
(`core/state_service.py:394`) needs a `run_isolation` worktree record, which
only a real State attempt's run has, and this worker makes no State writes.
The busy-gate drive (`drives_7cdf6004.txt`, `GATE_EVIDENCE`) records an
in-process inventory instead. Field mapping:

| `_relay_inventory` | in-process `inventory_*` | value |
|---|---|---|
| `run_id` | `run_id` | same run |
| `commits` (branch commits beyond `base_sha`) | `commits` (run code path beyond its base) | while busy: 1 (`step: implement [p]`); final: the same 1 |
| `error` | `error`, plus `status` / `current_node` | while busy: `running` at `test_gate_absent`; final: `completed` at `test_outcome` |
| `staged_files`, `code_changes`, `head_sha`, `mainline_ahead_by`, `digest` | none | not measured |

## 5. Criteria 1-4 on this candidate

| criterion | numbers | logs |
|---|---|---|
| `coding-impl-stops-using-409-as-control-flow` | queue probe: the round's request waited 1399.975 s in the harness queue and was answered; mutation CONTROL 253 passed | `queue1400_cand.txt`, `mut_CONTROL.txt` |
| `contention-is-a-distinct-outcome` | wait expiry: rc 2, unmeasured, not_admitted, absent true, not_run; refused then rc 1: measured_fail | `a1_wait_expiry_payload_cand_rerun.txt`, `a1_refused_then_crash_cand.txt` |
| `failure-identity-survives-a-long-output` | 32 real red gates: 125 findings, 125 ids, 0 errors | `identity_cand.txt` |
| `no-implement-loop-on-an-unrunnable-gate` | busy: 1 implement run (the round's own), 0 after release, 5 gate calls while busy, 6 in total, final `completed` | `drives_7cdf6004.txt` |

## 6. Mutations

13 copies (CONTROL + 12), each run against the 18 files listed in the r3 doc
section (253 tests); all 12 mutants red, CONTROL green, every mutant's
ignition count above 0. Table: `docs/repo-gate-unmeasured-protocol.md`,
"Mutations (r3)". Logs `mut_<id>.txt`, `mut_<id>.ignition`,
`mut_<id>_apply.txt`; the job list is `jobs.tsv`.

## Not done

- `relay_inventory` from StateService (see 4).
- No injection on the two 442 MB reports (see 4).
- The keepalive was measured with the fixture gate and the harness admission
  code on 127.0.0.1, not with the game gate against `aitelier-godot`.
- No engine render, no merge, no tag, no PyPI, no State DAG write.
