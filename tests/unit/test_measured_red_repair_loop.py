"""A MEASURED pytest red reaches coding_impl's repair loop; an absence still stops.

THE DEFECT THESE ASSERTIONS HOLD SHUT. `run_tests` marks the WHOLE report
`skipped: true` + `infrastructure_unavailable: true` whenever its Node leg
could not be measured (`aitelier/tools/run_tests/impl.py`, the node block: "NOTHING
was measured anywhere: the single-project absence"), and keeps the pytest leg's
own outcome beside it — `returncode: 1`, the `FAILED ...` lines. `coding_impl`'s
`test_evidence` schema used to require `skipped: {const: false}` AND
`node.skipped: {const: false}`, and BOTH of that gate's failing transitions point
at `test_evidence_missing` (a loop-external, `failed` terminal). A genuine pytest
red beside an unavailable Node leg therefore terminated the run instead of
reaching `test_outcome`'s repair loop (`-> implement`, `max_loop: 3`).

WHAT THIS TEST WOULD CATCH. It fails if the shipped gate regains a predicate that
refuses the report this producer really writes for that repository — either
`skipped`/`node.skipped` pinned to `false` again, or a replacement branch that
stops reading the producer's `returncode` witness — because the report would
validate `false` and the route would end at `test_evidence_missing`. It fails the
same way if the producer stops writing the measured verdict it writes today
(`skipped: true`, `node.skipped: true`, `returncode: 1`), because the test would
then no longer be about the shape that broke, and it fails if a *green* pytest
beside the same unavailable Node leg ever starts being routed back to the
implementer (control A) or if a plain green report stops reaching `done`
(control B).

NO HAND-BUILT SUBSTITUTES. All three reports are written by the REAL `run_tests`
over REAL repositories, on the same `repository_executor` entry the deployment
uses (`tests/gate_fixture.py`: the pytest session and the repository gate are
served, every `profile=` command is refused exactly as an image without a Node
runner refuses it). The validator is the SHIPPED native `json_schema` tool,
invoked with the SHIPPED step's own `files`/`inline_schema` (only
`$CONFIG_DIR/test` is replaced by the test's own directory, which is what the
engine does with it). The routing is the shipped graph's own `transitions`, in
their declared order, evaluated with the engine's own `_flags_match`; the flags
of the `test` step are the tool's return, which the engine merges into the step.

THE OPPOSITE REGRESSION, caught by the negative controls in the other two tests:
a branch that accepts a field which STATES NOTHING. Each control takes the SAME
real report, deep-copies it, replaces one field with a value that is not a
measurement, and requires the shipped validator + transitions to stop at
`test_evidence_missing`. They fail the moment `storage_diagnosis.state` loses its
`type: string` (null, false, 0, `[]` and `{}` all satisfy a bare
`not: {enum: [exhausted_preflight, exhausted_during_run]}`, so the report would
reach `implement` again) or its `minLength: 1` (`""` is still a string, which the
first attempt at this constraint measured), and the same way if `node.passed`
loses `type: boolean` (`"banana"` validates). That is the defect a non-author
review measured on the first version of the exceptional branch: an unmeasured
thing read as a measured one.

CONSUMER MAP — every PRODUCTION reader and writer of the fields this diff touches, grep'd
in THIS tree, plus the TEST CONSUMERS section at the end: the modules that NAME those
keys, swept rather than remembered, with the sweep and its numbers beside them. That
section is a census of NAMES, not of assertions: a mention is not a witness, and the
command is what a later reader runs instead of re-deriving this map.
CITATION CONVENTION: each one is named by its `path` plus the SYMBOL
or the exact clause it holds — e.g. ``aitelier/tools/run_tests/impl.py
`_aggregate_node_projects` (`"skipped": bool(unmeasured)`)``. A line number appears only
where the number is part of the thing described, and never as the sole identifier: parallel
branches merge over every number, and the earlier edition of this map was cited in numbers
that had already rotted on its own base. Re-grep the symbol before trusting a claim.

The diff changes NO producer byte: it narrows `configs/coding_impl.yaml`'s `test_evidence`
from `skipped: {const: false}` + `node.skipped: {const: false}` to an `anyOf` whose FIRST
branch is exactly that and whose second branch admits ONE shape — a MEASURED pytest red
(`returncode: 1`) beside a Node leg the producer could not measure — and requires the node
verdict inside it.

PROTECTED BOUNDARIES — all four were checked against this diff and NONE is weakened:
 (a) ACCEPTANCE CRITERIA: the second branch moves WHICH ROUTE an absence-beside-a-red takes
     to the repair loop; it admits no report that was ever an acceptance. `done` is still
     reachable only through `test_outcome`'s `passed: {const: true}`, the first branch is
     the shipped predicate unchanged, and no end condition or `max_loop` in
     `configs/coding_impl.yaml` changed.
 (b) THE EVIDENCE FORMAT: no producer byte moved. `run_tests` writes the same keys with the
     same values (`skipped: true`, `node.skipped: true`, `returncode: 1`, and the node
     verdict); the schema gained a second admissible SHAPE, not a new field, so every
     other consumer's input is unchanged — per-field below.
 (c) THE NON-AUTHOR REVIEW CONTROL: neither file removes, relaxes or bypasses a review or
     verification step. The change is a comment-only map plus one test fixture that now
     deep-copies (a control can only catch MORE), and no assertion was loosened. Acceptance
     of this card still comes from a non-author review of the delivered commit; nothing
     here approves itself.
 (d) THE PRIVACY / PUBLICATION BOUNDARY: nothing is published by this card and the
     publication gate is untouched — `release_disposition`'s
     `publication_scope_refusal(report.get("gate_coverage"))` branch, the
     `PROVISIONAL_PURPOSE` handling and `configs/addons/game_harness.yaml`'s release edges
     are all as they were. No source, artifact, manifest or PCK leaves the workspace.

`skipped` (whole report; the absence statement).
  writes  aitelier/tools/run_tests/impl.py, six branches, each setting the top-level
          `skipped=True` beside its own reason: `_resolve_pytest_python`'s
          persistent-outage branch (`skipped=True, infrastructure_unavailable=True,
          evidence_state="infrastructure_unavailable", returncode=0` — no pytest toolchain
          could be provisioned); `_mark_storage_unmeasured` (`skipped_because=
          "storage_exhausted"`); `_host_storage_failure`'s non-exhausted branch
          (`skipped_because="test_workspace_unavailable"`); `run_tests`'s own
          `pytest_wall_seconds` validation
          (`skipped_because="invalid_pytest_wall_seconds"`);
          its `except repository_executor.IsolationUnavailable` branch (`skipped_because=
          "cpu_executor_unavailable"`); and the node block's nothing-measured branch
          (`if node.get("skipped") and not node.get("measured_dirs")`, which sets
          `skipped`, `infrastructure_unavailable` and `evidence_state` together). The
          retained non-delivery payload from `_report_write_failure` is NOT one of them:
          it carries `infrastructure_unavailable` and `evidence_state` but writes no
          `skipped` key (the earlier edition claimed a `skipped` write there; there is
          none).
  reads   core/coding_impl_outcome.py `measurement` (`any(report.get(k) for k in
          ("skipped", ...))` raises "incomplete pytest measurement") and
          `load_coding_outcome` (the same tuple raises "contradictory unmeasured completed
          report"); core/release_gate_migration.py `old_final`, the LEGACY dpe graph's
          `5_final_test` replan edge — `{"match": {"field": "skipped", "from_file":
          "test_report.json", "value": True}, "to": "5_final_test_replan"}`.
  reads   aitelier/gate_evidence.py, and WHICH FUNCTION ANSWERS WHICH WORD is the point:
          `_absence_state` holds the executable read (`markers = [name for name in
          _SKIP_MARKERS if report.get(name)]`, `_SKIP_MARKERS` at the top of the module)
          and answers `skipped`; `report_state` calls it and returns that word verbatim —
          but for an ORDINARY (non-provisional) report of the shape this branch admits,
          the clause that fires FIRST is
          `report.get("infrastructure_unavailable") is True or status in _INFRA_STATUSES`,
          so `report_state` answers `infrastructure_unavailable`, not `skipped` and not
          `unresolved`; `release_disposition` then answers `unresolved` (its final
          `return "unresolved"`), for every word in that absence vocabulary.
          A PROVISIONAL MEASURED RED IS THE EXCEPTION, and this branch admits that report
          too: when its `purpose` is `provisional_round_feedback`, its
          `PROVISIONAL_PRODUCER` is `run_tests`, its `returncode` is 1 and it carries
          neither `measurement_not_taken` nor `storage_unmeasured`,
          `_provisional_red_is_measured` answers True, so `report_state` SKIPS
          `_absence_state` (`if not measured_red:`) and answers `failed`, and
          `release_disposition` answers `known_failure` through its `state in
          _KNOWN_FAILURE_STATES` branch — NOT `infrastructure_unavailable` /
          `unresolved`. The very same `node.skipped: true` section that makes an ordinary
          absence an absence buys such a red no softening; the absence beside it is
          neither a pass nor a park.
          `audit_evidence` buckets the absence words apart (`"skipped": "skipped_gates"`,
          `"infrastructure_unavailable": "infrastructure_unavailable_gates"`), and
          `_audit_one` derives `skipped_because` (`gate_unrun` / `gate_skipped`) from
          the report's own `status` / `unrun` when the state is `skipped`.
  reads   (config-side routing) the FIRST branch of this schema (`skipped: {const:
          false}`); configs/coding_impl.yaml's own `failed_candidate_evidence` step —
          reached by the `test` step's transition `- to: "failed_candidate_evidence"`
          `match: {field: retained_red_candidate, value: true}`, and validating
          `skipped: {const: false}` in its own `inline_schema`; and
          configs/gen_coop_shell_ui60_20260907.yaml's `test_evidence`
          (`properties: skipped: {const: false}` beside `node: {skipped: {const:
          false}}`).
  reads   (NOT this const — a CONTRAST, stated accurately): configs/fix_tests.yaml and
          configs/subagent.yaml have NO `test_evidence` step and no `skipped` constraint
          anywhere. Their edges are `match: {field: "node_gate_complete", value: false}`
          and `match: {node_gate_complete: true, passed: true}` — they route on the
          producer's RETURN flag, not on this const. The earlier edition claimed each of
          them carried "the same `skipped` const in its own `test_evidence`"; neither has
          a `test_evidence` step at all.
  changes NOTHING for any of them: the second branch REQUIRES `skipped: true`, so every
          report it admits is still the absence those readers already refuse or classify.
          The diff moves which ROUTE such a report takes in coding_impl, not what it says.

`node.skipped` (the Node leg's own absence statement).
  writes  impl.py `_aggregate_node_projects` (`"skipped": bool(unmeasured)` in the
          aggregate dict literal, and `node.update(passed=True, skipped=True,
          unmeasured_reason=reason, ...)` on its nothing-measured path),
          `_run_node_project`'s `unmeasured()` closure (`node.update(passed=True,
          skipped=True, unmeasured_reason=reason, kind=kind, ...)`), and
          `unmeasured_entry` (`{"dir": ..., "passed": True, "skipped": True, ...}` for a
          project past the cap, omitted by the repository's declaration, or raised).
  reads   impl.py `_aggregate_node_projects` itself, on the ENTRY level before the section
          exists (`measured = [r for r in results if not r.get("skipped")]`, and the
          `unmeasured` comprehension `for r in results if r.get("skipped")`).
  reads   impl.py, on the SECTION, the three EXECUTABLE readers in the node block of
          `run_tests` — the function that contains them, NOT `_aggregate_node_projects`,
          which only builds the node dict above and writes none of these:
          `report["node_gate_complete"] = not (node.get("skipped") or
          node.get("unmeasured"))` (completeness, written to the report and returned by
          `_node_gate_verdict`); `executed.node_gate_ran = (not node.get("skipped") and
          not unmeasured_projects)` (execution bookkeeping — the `unmeasured_projects =
          node.get("unmeasured") or []` line above it only reads the list, it does no
          bookkeeping); and `if node.get("skipped") and not node.get("measured_dirs"):`,
          the nothing-measured branch that sets the whole-report `skipped`,
          `infrastructure_unavailable` and `evidence_state`. `run_tests._node_gate_verdict`
          is a DIFFERENT reader: it reads `node_gate_complete` (absent answers True), not
          this key.
  reads   the first branch of this schema and configs/gen_coop_shell_ui60_20260907.yaml
          (`node: {skipped: {const: false}}` in its `test_evidence`); and
          core/coding_impl_outcome.py `measurement` THROUGH A LOOP VARIABLE —
          `for key in ("node", "repo_gate"): leg = report.get(key)`, and two lines later
          the verdict `if leg is not None and (leg.get("passed") is not True or
          leg.get("skipped") or ...)` raising `ValueError("unsupported nonpassing leg")`
          — so a literal grep for `node["skipped"]` misses it. That reader refuses a
          pre-change measurement whose node leg was skipped.
  changes the same nothing: the field is REQUIRED, not relaxed (`skipped: {const: true}`).

`node.passed` (the verdict over the Node projects the gate could MEASURE).
  writes  impl.py `_aggregate_node_projects` (`"passed": all(r["passed"] for r in
          measured)` in the aggregate dict, and `passed=True` in the nothing-measured
          `node.update`); `_run_node_project` — the initial `node: dict = {"passed": True,
          "dir": ..., "checks": {}}`, `node["passed"] = False` when the `node_fetch`
          install fails, `node["passed"] = all(c["passed"] for c in
          node["checks"].values())` as the final verdict, and `passed=True` in its
          `unmeasured()` closure; and `unmeasured_entry`.
  reads   impl.py `_aggregate_node_projects`'s own `all(r["passed"] for r in measured)` (the
          per-project boolean is its input) and the node block's `if not node["passed"]:`,
          which turns the report red and appends one failure entry per failing check;
          core/coding_impl_outcome.py `measurement`'s `leg.get("passed") is not True`
          (refuses a non-passing leg for the pre-change baseline).
  THIS DIFF IS WHERE IT MOVES: the field was already `type: boolean` here; it is now also
          REQUIRED, so a node section that omits it is refused as evidence instead of being
          read as a verdict. GAINS the refusal; LOSES nothing the producer writes — every
          path that writes a node section writes the boolean. The earlier edition called the
          storage-preflight refusal "the only path with no node section", and that is FALSE
          twice over: `_run_node_checks` returns `None` before it runs any project when the
          repository has none (`projects, absent = _find_node_projects(repo)`; `if not
          projects and not absent: return None`), so a Python-only repository carries no
          `node` key at all — the plain-green control in this module (control B: a real
          repository whose pytest suite passes and which has no `package.json`) asserts
          exactly that absence — beside the storage-preflight `node = None if
          storage_preflight_refused else ...`. Both are absences, and neither can match
          this branch either way.

`repo_gate_absent` (the REPOSITORY gate's absence — a different leg from the node gate).
  writes  impl.py `report["repo_gate_absent"] = absent` in the `REPO_GATE_UNMEASURED` branch
          (`absent = (not measured_elsewhere and not
          report.get("infrastructure_unavailable"))`), `report["repo_gate_absent"] = False`
          on the unattributable path, and the flattened copy on the RETURN
          (`"repo_gate_absent": bool(report.get("repo_gate_absent"))`).
  reads   core/gate_deferral.py `read_absence` (`absent = (data.get("repo_gate_absent") if
          "repo_gate_absent" in data else data.get("repo_gate_unmeasured"))` decides
          whether a deferral is actionable); core/coding_impl_outcome.py `measurement` and
          `load_coding_outcome` (both refuse a report carrying it); impl.py
          `_apply_baseline` (`absent = bool(report.get("repo_gate_absent") or
          report.get("repo_gate_unmeasured"))`); and — the config-side routing consumer —
          configs/coding_impl.yaml's `test` step transition `- to: "test_gate_absent"`
          `match: {field: "repo_gate_absent", value: true}`, which fires BEFORE the
          `written: "test_report.json"` edge, so a return carrying it never reaches
          `test_evidence` at all.
  changes NOTHING: the second branch names no clause for it, and the earlier edge keeps
          that shape out of reach.

`evidence_state` (the producer's own word for the state of this report's evidence).
  writes  impl.py, on the report: `_resolve_pytest_python`'s persistent-outage branch
          (toolchain unavailable), `_mark_storage_unmeasured`, `_report_write_failure`'s
          retained non-delivery payload (a report that could not be stored at all), the
          `except repository_executor.IsolationUnavailable` branch, the node block's
          nothing-measured branch (this writer says the NODE leg was unavailable), and
          `report["evidence_state"] = "unattributable"` / `"not_run"` on those two paths;
          plus `_commit_baseline`'s write-failure branch, which restates through
          aitelier/gate_evidence.py `restate_classification(report, create=False)` and so
          through `report_state`. The stamp writes it too (gate_evidence `_write_state`,
          reached from `_stamp_classification` via `stamp_report`/`stamp_file`, and from
          `_commit_baseline` with `create=False` — see the readers-and-writers entry
          below), and so do other tools' producers: aitelier/round_feedback.py
          `run_feedback` (`"evidence_state": "partial"`),
          aitelier/tools/godot_playtest/impl.py, aitelier/tools/godot_compile/impl.py
          `_refuse_incomplete_pass` and `_godot_compile_unstamped`'s builder-unreachable
          branch, and docker/godot/godot_harness.py — all for their own reports.
  reads   aitelier/gate_evidence.py `_absence_state`
          (`status = str(report.get("evidence_state")
          or report.get("status") or "")`, which classifies the evidence for
          `report_state`; `_provisional_red_is_measured` deliberately reads NO state
          vocabulary); impl.py's main RETURN, which copies the state onto the flags the
          engine merges into the step (`"evidence_state": report.get("evidence_state")`);
          and this schema's second branch, which now pins
          `const: infrastructure_unavailable`.
  reads AND writes  aitelier/gate_evidence.py `_write_state` — one function, both roles,
          and the READ is what makes the write safe. It reads the field it is about to
          overwrite (`had_label = "evidence_state" in report`, `prior =
          report.get("evidence_state")`) and then reads it BACK through the derivation
          (`report["evidence_state"] = state; if report_state(report) == state: return`),
          restoring the producer's own word when the label does not read back as itself.
          Its docstring states the rule this map must not lose: a label may be written
          only when the report still reads as that label afterwards — the FIXPOINT, asked
          of `report_state` rather than of a list, so the vocabulary and its reader move
          together. Ignore the read and the write is a RENAME: `skipped` is not one of
          the module's absence statuses (`_UNRUN_STATUSES` / `_INFRA_STATUSES`), so
          stamping a report whose producer said `evidence_state: "not_run"` (green pytest
          leg, repository gate unmeasured) CARRIED THE ABSENCE OUT OF THE FILE, every
          later reader of that file answered `failed` / `known_failure`, and a failure
          RELEASES the run past the hold (`iss-b161216de6a14a81`). The pinned witness is
          `tests/unit/test_baseline_write_failure_keeps_absence.py`.
  changes THIS: the branch admits only the report in which the producer itself said the
          evidence state is `infrastructure_unavailable`. A report saying `unattributable`,
          `not_run`, `partial` or `skipped` is refused — those are not a measured red.
  NOT "the pytest leg's state", which is what the earlier edition called it: it is the
          report's evidence state, and one of its writers (the node block's
          nothing-measured branch) writes it because the NODE leg could not be measured, not
          the pytest leg. aitelier/round_feedback.py is a WRITER of this field, not a
          READER of it.

`release_evidence` (the release disposition the producer folds onto the report).
  writes  aitelier/gate_evidence.py `restate_classification` (`report["release_evidence"] =
          disposition` — the path `_stamp_classification`/`stamp_report`/`stamp_file` takes
          with `create=True`, and `_commit_baseline` takes with `create=False`) and
          `upstream_failed_report` (`"release_evidence": disposition` on the fresh blocker
          report); impl.py `release_disposition(report)` on the upstream-blocked fail-fast
          RETURN and on the main RETURN, beside the constant
          `"release_evidence": "unresolved"` in `_report_write_failure`;
          aitelier/tools/godot_compile/impl.py (the upstream-blocked return's
          `release_disposition(compile_report)`, and the stamped path that folds the two
          per-report dispositions into one); aitelier/tools/godot_vision/impl.py (the same
          two shapes for `vision_report.json`); and aitelier/round_feedback.py
          `run_feedback`, which writes THIS field only THROUGH the stamp — it calls
          `stamp_report(report, run_id=owner["run_id"], out_dir=...)` when `run_id` is set,
          and that reaches `restate_classification`, which is what sets
          `report["release_evidence"]`. It writes no `release_evidence` literal of its own.
          (The earlier edition listed three of these, two of them by a line number that
          had already moved; the symbols above are the inventory.)
  reads AND writes  aitelier/gate_evidence.py `restate_classification` reads the PRESENCE
          of both fields before writing either (`if create or "evidence_state" in
          report:`, `if create or "release_evidence" in report:`). With `create=True` the
          read cannot refuse; with `create=False` — the shape `_commit_baseline`'s
          write-failure branch calls — it is the entire safety property: a report whose
          producer wrote no label and no disposition must come out of a failed baseline
          write carrying NEITHER, never the stamp's answers invented for it. Ignore that
          read and a report that stated nothing gains a stated answer, which is the same
          class of defect as an absence read as a measured one. It is pinned by
          `tests/unit/test_baseline_write_failure_keeps_absence.py`
          (`test_an_unstamped_absence_gets_no_label_or_disposition_it_never_had`).
          `_classify` (above it) makes the same point from the other side: it reads
          BOTH ANSWERS before either is written, so neither is computed from the other's
          annotation.
  NOT `release_evidence` — a DIFFERENT KEY, stated so the two are never blurred again:
          aitelier/round_feedback.py `run_feedback` and
          aitelier/tools/godot_playtest/impl.py each write the CONSTANT
          `"release_disposition": "unresolved"` onto the report they AUTHOR. That key is
          named after `gate_evidence.release_disposition`, but it is not its output field:
          no reader above and no config edge reads it, `restate_classification` never
          writes it, and NEITHER producer writes `release_evidence` — the playtest
          fallback writes none itself and calls no stamp, and `run_feedback` only gets one
          from the `stamp_report` call above. An earlier edition listed both as
          `release_evidence` writers; a non-author review measured that against
          `round_feedback.py`'s dict literal, which is the confusion this paragraph exists
          to end.
  reads   core/release_gate_migration.py `_TARGET_COMPILE` / `_TARGET_VISION` /
          `_TARGET_FINAL` (the legacy dpe graph's `5_compile` -> `5_vision`, `5_vision` ->
          `5_final_test` / `5_knowledge` and `5_final_test` -> `5_review` /
          `5_final_test_replan` edges), and configs/addons/game_harness.yaml, three pairs of
          `{field: release_evidence, value: known_failure|passed}` edges.
  changes NOTHING: no clause here reads it. It is named because a review suspected this
          branch of moving it and it does not.

`storage_diagnosis.state` (the storage diagnosis, one of five strings).
  writes  impl.py `_apply_storage_diagnosis` (`report["storage_diagnosis"] = diagnosis`, the
          verdict of `diagnose_storage`: exhausted_preflight / exhausted_during_run /
          ambiguous / clean / unknown) and `_host_storage_failure`
          (`report["storage_diagnosis"] = {"state": state, "scope":
          "backend_output_root", ...}` for the backend's own output root).
  reads   impl.py `_mark_storage_unmeasured` (`ran = diagnosis["state"] ==
          "exhausted_during_run"`, which chooses the sentence it writes);
          `_apply_storage_diagnosis` itself (`if diagnosis["state"] in
          STORAGE_EXHAUSTED_STATES:` and, inside it, `if diagnosis["state"] ==
          "exhausted_preflight":`); its call site in `run_tests`, three reads
          (`== "exhausted_preflight"` sets `storage_preflight_refused`, `in
          STORAGE_EXHAUSTED_STATES` withdraws the prune licence, `==
          "exhausted_during_run"` retains the reds completed before the refusal);
          `_report_write_failure` (`retained["storage_diagnosis"] = diagnosis` and
          `retained["storage_state"] = diagnosis.get("state")`); the main RETURN
          (`"storage_state": (report.get("storage_diagnosis") or {}).get("state")`); and
          `_apply_baseline` (`diagnosed = (report.get("storage_diagnosis") or
          {}).get("state") or "unknown"`), which puts that word into a DIAGNOSTIC SENTENCE
          in `baseline_error` and returns with `baseline_state: "unmeasured"`. That last one
          is NOT a prune — nothing is pruned on this path, and the earlier edition's
          "`_apply_baseline`'s pruning" was wrong. The `except _HostPrepError` branch reads
          a NEIGHBOUR of this field, `report["storage_diagnosis"]["proof"]`, to decide
          `storage_preflight_refused`: `proof`, not `state`.
  reads   core/repository_executor.py has a comment ABOUT the entry and is not a reader; and
          this schema's second branch pins `type: string` + `minLength: 1` + `not: {enum:
          [exhausted_preflight, exhausted_during_run]}`.
  changes THIS: a diagnosis that states NOTHING (null, false, 0, "", [] and {}) is refused
          instead of being read as "storage was measured and is fine", and an exhausted
          diagnosis is refused because it is the producer's own statement that storage cost
          the measurement.

TEST CONSUMERS — the modules that NAME one of these fields. They are not production
readers, and naming a key is not the same as asserting on it: this section is a SWEEP, and
its numbers are a snapshot of THIS tree rather than a standing claim. One sweep per
unambiguous key, from the repository root:

    grep -rl --include=*.py <key> tests/

answers 42 modules here - repo_gate_absent 18 / evidence_state 17 / release_evidence 24 /
node_gate_complete 7, overlapping. The list below is those 42 minus this module, which
names all four because it IS this map: 41 rows, 17 / 16 / 23 / 6. If a merge moves the
tree, re-run the sweep; the numbers are stated only so a later reader can see at a glance
that they should. Two earlier editions of this paragraph were hand-written readings and
both were wrong - the first named four modules, the second named 41 and still could not be
reproduced, and it called `test_outcome` a reader of a `skipped` key it does not read. The
command, not the list, is the witness.

    tests/integration/test_full_pipeline_real_runner.py                 release_evidence
    tests/integration/test_new_pipelines_mocked_agent.py                node_gate_complete
    tests/skillflow/test_coding_impl_absence_needs_nothing_else_red.py  repo_gate_absent, evidence_state
    tests/skillflow/test_coding_impl_gate.py                            repo_gate_absent
    tests/skillflow/test_coding_impl_gate_absence.py                    repo_gate_absent
    tests/skillflow/test_config_loading.py                              repo_gate_absent, node_gate_complete
    tests/skillflow/test_gated_pipelines.py                             node_gate_complete
    tests/unit/test_absence_survives_the_stamp.py                       repo_gate_absent, evidence_state, release_evidence
    tests/unit/test_addon_registry.py                                   release_evidence
    tests/unit/test_baseline_write_failure_keeps_absence.py             repo_gate_absent, evidence_state, release_evidence
    tests/unit/test_coding_impl_node_gate_partial.py                    evidence_state, release_evidence, node_gate_complete
    tests/unit/test_dpe_refusal_boundaries/test_measured_failure.py     release_evidence
    tests/unit/test_dpe_test_caller_budget.py                           release_evidence
    tests/unit/test_gate_consumer_composition.py                        repo_gate_absent, release_evidence
    tests/unit/test_gate_deferral_execution_points.py                   repo_gate_absent
    tests/unit/test_gate_deferral_is_accounted.py                       repo_gate_absent
    tests/unit/test_gate_deferral_reads_the_run.py                      repo_gate_absent
    tests/unit/test_godot_compile_phase_evidence.py                     evidence_state
    tests/unit/test_harness_render_queue.py                             release_evidence
    tests/unit/test_node_gate_complete_return.py                        node_gate_complete
    tests/unit/test_normal_gate_binding_matches_engine.py               release_evidence
    tests/unit/test_partial_gate_cannot_reach_done.py                   node_gate_complete
    tests/unit/test_provisional_round_cannot_conclude_success.py        repo_gate_absent, release_evidence
    tests/unit/test_release_fail_fast.py                                evidence_state, release_evidence
    tests/unit/test_repo_gate_admission.py                              repo_gate_absent, evidence_state, release_evidence
    tests/unit/test_repo_gate_early_contract_red.py                     repo_gate_absent, evidence_state, release_evidence
    tests/unit/test_repo_gate_outcome_table.py                          repo_gate_absent, release_evidence
    tests/unit/test_repo_gate_red_beats_absence.py                      repo_gate_absent, evidence_state
    tests/unit/test_round_feedback.py                                   release_evidence
    tests/unit/test_run_tests_delivery_truthfulness.py                  evidence_state, release_evidence
    tests/unit/test_run_tests_gate.py                                   evidence_state
    tests/unit/test_run_tests_node_multi_project.py                     evidence_state
    tests/unit/test_run_tests_pytest_wall.py                            evidence_state, release_evidence
    tests/unit/test_run_tests_required_async_plugin.py                  evidence_state
    tests/unit/test_run_tests_storage_boundaries.py                     evidence_state, release_evidence
    tests/unit/test_run_tests_storage_exhaustion.py                     evidence_state, release_evidence
    tests/unit/test_run_tests_storage_namespace.py                      release_evidence
    tests/unit/test_run_tests_unmeasured_declaration.py                 repo_gate_absent
    tests/unit/test_scoped_gate.py                                      release_evidence
    tests/unit/test_tree_level_accounting_witnesses.py                  repo_gate_absent
    tests/unit/test_vision_handoff.py                                   release_evidence

  `skipped` is deliberately NOT a column above, and it gets no list either. The word is
  three different things in this tree: the WHOLE-REPORT absence flag this change is about
  (`report["skipped"]`, `node["skipped"]`); the DERIVED evidence-state label
  `report_state(...) == "skipped"` that an absence answers, which several tests assert
  about an absence and not about this flag; and neighbours or prose (`skipped_because`,
  `gate_skipped`, `skipped_gates`, comment text). `grep -rl --include=*.py skipped tests/`
  therefore answers 98 modules and is not a consumer census, and the narrower sweep for
  the flag's syntax (`["skipped"]`, `.get("skipped")`, `node.skipped`,
  `"skipped": true|false`) answers 22 - of which several only MENTION it. Which of those
  22 read the flag cannot be decided by grep, so the readers named below are the
  PRODUCTION ones plus this module, which reads the flag off the producer's own reports:

    aitelier/tools/run_tests/impl.py  `run_tests` WRITES it at its two
        `report.update(passed=False, skipped=True, ...)` sites - the nothing-measured
        branch and the Node-gate-unavailable path, beside `infrastructure_unavailable`
        and `evidence_state` (:2819 and :3353 in this tree)
    configs/coding_impl.yaml  `test_evidence`'s inline schema REFUSES either form -
        the first `anyOf` branch's `skipped: { const: false }` (:628) and
        `node: { skipped: { const: false } }` (:632), with both of its failing
        transitions landing on `test_evidence_missing` (:736 and :740 here)
    configs/coding_impl.yaml  `test_outcome` is NOT a reader of the flag: its inline
        schema (:748-752 here) requires `passed: { const: true }` and reads nothing
        else, which is why the whole-report flag alone cannot stop a green-looking
        report - the `test_evidence` refusal above is the only place it bites
  * tests/unit/test_node_gate_complete_return.py
    `test_return_and_file_both_say_incomplete_for_the_measured_partial_shape` — reads
    `node_gate_complete` on BOTH carriers of ONE report: the `run_tests` RETURN
    (`ret["node_gate_complete"] is False`, plus `isinstance(..., bool)` so a truthy
    non-boolean cannot stand in) and `test_report.json`
    (`report["node_gate_complete"] is ret["node_gate_complete"]`, which also requires the
    two to AGREE). It fails if the key leaves the file or the return, or stops being a
    boolean; its sibling `test_return_and_file_both_say_complete_for_the_green_control`
    reads the same two carriers for `True`.
  * tests/unit/test_node_gate_measured_semantics.py
    `test_PROBE_single_project_report_shape_is_unchanged` — reads `node.passed` (`is
    True`) and the ABSENCE of five aggregate-only keys on a one-project section
    (`projects`, `dirs`, `measured_dirs`, `unmeasured`, `no_tests`), then validates the
    whole report through `_validate` against the SHIPPED `test_evidence` `inline_schema`
    (`_evidence_schema`), i.e. against the first `anyOf` branch's `skipped` /
    `node.skipped` consts; and it reads `evidence_state`'s derivation by asserting
    `report_state(report) == "passed"`.
    `test_PROBE_single_project_a_gate_that_measured_nothing_keeps_the_key` reads
    `node.skipped` (`absent["node"]["skipped"] is True`) beside `absent["passed"] is
    False`: it fails if the nothing-measured path stops setting `node.skipped`, the key
    this diff's second branch requires.
  * tests/unit/test_baseline_write_failure_keeps_absence.py
    `test_an_absence_whose_baseline_cannot_be_written_stays_an_absence` — reads two of
    these fields off the REPORT only (`repo_gate_absent` `is True`; `evidence_state`
    `== "not_run"`, the producer's word survived the restatement) and `release_evidence`
    off BOTH carriers (`== "unresolved"`, on `report` and on `result`), and then asks
    `report_state` / `release_disposition` for their answer on the FILE and on a JSON
    round-trip of it. It fails if `_write_state` stops keeping the producer's absence
    word, or if `restate_classification` starts inventing a disposition the report never
    had.
  * tests/unit/test_run_tests_storage_boundaries.py
    `test_repository_text_claiming_numbered_dir_failure_is_not_trusted_evidence` — reads
    `storage_diagnosis.state` (`== "clean"`) and the ABSENCE of `skipped` /
    `infrastructure_unavailable` on a report whose own stdout says "no space left", then
    requires `report_state(report)` to be one of `failed` / `known_failure` /
    `unattributed` (which is `evidence_state`'s derivation answering a MEASURED red, not
    an absence). It fails if the diagnosis starts copying the repository's text, or if a
    clean diagnosis starts writing an absence flag.
"""
from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

