# coding_impl now OBSERVES the implement step (one additive step, no routing change)

Kept as evidence for the card *"judge `coding_impl`'s implement step by OBSERVED
change, not by the worker's self-report"* (branch `coding-impl-observes`).
Owner instruction: migrate `coding_impl` first, run it for a few days, fix what
it reveals, only then migrate the rest — so this step is additive and routes
nothing.

Base: `main` at `c2adadd4c0` (the branch was rebased onto it after `main` moved
during the work); the artifacts below were produced on that base.

## What ran

The existing fixture — `Rig` in
`tests/skillflow/test_coding_impl_failed_candidate.py` (real native graph from
`configs/coding_impl.yaml`, real `run_tests`, real executor entry subprocess;
only the outer Docker launch is substituted by the enclosing disposable
container) — driven to termination inside the disposable test container:

    docker run --rm --network none --cpus 2 --memory 2g -u 1000:1000 \
      -v <worktree>:/w:ro \
      -v <worktree>/evidence/coding-impl-observes-20261104:/evidence:rw \
      -e IMPLEMENT_EVIDENCE_DIR=/evidence/confirmed-run \
      -e IMPLEMENT_EVIDENCE_UNCONFIRMED_DIR=/evidence/unconfirmed-run \
      -w /w aitelier:latest python3 -m pytest \
      tests/skillflow/test_coding_impl_implement_evidence.py -q -p no:cacheprovider

## `confirmed-run/` — a real change, observed

worker edits `feature.py` + `impl.py`, commits, `finish_step`; the observer
reads `implement/code_changes.json` and evaluates one `file_changed` per
declared file against the BASE COMMIT's content, plus one `command_exit` on the
delivered commit.

* `implement_evidence.json` — `verdict: confirmed`, `basis`: `feature.py changed
  as intended`, `impl.py changed as intended`, `exit 0 as expected`.
* `run.json` — the run `completed`; the gate still decided on its own and is
  still RED on this fixture (`test_report.json`: `passed: false`,
  `coding_outcome.cause_attribution: undetermined`). A confirmed observation
  releases nothing.
* `trace.jsonl`, `steps.jsonl` — the run trace and every step row, including
  `implement_evidence`'s own `outputs` (the whole returned dict, verdict first).
* `implement_code_changes.json` — the receipt the observation read.

## `unconfirmed-run/` — a claim the worktree contradicts

Same fixture, GREEN app, and the worker calls `finish_step` having written
nothing. Observed on this fixture: the implement step completes with
`code_changes.json.files == []` and flags `wrote_files: false`, and the run
advances — the exact shape this step exists for.

* `implement_evidence.json` — `verdict: unconfirmed`, `state: measured`,
  `missing[0]`: the receipt names no changed file.
* `run.json` — the run still `completed`, and `test_report.json` still passes:
  an unconfirmed observation cannot fail a run that would otherwise pass.

These are two DIFFERENT fixtures (red application vs green application), so
they are not compared with each other. The routing-unchanged proof is inside
`test_an_unconfirmed_observation_does_not_fail_a_green_run` and
`test_a_red_candidate_routes_identically_with_and_without_the_step`: each drives
the same fixture twice — once against the shipped YAML, once against a variant
with the step stripped out and `implement -> test` restored — and asserts the
same terminal status, the same completed-step sequence (minus the observer) and
the same transition list. `run.json` here keeps `completed_steps` and the engine
answer to `get_checkpoint` for the reader to check the claim against the artifact
it came from.
