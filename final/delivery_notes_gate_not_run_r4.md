# Delivery notes: gate-not-run, rev 4 (2026-09-25)

Node `harness.a-gate-that-could-not-run-is-not-a-failing-test` rev 4, attempt
`attempt-8a660c02b89c46e784552a03eb6b1c8e`. Branch
`director/gatenotrun-r4-20260925`, base `1b40d4845a59de759cb2de32def342048afd236e`.
Code and tests: commit `c9e6dd942663ec3b28eb3675916e1574dcec6a29`; every
evidence run below was on it. After it, `e8f90c6b` adds only
`logs/gate_not_run_r4/` and this file, and the next commit makes one
attribution sentence in `tool.yaml` and in the protocol doc exact (a report
that is the gate's first entry must still name the repository, and the
unobserved fallback) and edits this paragraph; no code or test file changes
after `c9e6dd94`. The whole suite runs on the last commit of the branch (log
outside the tree: `~/.AItelier/worktrees-scratch/gn4-logs/suite_<sha>.txt`).

Every test run below was in a throwaway `aitelier:latest` container
(`docker run --rm --init -m 3g`), never more than 4 throwaway containers on the
server; each log starts with the command, the in-container `git rev-parse HEAD`
and the import paths, and ends with `BARE_RC`.

## 1. The outcome table, candidate against base

`tests/unit/test_repo_gate_outcome_table.py::test_the_outcome_of_every_report_shape`,
22 rows. Each cell is `measured` / `repo_gate_absent` / next node of
`configs/coding_impl.yaml`.

| row | candidate | base | base row |
|---|---|---|---|
| `clean_refusal` | unmeasured / true / test_gate_absent | same | green |
| `python_red_then_refused` | measured_fail / false / implement | same | green |
| `compile_red_then_refused` | measured_fail / false / implement | same | green |
| `second_manifest_same_repo` | measured_fail / false / implement | unmeasured / true / test_gate_absent | **red** |
| `foreign_unreadable_manifest` | measured_fail / false / implement | unmeasured / true / test_gate_absent | **red** |
| `missing_stage_report` | measured_fail / false / implement | unmeasured / true / test_gate_absent | **red** |
| `nested_two_levels` | measured_fail / false / implement | unmeasured / true / test_gate_absent | **red** |
| `empty_findings_failed_stage` | measured_fail / false / implement | unmeasured / true / test_gate_absent | **red** |
| `foreign_names_repo_gate_wrote_none` | unmeasured / true / test_gate_absent | measured_fail / false / implement | **red** |
| `foreign_names_repo_beside_own` | unmeasured / true / test_gate_absent (identity error) | same triple, no identity error | **red** (identity) |
| `other_repo_reports_beside_green_gate` | measured_pass / false / done | same | green |
| `declared_absence` | unmeasured / true / test_gate_absent | same | green |
| `declared_failed_is_not_an_absence` | measured_fail / false / implement | same | green |
| `gate_killed_at_its_timeout` | unmeasured / true / test_gate_absent | same | green |
| `exit_2_after_an_answered_request` | measured_fail / false / implement | same | green |
| `exit_1_after_a_refusal` | measured_fail / false / implement | same | green |
| `pytest_red_beside_refused_gate` | unmeasured / false / implement | same | green |
| `pytest_wall_beside_refused_gate` | unmeasured / true / test_gate_absent | unmeasured / false / implement | **red** |
| `node_runner_unavailable_beside_refused_gate` | unmeasured / false / test_evidence_missing | same | green |
| `pytest_runner_unavailable_beside_refused_gate` | unmeasured / false / test_evidence_missing | unmeasured / true / test_gate_absent | **red** |
| `green_gate` | measured_pass / false / done | same | green |
| `answered_red` | measured_fail / false / implement | same | green |