from aitelier.tools.run_tests import impl as rt
from skillflow.graph import PipelineGraph, _flags_match
from skillflow.tool_loader import ToolLoader
from tests.gate_fixture import install_entry_executor

ROOT = Path(__file__).resolve().parents[2]
GRAPH = ROOT / "configs" / "coding_impl.yaml"
RUN_ID = "measured-red-repair"


def _graph() -> PipelineGraph:
    return PipelineGraph.from_yaml(GRAPH)


def _steps() -> dict:
    return {step.id: step for step in _graph().steps}


@functools.lru_cache(maxsize=None)
def _native_validator():
    """The shipped `json_schema` tool implementation, found the way the engine does."""
    import skillflow

    return ToolLoader(Path(skillflow.__file__).resolve().parent / "tools").load_fn(
        "json_schema")


def _gate_verdict(step, report: dict, workspace: Path) -> dict:
    """What the shipped step's OWN validator says about `report`.

    `json_schema` is a native tool: it returns `{all_passed, results}` and does
    not raise for a report that fails the schema. `_error` is the ENGINE's flag
    for a tool that raised, which is why `test_evidence`'s `_error` edge is not
    the one a refused report takes — the catch-all edge is.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    files = list(step.tool_params["files"])
    for name in files:
        (workspace / name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    verdict = _native_validator()(
        files=files,
        inline_schema=step.tool_params["inline_schema"],
        workspace_root=str(workspace))
    assert "_error" not in verdict, verdict
    return verdict


def _route(result: dict, report: dict, workspace: Path) -> list[str]:
    """The nodes `configs/coding_impl.yaml` walks from `test` for this outcome."""
    steps = _steps()
    flags = dict(result)          # a tool node's return becomes the step's flags
    node = "test"
    path = [node]
    for _ in range(len(steps)):
        for transition in steps[node].transitions:
            if transition.match is None or _flags_match(transition.match, flags):
                break
        else:
            raise AssertionError(f"no transition out of {node!r} matches {flags}")
        node = transition.to
        path.append(node)
        if steps[node].tool_name != "json_schema":
            return path
        flags = _gate_verdict(steps[node], report, workspace / node)
    raise AssertionError("the route never left the validator steps")


def _repo(tmp_path: Path, name: str, *, pytest_red: bool, node_project: bool) -> Path:
    """One real repository: a python suite that really passes or really fails,
    and (for the subject and control A) a Node section whose profile the executor
    seam cannot provision."""
    repo = tmp_path / name
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_contract.py").write_text(
        "def test_contract():\n"
        + ("    assert 1 + 1 == 3\n" if pytest_red else "    assert 1 + 1 == 2\n"),
        encoding="utf-8")
    if node_project:
        (repo / "package.json").write_text(json.dumps(
            {"name": "web", "version": "1.0.0",
             "scripts": {"build": "echo build", "test": "echo test"}}),
            encoding="utf-8")
    return repo


def _measured(tmp_path: Path, monkeypatch, name: str, *, pytest_red: bool,
              node_project: bool = True):
    """Run the REAL `run_tests` over that repository and read what it wrote.

    `state_dir` is passed because the shipped `test` step declares the `stateful`
    capability (the known-red baseline); `run_id` without
    `evidence_cycle_start` is what `coding_impl` does — its `test` step is not the
    one that opens an evidence cycle, so the report carries no stamp.
    """
    repo = _repo(tmp_path, name, pytest_red=pytest_red, node_project=node_project)
    calls = install_entry_executor(monkeypatch)
    out = tmp_path / "graph" / name / "test"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out), run_id=RUN_ID,
                          state_dir=str(tmp_path / "state" / name))
    report = json.loads((out / "test_report.json").read_text(encoding="utf-8"))
    return result, report, out.parent, calls


def test_a_measured_red_reaches_the_repair_loop_through_the_shipped_gate(
        tmp_path, monkeypatch):
    """The subject plus both controls, on the shipped gate and the real producer.

    Driven on: a python suite whose one test really fails, a `package.json` whose
    Node profile the executor cannot provision, the REAL `run_tests`, the SHIPPED
    `test_evidence`/`test_outcome` validators, and the shipped transitions.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))

    subject, subject_report, subject_graph, subject_calls = _measured(
        tmp_path, monkeypatch, "repo-red", pytest_red=True)

    # ── the producer's own facts. The Node leg was ATTEMPTED and could not be
    # provisioned (the seam refuses every `profile=` command, which is what an
    # image without a Node runner does), and it says so in its own words.
    assert any(call["args"][:1] == ["npm"] for call in subject_calls), subject_calls
    assert subject_report["node"]["skipped"] is True
    assert subject_report["node"]["checks"]["install"]["isolation_unavailable"] is True
    # …the pytest leg really ran and really failed…
    assert subject_report["returncode"] == 1, subject_report.get("returncode")
    assert subject_report["passed"] is False
    assert any(f.startswith("FAILED") for f in subject_report["failures"]), \
        subject_report["failures"]
    # …and the whole report still carries the absence vocabulary the old gate
    # refused. THIS is the field/predicate combination that used to end the run:
    assert subject_report["skipped"] is True
    assert subject_report["infrastructure_unavailable"] is True
    assert subject_report["evidence_state"] == "infrastructure_unavailable"

    # The route through the SHIPPED config: the gate is on the path and ACCEPTS
    # the file, so the repair loop is reached rather than the terminal.
    subject_path = _route(subject, subject_report, subject_graph / "evidence")
    assert subject_path == ["test", "test_evidence", "test_outcome", "implement"], \
        subject_path

    # CONTROL A — the SAME repository shape with the pytest leg passing. The
    # report differs in the measured verdict and nothing that matters here: it is
    # still `passed: false` (a Node leg was not measured), still `skipped: true`,
    # still `node.skipped: true`, still `infrastructure_unavailable: true`.
    control_a, control_a_report, control_a_graph, _calls = _measured(
        tmp_path, monkeypatch, "repo-green-node", pytest_red=False)
    assert control_a_report["returncode"] == 0, control_a_report.get("returncode")
    assert control_a_report["passed"] is False
    assert control_a_report["skipped"] is True
    assert control_a_report["node"]["skipped"] is True
    assert control_a_report["infrastructure_unavailable"] is True
    # So the gate's answer turns on the producer's `returncode` witness, not on
    # the absence: no measured red, no repair loop. An absence still stops.
    control_a_path = _route(control_a, control_a_report, control_a_graph / "evidence")
    assert control_a_path == ["test", "test_evidence", "test_evidence_missing"], \
        control_a_path

    # CONTROL B — a plain green report (no Node section, nothing absent) still
    # reaches `done`: widening the gate did not turn every report into a pass.
    control_b, control_b_report, control_b_graph, _calls = _measured(
        tmp_path, monkeypatch, "repo-green-plain", pytest_red=False,
        node_project=False)
    assert control_b_report["passed"] is True
    assert "skipped" not in control_b_report and "node" not in control_b_report
    control_b_path = _route(control_b, control_b_report, control_b_graph / "evidence")
    assert control_b_path == ["test", "test_evidence", "test_outcome", "done"], \
        control_b_path

    # THE DIFFERENTIAL, on one report. Only the pytest verdict is flipped; the
    # skipped/absent fields stay exactly as the producer wrote them. The shipped
    # validator refuses it, so the acceptance above cannot be satisfied by a gate
    # that merely stopped looking at `skipped`.
    unmeasured = dict(subject_report, returncode=0)
    refused = _gate_verdict(_steps()["test_evidence"], unmeasured,
                            subject_graph / "evidence-flipped")
    assert refused["all_passed"] is False, refused
    # …and the same file with the measured red restored is accepted.
    accepted = _gate_verdict(_steps()["test_evidence"], subject_report,
                             subject_graph / "evidence-restored")
    assert accepted["all_passed"] is True and accepted["results"][0]["passed"] is True

    # THE VETOES, still vetoes. `passed: false` alone is not a measurement: the
    # producer's own "this session measured nothing" flags outrank a raw
    # returncode (`storage_unmeasured`: a proved storage refusal; the pytest wall
    # and the missing executor write `measurement_not_taken`), and a report that
    # states no pytest verdict at all states nothing measured. Each must stay
    # refused. The last shape is the one the second branch's `required` exists
    # for: `properties` constrains only a key that is PRESENT, and without it a
    # `passed: false` report carrying no `returncode` passed the gate and reached
    # `implement` (measured, on `tests/skillflow/test_coding_impl_gate.py`'s
    # `report11` and here).
    subject_step = _steps()["test_evidence"]
    for name, shape in (
            ("storage_unmeasured",
             dict(subject_report, storage_unmeasured=True)),
            ("measurement_not_taken",
             dict(subject_report, measurement_not_taken=True)),
            ("no returncode",
             {k: v for k, v in subject_report.items() if k != "returncode"})):
        verdict = _gate_verdict(subject_step, shape,
                                subject_graph / f"evidence-{name.replace(' ', '-')}")
        assert verdict["all_passed"] is False, (name, verdict)


