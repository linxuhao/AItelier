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

CONSUMER MAP — every reader of the fields this diff touches, grep'd in THIS tree (re-grep
before trusting a line; the numbers move). The diff changes NO producer byte: it narrows
`configs/coding_impl.yaml`'s `test_evidence` from `skipped: {const: false}` +
`node.skipped: {const: false}` to an `anyOf` whose FIRST branch is exactly that and whose
second branch admits ONE shape — a MEASURED pytest red (`returncode: 1`) beside a Node leg
the producer could not measure — and requires the node verdict inside it.

`skipped` (whole report; the absence statement).
  writes  aitelier/tools/run_tests/impl.py:3340-3345 (the node block's nothing-measured
          branch sets `skipped`, `infrastructure_unavailable` and `evidence_state` together)
          and the retained non-delivery report at :2916-2920.
  reads   core/coding_impl_outcome.py:142-145 (`measurement()` raises "incomplete pytest
          measurement") and :240-243 (`load_coding_outcome` raises "contradictory unmeasured
          completed report"); core/release_gate_migration.py:372-374 (the LEGACY dpe graph's
          `5_final_test` replan edge matches `skipped: true` `from_file`);
          aitelier/gate_evidence.py `report_state` (:220, :305, `_SKIP_MARKERS` at :15)
          answers `partial`/`unresolved`; the first branch of this schema; and
          configs/fix_tests.yaml, configs/subagent.yaml and
          configs/gen_coop_shell_ui60_20260907.yaml, each carrying the same `skipped` const
          in its own `test_evidence`.
  changes NOTHING for them: the second branch REQUIRES `skipped: true`, so every report it
          admits is still the absence those readers already refuse or classify. The diff
          moves which ROUTE such a report takes in coding_impl, not what it says.

`node.skipped` (the Node leg's own absence statement).
  writes  impl.py:1146 (`skipped = bool(unmeasured)`) into the aggregate dict at :1138,
          :1153 on the nothing-measured path, :1193 in `_run_node_project`'s `unmeasured`.
  reads   impl.py:3337 (copied onto the report) and :3347 (`executed.node_gate_ran`); the
          first branch of this schema; configs/gen_coop_shell_ui60_20260907.yaml:124-127;
          and core/coding_impl_outcome.py:147-153 THROUGH A LOOP VARIABLE (`leg =
          report.get(key)` for key in ("node", "repo_gate"), then `leg.get("skipped")`), so
          a literal grep for `node["skipped"]` misses it — that reader refuses a pre-change
          measurement whose node leg was skipped.
  changes the same nothing: the field is REQUIRED, not relaxed (`skipped: {const: true}`).

`node.passed` (the verdict over the Node projects the gate could MEASURE).
  writes  impl.py:1138 (`all(r["passed"] for r in measured)`), :1153 (absence: True), :1190
          (`{"passed": True, ...}`) and :1277 (`all(checks)`).
  reads   impl.py:3350 (`if not node["passed"]` turns the report red) and
          core/coding_impl_outcome.py:148 (`leg.get("passed") is not True` refuses a
          non-passing leg for the pre-change baseline).
  THIS DIFF IS WHERE IT MOVES: the field was already `type: boolean` here; it is now also
          REQUIRED, so a node section that omits it is refused as evidence instead of being
          read as a verdict. GAINS the refusal; LOSES nothing the producer writes — every
          path that writes a node section writes the boolean (the only path with no node
          section at all is the storage-preflight refusal, impl.py:3316 `node = None`, and
          that report is an absence that cannot match this branch either way).

`repo_gate_absent` (the REPOSITORY gate's absence — a different leg from the node gate).
  writes  impl.py:3456 (`report["repo_gate_absent"] = absent`), :3402 (False on the
          unattributable path), and the flattened copy on the RETURN at :3642.
  reads   core/gate_deferral.py:427 (`data.get("repo_gate_absent")` decides whether a
          deferral is actionable); core/coding_impl_outcome.py:144 and :241 (refuse);
          impl.py:2541 (`_apply_baseline`'s `absent`); and coding_impl's own `test` step,
          whose `repo_gate_absent: true` absence edge fires BEFORE the `written` edge — so a
          return carrying it never reaches `test_evidence` at all.
  changes NOTHING: the second branch names no clause for it, and the earlier edge keeps that
          shape out of reach.

`evidence_state` (the one word the producer writes about its own pytest leg's evidence).
  writes  impl.py:3345 and :2920 (`infrastructure_unavailable`), :3406 (`unattributable`),
          :3441 (`not_run`); :2647 recomputes it through `gate_evidence.report_state`.
  reads   aitelier/round_feedback.py:210 (the feedback report's own copy);
          aitelier/tools/godot_compile/impl.py:223/:275 write their own for a different
          tool; and this schema's second branch, which now pins
          `const: infrastructure_unavailable`.
  changes THIS: the branch admits only the report in which the producer itself said the
          pytest leg's evidence state is `infrastructure_unavailable`. A report saying
          `unattributable`, `not_run` or `partial` is refused — those are not a measured red.

`release_evidence` (the release disposition the producer folds onto the report).
  writes  impl.py:2648-2649 (`release_disposition(report)`), aitelier/tools/godot_compile/
          impl.py:382/:401.
  reads   core/release_gate_migration.py:55-68 (the legacy dpe graph's edges) and
          configs/addons/game_harness.yaml:78/:80/:185/:187/:497/:499.
  changes NOTHING: no clause here reads it. It is named because a review suspected this
          branch of moving it and it does not.

`storage_diagnosis.state` (the storage diagnosis, one of five strings).
  writes  impl.py:2827-2855 `_apply_storage_diagnosis` (called at :3249): one of
          exhausted_preflight / exhausted_during_run / ambiguous / clean / unknown.
  reads   impl.py:2511 (`_apply_baseline`'s pruning), :3267 (`storage_preflight_refused`),
          :3616 (the RETURN's `storage_state`); core/repository_executor.py:849 (a comment
          about the entry, not a reader); and this schema's second branch, which now pins
          `type: string` + `minLength: 1` + `not: {enum: [exhausted_preflight,
          exhausted_during_run]}`.
  changes THIS: a diagnosis that states NOTHING (null, false, 0, "", [] and {}) is refused
          instead of being read as "storage was measured and is fine", and an exhausted
          diagnosis is refused because it is the producer's own statement that storage cost
          the measurement.
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
        return {k: v for k, v in report.items() if k != key}

    def node_without(key):
        return dict(report, node={k: v for k, v in report["node"].items() if k != key})

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