Candidate: 24 passed (22 rows + the two citation tests), BARE_RC 0,
`logs/gate_not_run_r4/table_cand.txt`. Base: 9 failed, 13 passed, BARE_RC 1,
`logs/gate_not_run_r4/table_base.txt`. Coverage: these 22 constructed shapes,
each driven once through the real `run_tests` with a stub gate script and the
real admission relay against a fixture harness; no engine render.

The last base-red row is a change the director did not rule on by name: a
pytest runner that cannot be provisioned, beside a refused gate, went to
`test_gate_absent` on the base and now ends at `test_evidence_missing`, the
same route the director accepted for an unavailable node runner (waiting for
the gate does not provision a runner).

## 2. D4 on the real graph

`test_coding_impl_absence_needs_nothing_else_red.py::test_a_pytest_wall_behind_a_busy_gate_waits_at_the_absence_gate`
(real graph, real tick, the `run_tests` the ToolLoader loads, pytest wall 2 s,
busy fixture harness, episode ceiling 30 s, wait 5 s):
`implement_runs` 1, 6 gate calls, final node `test_gate_absent`, status
`failed`, terminal text
`gate did not run: no verdict was measured (run_tests.sh, 6 gate run(s))`.
3 passed, BARE_RC 0, `logs/gate_not_run_r4/drive_d4_cand.txt`.

The reviewer's own drive probe (`test_r3_drive.py`, unchanged) on the
candidate: D4 `implement_runs` 1, 6 gate calls, same terminal text (base had 4
and `Cycle limit exceeded`); D4b (lone pytest wall, gate green) 4 and
`Cycle limit exceeded`, unchanged from the base as the director ruled; D5
(npm unavailable, gate refused) 1 and `Node 'test_evidence_missing' reached`.
3 passed, BARE_RC 0, `logs/gate_not_run_r4/drive_reviewer_probe_cand.txt`.

## 3. The count in the terminal sentence

`test_the_absence_gate_laps_run_out_naming_the_absence` (wait 60, 101 gate
calls) now asserts the sentence ends with `(run_tests.sh, 101 gate run(s))`;
green on the candidate (`drive_d4_cand.txt`). The same test on the base ends
with `(run_tests.sh, 503 attempt(s))` for the same 101 gate calls
(`logs/gate_not_run_r4/laps_count_base.txt`, BARE_RC 1 on that assertion).

## 4. Planted sentences

The reviewer's P1-P4 sentence texts, inserted into the candidate (one detached
worktree each), against `test_every_routing_sentence_cites_a_row`,
`test_the_doc_routing_table_is_this_table`,
`test_tree_level_accounting_witnesses.py` and
`test_run_tests_unmeasured_declaration.py` (53 tests):

| plant | red | log |
|---|---|---|
| P1 "An absence is DECLARED and never inferred." (tool.yaml) | 2: `test_every_routing_sentence_cites_a_row`, `test_N9_the_protocol_text_has_a_reader` | `plant_P1_readd_never_inferred.txt` |
| P2 "UNMEASURED is only ever what the gate itself says; it is never deduced." (tool.yaml) | 1: `test_every_routing_sentence_cites_a_row` | `plant_P2_paraphrase_false.txt` |
| P3 "Every gate that did not run parks at `test_gate_absent`, whatever else failed." (tool.yaml) | 1: `test_every_routing_sentence_cites_a_row` | `plant_P3_false_routing.txt` |
| P4 "An unmeasured gate is only ever declared by the gate; nothing infers it." (doc) | 1: `test_every_routing_sentence_cites_a_row` | `plant_P4_doc_paraphrase.txt` |

What the citation test does not catch: a false routing sentence that carries
a citation of an existing row. It checks that every routing sentence names a
row or a drive and that the named row or test exists; the doc's routing table
is checked cell by cell against the rows, but prose is not parsed for its
claim.

## 5. Not done

* The lone pytest wall that loops four times (D4b) is unchanged, as the
  director ruled.
* No engine call, no gate run, no State DAG write, no merge, release or tag.
* The reviewer's probe files are not in the tree; they were run from
  `~/.AItelier/worktrees-scratch/gn4-probes/`.