def _contradictory_shapes(report: dict) -> dict:
    """Shapes the exceptional branch must NOT admit: the base refused each of them.

    Each one is `report` with one thing changed. The reviewer executed the
    widened branch against the four families below (a `skipped: true` report with
    an explicit false veto; an exhausted-storage diagnosis with
    `storage_unmeasured: false`; a hand-made `node: {}`; no Node section at all)
    and they reached `implement`; the rest pin the producer's own witnesses.
    """
    def without(key):
        shape = copy.deepcopy(report)
        shape.pop(key, None)
        return shape

    def node_without(key):
        # A DEEP copy, not `dict(report, node=<new dict>)`: deleting a nested
        # property must not be able to reach the fixture it came from, and the
        # fixture is one object shared by every shape in this table.
        shape = copy.deepcopy(report)
        shape["node"].pop(key, None)
        return shape

    exhausted = {"state": "exhausted_during_run", "findings": [], "proof": []}
    return {
        "skipped true with measurement_not_taken false":
            dict(report, measurement_not_taken=False),
        "measurement_not_taken true":
            dict(report, measurement_not_taken=True),
        "storage exhausted during run with storage_unmeasured false":
            dict(report, storage_unmeasured=False, storage_diagnosis=exhausted),
        "storage exhausted during run with storage_unmeasured true":
            dict(report, storage_unmeasured=True, storage_diagnosis=exhausted),
        "storage exhausted before the run (preflight)":
            dict(report, storage_diagnosis={"state": "exhausted_preflight",
                                            "findings": [], "proof": []}),
        "hand-made node {} with no skipped key":
            dict(report, node={}),
        "no node section at all":
            without("node"),
        "node.skipped false beside skipped true":
            dict(report, node=dict(report["node"], skipped=False)),
        "skipped false beside node.skipped true":
            dict(report, skipped=False),
        "returncode is a boolean, not an integer":
            dict(report, returncode=True),
        "no infrastructure_unavailable witness":
            without("infrastructure_unavailable"),
        "no evidence_state witness":
            without("evidence_state"),
        "no storage_diagnosis witness":
            without("storage_diagnosis"),
        # The node section STATES NO VERDICT. A review measured that deleting
        # this key still validated while only `skipped` was required inside
        # `node`; the producer writes the boolean on every path that writes a
        # node section at all (see the consumer map at the top of this file),
        # so the key is required now and its absence must stop here.
        "node section without its passed verdict":
            node_without("passed"),
    }


def test_a_contradictory_or_hand_made_shape_still_stops_at_test_evidence_missing(
        tmp_path, monkeypatch):
    """Negative controls on the SHIPPED gate: each shape ends at `test_evidence_missing`.

    The base is the report the REAL producer writes for a measured red beside an
    absent Node leg (`_measured`, the same run the positive case uses). Each shape
    changes ONE thing from that base. Every one must be routed by the shipped
    config's own `test_evidence` validator and transitions to
    `test_evidence_missing`, never to `implement`.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    subject, subject_report, subject_graph, _calls = _measured(
        tmp_path, monkeypatch, "repo-red-controls", pytest_red=True)
    shapes = _contradictory_shapes(subject_report)
    assert len(shapes) == 14, sorted(shapes)
    for name, shape in shapes.items():
        # `subject` is the run_tests RETURN (the `test` step's flags); `_route`
        # validates `shape` at `test_evidence` through the shipped validator.
        path = _route(subject, shape,
                      subject_graph / f"route-{name.replace(' ', '-')}")
        assert path == ["test", "test_evidence", "test_evidence_missing"], (name, path)


def _shapes_that_state_nothing(report: dict) -> dict:
    """The real report with ONE field replaced by a value that measures nothing.

    `storage_diagnosis.state` is a field `diagnose_storage` always writes as a
    STRING; every value below is one that a bare `not: {enum: [...]}` lets
    through (null, false, 0, "", [] and {} all fail that enum's test, so the
    clause is satisfied) while stating no diagnosis at all. `node.passed` is the
    producer's own boolean verdict over its measured Node projects; a string
    there is a hand-made section, not a measurement.
    """
    def with_state(value):
        shape = copy.deepcopy(report)
        shape["storage_diagnosis"]["state"] = value
        return shape

    def with_node_passed(value):
        shape = copy.deepcopy(report)
        shape["node"]["passed"] = value
        return shape

    return {
        "storage state null": with_state(None),
        "storage state false": with_state(False),
        "storage state 0": with_state(0),
        "storage state empty string": with_state(""),
        "storage state empty list": with_state([]),
        "storage state empty object": with_state({}),
        "node.passed is not a boolean": with_node_passed("banana"),
    }


def test_a_storage_state_that_states_nothing_stops_at_test_evidence_missing(
        tmp_path, monkeypatch):
    """The branch's TYPE constraint, driven on the REAL producer's report.

    The base is the report the REAL `run_tests` writes for a measured red beside
    an absent Node leg — the same producer entry the positive case uses, never a
    hand-built substitute. Each shape is a DEEP COPY of it with one field
    replaced by a value that is not a measurement, and each must be refused by
    the SHIPPED `test_evidence` validator and the shipped transitions, i.e. end
    at `test_evidence_missing`.

    These controls fail if the branch's `state: {type: string}` is removed: null,
    false, 0, `[]` and `{}` then satisfy `not: {enum: [...]}` and the report
    reaches `implement` again. They fail if `minLength: 1` is removed too: `""`
    is a string, so it passed the type constraint alone (measured while writing
    these controls). They fail the same way if `node.passed: {type: boolean}` is
    removed (`"banana"` validates). The untouched deep copy is asserted to still
    reach `test_outcome -> implement`, so the refusal cannot be satisfied by a
    gate that stopped accepting the shape altogether.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    subject, subject_report, subject_graph, _calls = _measured(
        tmp_path, monkeypatch, "repo-red-unstated", pytest_red=True)

    # The pin is justified by the producer, not assumed here: the report this
    # branch is about carries a STRING diagnosis and a BOOLEAN node verdict.
    assert isinstance(subject_report["storage_diagnosis"]["state"], str), \
        subject_report["storage_diagnosis"]
    assert isinstance(subject_report["node"]["passed"], bool), subject_report["node"]

    # CONTROL — the untouched copy still reaches the repair loop, so every
    # refusal below is about the ONE replaced field.
    untouched_path = _route(subject, copy.deepcopy(subject_report),
                            subject_graph / "route-untouched")
    assert untouched_path == ["test", "test_evidence", "test_outcome", "implement"], \
        untouched_path

    shapes = _shapes_that_state_nothing(subject_report)
    assert len(shapes) == 7, sorted(shapes)
    for name, shape in shapes.items():
        slug = name.replace(" ", "-")
        path = _route(subject, shape, subject_graph / f"route-{slug}")
        assert path == ["test", "test_evidence", "test_evidence_missing"], (name, path)
        verdict = _gate_verdict(_steps()["test_evidence"], shape,
                                subject_graph / f"verdict-{slug}")
        assert verdict["all_passed"] is False, (name, verdict)
