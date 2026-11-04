"""coding_impl: the implement step is OBSERVED, and nothing about routing moves.

Owner instruction behind this file: migrate `coding_impl` first, run it for a
few days, fix what the observation reveals, and only then migrate the rest. So
the step added here must be strictly additive, and that is PROVED rather than
asserted:

* the graph's transitions, `max_loop`, `begin`, end conditions and
  `output_step` are identical with and without the step — read from the real
  YAML and from a variant with the step stripped out of it;
* the checkpoint semantics are unchanged: neither graph declares a checkpoint
  step, and the engine answers `get_checkpoint` the same way on both runs;
* a full run through the existing fixture (`Rig` in
  tests/skillflow/test_coding_impl_failed_candidate.py — real native graph, real
  entry subprocess, real run_tests) reaches the same terminal status and the
  same completed-step sequence with the step and without it;
* the observation can CONTRADICT the worker (a claim with no change behind it is
  `unconfirmed`) yet cannot fail a run that would otherwise pass (the green
  fixture still completes), and a `confirmed` observation cannot release a red
  gate (the red fixture still ends on the same outcome).
"""
import json
import os
from pathlib import Path

import yaml

from skillflow.core import StepResult
from skillflow.graph import PipelineGraph

from tests.skillflow.test_coding_impl_failed_candidate import Rig, useful_change

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "configs" / "coding_impl.yaml"
STEP_ID = "implement_evidence"


# ── the graph itself ───────────────────────────────────────────────────────

def _raw_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _without_the_step(data: dict) -> dict:
    """The same config with this card's step removed and implement->test restored."""
    stripped = json.loads(json.dumps(data))
    stripped["steps"] = [s for s in stripped["steps"] if s["id"] != STEP_ID]
    implement = next(s for s in stripped["steps"] if s["id"] == "implement")
    implement["transitions"] = [{"to": "test"}]
    return stripped


def _fixture_root(tmp_path: Path, data: dict) -> Path:
    """A repo-root-shaped dir whose coding_impl.yaml is `data`; everything else
    is the real tree, symlinked, so the fixture exercises the code under test."""
    root = tmp_path / "fixture-root"
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "coding_impl.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    for name in ("agent_configs", "templates", "aitelier"):
        (root / name).symlink_to(REPO_ROOT / name, target_is_directory=True)
    return root


def test_the_step_sits_between_implement_and_test_and_routes_nothing_else():
    graph = PipelineGraph.from_yaml(CONFIG)
    steps = {step.id: step for step in graph.steps}
    assert STEP_ID in steps, "the observation step must exist in the shipped graph"
    # implement stops where it used to (at a step that only observes) ...
    assert [(t.to, t.match, t.max_loop) for t in steps["implement"].transitions] \
        == [(STEP_ID, None, None)]
    # ... and the observer's ONLY edge is the one implement used to take.
    observer = steps[STEP_ID]
    assert observer.step_type == "tool"
    assert observer.tool_name == STEP_ID
    assert [(t.to, t.match) for t in observer.transitions] == [("test", None)]
    assert all(t.max_loop is None for t in observer.transitions), \
        "a lap budget here would bound the run"
    # It is not a terminal and cannot decide anything.
    terminals = {c.node for c in graph.end_conditions.conditions}
    assert STEP_ID not in terminals
    assert graph.begin == "baseline"
    assert [c.node for c in graph.end_conditions.conditions] != []


def test_routing_and_checkpoint_semantics_are_identical_with_and_without_the_step(tmp_path):
    """Proved against the real YAML, not asserted about intent."""
    with_step = PipelineGraph.from_yaml(CONFIG)
    without = PipelineGraph.from_yaml(_fixture_root(tmp_path, _without_the_step(_raw_config()))
                                     / "configs" / "coding_impl.yaml")
    assert with_step.validate() == [] and without.validate() == []

    added = {s.id for s in with_step.steps} - {s.id for s in without.steps}
    assert added == {STEP_ID}, "exactly one step may be added, and it must be this one"
    assert {s.id for s in without.steps} - {s.id for s in with_step.steps} == set()

    # Same start, same end conditions, same input/output declaration.
    assert with_step.begin == without.begin
    assert ([(c.type, c.node, c.result) for c in with_step.end_conditions.conditions]
            == [(c.type, c.node, c.result) for c in without.end_conditions.conditions])
    # `output_step` is the step readers treat as the run's result/checkpoint; it
    # is declared in `x-aitelier` and derived from the graph when absent.
    from core.pipeline_registry import derive_output_step
    assert derive_output_step(with_step) == derive_output_step(without), \
        "the derived result step moved when the observer was added"
    raw = _raw_config()
    stripped_raw = _without_the_step(raw)
    assert (raw["x-aitelier"]["output_step"] == stripped_raw["x-aitelier"]["output_step"]
            == "test")
    assert raw["x-aitelier"]["seed_file"] == stripped_raw["x-aitelier"]["seed_file"]

    # Every step that existed before keeps its transitions, its lap bounds and
    # its checkpoint declaration. `implement` is the ONE exception, and its
    # detour through the observer lands exactly where it used to go.
    a = {s.id: s for s in with_step.steps}
    b = {s.id: s for s in without.steps}
    for step_id in sorted(b):
        if step_id == "implement":
            continue
        assert [(t.to, t.match, t.max_loop) for t in a[step_id].transitions] \
            == [(t.to, t.match, t.max_loop) for t in b[step_id].transitions], \
            f"{step_id}: routing changed"
        assert (a[step_id].checkpoint, a[step_id].checkpoint_label,
                a[step_id].checkpoint_reject_to) \
            == (b[step_id].checkpoint, b[step_id].checkpoint_label,
                b[step_id].checkpoint_reject_to), \
            f"{step_id}: checkpoint semantics changed"
    assert [(t.to, t.match, t.max_loop) for t in a["implement"].transitions] \
        == [(STEP_ID, None, None)]
    # The observer contributes nothing but the hop: its own only edge carries the
    # same match/loop as the edge implement used to take, so the flow is the same.
    assert [(t.to, t.match, t.max_loop) for t in a[STEP_ID].transitions] \
        == [(t.to, t.match, t.max_loop) for t in b["implement"].transitions] \
        == [("test", None, None)]
    # Same checkpoint story: no step pauses, in either graph.
    assert [s.id for s in with_step.steps if s.checkpoint] == [] \
        == [s.id for s in without.steps if s.checkpoint]


def test_the_observer_is_not_a_checkpoint_and_does_not_become_one():
    """Checkpoint semantics are unchanged because this step never pauses a run."""
    step = next(s for s in PipelineGraph.from_yaml(CONFIG).steps if s.id == STEP_ID)
    assert step.checkpoint is False
    assert step.checkpoint_label == "" and step.checkpoint_reject_to == ""


# ── full runs through the existing fixture ─────────────────────────────────

def _drive_claiming_completion(rig) -> tuple[str, int]:
    """The fixture's own loop, MINUS the write.

    Observed on this fixture: an implement step can confirm with no recorded
    write at all — `code_changes.json.files == []`, step flags
    `wrote_files: false` — and the run advances. That is the worker claiming
    completion while nothing changed, which is exactly what the observer is for.
    (The fixture's `Rig.drive` is used unchanged wherever a real change is
    wanted: it turns a disk edit into the write-tool calls the engine records.)
    """
    count = 0
    for _ in range(80):
        rig.sf.advance_run(rig.rid)
        run = rig.sf.get_run(rig.rid)
        if run["status"] in {"completed", "failed"}:
            return run["status"], count
        claim = rig.sf.claim_next_step(rig.rid)
        if claim is None:
            continue
        assert claim.step_id == "implement", claim.step_id
        count += 1
        rig.sf.confirm_step(claim.token, StepResult(flags={}))
    raise AssertionError("the native graph did not terminate")


def _steps(rig) -> list[str]:
    """The completed-step sequence: the run's own record of where it went."""
    rows = rig.sf.trace_query(
        rig.rid,
        "SELECT step_id FROM skillflow_steps WHERE run_id = ? AND status = 'completed' "
        "ORDER BY completion_seq", (rig.rid,))
    return [row[0] for row in rows]


def _transitions_taken(rig) -> list[tuple]:
    rows = rig.sf.trace_query(
        rig.rid,
        "SELECT step_id, payload_json FROM skillflow_trace WHERE run_id = ? "
        "AND category = 'step' AND event = 'completed' ORDER BY seq", (rig.rid,))
    return [(row[0], json.loads(row[1] or "{}").get("next_node")) for row in rows]


def _evidence(rig) -> dict:
    path = rig.sf._workspace.get_step_dir("p", "coding_impl", STEP_ID) / "implement_evidence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _checkpoint_outcome(rig) -> tuple:
    try:
        rig.sf.get_checkpoint(rig.rid)
    except Exception as exc:                      # noqa: BLE001 — the answer IS the datum
        return ("raised", type(exc).__name__)
    return ("ok",)


def _rig(tmp_path: Path, monkeypatch, root: Path, name: str, *, green: bool = False) -> Rig:
    """The existing fixture, with the config root it should load."""
    import tests.skillflow.test_coding_impl_gate as gate_module
    monkeypatch.setattr(gate_module, "_REPO_ROOT", root)
    return Rig(tmp_path / name, monkeypatch, green=green)


def _dump_evidence(target: str, rig, *, status: str, count: int) -> None:
    """Keep the run trace and step outputs: an observation is only as good as the
    evidence a reader can re-open."""
    out = Path(target)
    out.mkdir(parents=True, exist_ok=True)
    step_dir = rig.sf._workspace.get_step_dir("p", "coding_impl", STEP_ID)
    (out / "implement_evidence.json").write_text(
        (step_dir / "implement_evidence.json").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "implement_code_changes.json").write_text(
        (rig.sf._workspace.get_step_dir("p", "coding_impl", "implement")
         / "code_changes.json").read_text(encoding="utf-8"), encoding="utf-8")
    report = rig.sf._workspace.get_step_dir("p", "coding_impl", "test") / "test_report.json"
    if report.is_file():
        (out / "test_report.json").write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
    trace = rig.sf.trace_query(
        rig.rid,
        "SELECT seq, step_id, category, event, payload_json FROM skillflow_trace "
        "WHERE run_id = ? ORDER BY seq", (rig.rid,))
    (out / "trace.jsonl").write_text(
        "\n".join(json.dumps({"seq": r[0], "step_id": r[1], "category": r[2],
                              "event": r[3], "payload": json.loads(r[4] or "{}")})
                  for r in trace) + "\n", encoding="utf-8")
    steps = rig.sf.trace_query(
        rig.rid,
        "SELECT step_id, status, completion_seq, result_flags_json FROM skillflow_steps "
        "WHERE run_id = ? ORDER BY id", (rig.rid,))
    (out / "steps.jsonl").write_text(
        "\n".join(json.dumps({"step_id": r[0], "status": r[1], "completion_seq": r[2],
                              "outputs": json.loads(r[3] or "{}")}) for r in steps) + "\n",
        encoding="utf-8")
    (out / "run.json").write_text(json.dumps({
        "run": dict(rig.sf.get_run(rig.rid)), "status": status, "implement_rounds": count,
        "completed_steps": _steps(rig), "transitions_taken": _transitions_taken(rig),
        "implement_evidence": _evidence(rig),
    }, indent=2), encoding="utf-8")


def test_a_confirmed_observation_does_not_release_a_red_gate(tmp_path, monkeypatch):
    """Full coding_impl run: the change is observed, and the red gate stays red."""
    rig = _rig(tmp_path, monkeypatch, REPO_ROOT, "with")
    status, count = rig.drive(useful_change)
    assert (status, count) == ("completed", 1)

    evidence = _evidence(rig)
    assert evidence["verdict"] == "confirmed", evidence
    assert evidence["state"] == "measured"
    # Named basis: the file_changed predicates and the delivered commit.
    assert {entry["predicate"] for entry in evidence["basis"]} == {"file_changed", "command_exit"}
    changed = [entry["detail"] for entry in evidence["basis"]
               if entry["predicate"] == "file_changed"]
    assert any("feature.py" in detail for detail in changed), changed

    # The gate still decides, and it is still RED on this fixture.
    assert rig.report()["passed"] is False
    assert rig.report()["coding_outcome"]["cause_attribution"] == "undetermined"

    if os.environ.get("IMPLEMENT_EVIDENCE_DIR"):
        _dump_evidence(os.environ["IMPLEMENT_EVIDENCE_DIR"], rig, status=status, count=count)


def test_the_verdict_reaches_the_trace_and_the_step_outputs(tmp_path, monkeypatch):
    """Not a log line: the verdict is a step output AND part of the step record."""
    rig = _rig(tmp_path, monkeypatch, REPO_ROOT, "with")
    status, _ = rig.drive(useful_change)
    assert status == "completed"

    # 1. The step's promoted output dir carries the verdict (this is the file
    #    get_step_output(run, "implement_evidence") hands back).
    step_dir = rig.sf._workspace.get_step_dir("p", "coding_impl", STEP_ID)
    assert sorted(p.name for p in step_dir.iterdir()) == ["implement_evidence.json"]
    body = json.loads((step_dir / "implement_evidence.json").read_text(encoding="utf-8"))
    assert body["verdict"] == "confirmed"
    assert body["basis"] and body["missing"] == []

    # 2. The trace records the tool result that produced it ...
    rows = rig.sf.trace_query(
        rig.rid,
        "SELECT payload_json FROM skillflow_trace WHERE run_id = ? AND step_id = ? "
        "AND category = 'tool_result'", (rig.rid, STEP_ID))
    assert [json.loads(row[0]) for row in rows] == [
        {"source": "tool_step", "written": "implement_evidence.json"}]

    # 3. ... and the step's own record keeps the whole returned dict, verdict first.
    step = rig.sf.trace_query(
        rig.rid, "SELECT outputs_json, result_flags_json FROM skillflow_steps "
                 "WHERE run_id = ? AND step_id = ? AND status = 'completed'",
        (rig.rid, STEP_ID))
    assert len(step) == 1
    outputs = json.loads(step[0][0])
    assert outputs["verdict"] == "confirmed" and outputs["basis"]
    assert json.loads(step[0][1])["verdict"] == "confirmed"


def test_an_unconfirmed_observation_does_not_fail_a_green_run(tmp_path, monkeypatch):
    """The contradiction, and the proof it costs a passing run nothing.

    The worker claims completion and writes NOTHING (observed on this fixture:
    the implement step completes with `code_changes.json.files == []`, flags
    `wrote_files: false`, and the run advances). On the GREEN fixture the gate
    passes and the run PASSES while the observation says `unconfirmed` — an
    observation may not turn a green run red. The same fixture with the step
    stripped out lands on exactly the same completed-step sequence.
    """
    data = _without_the_step(_raw_config())
    rig_without = _rig(tmp_path, monkeypatch, _fixture_root(tmp_path, data), "without",
                       green=True)
    status_without, count_without = _drive_claiming_completion(rig_without)
    assert status_without == "completed"
    assert rig_without.report()["passed"] is True, "the fixture itself must be green"
    without_steps = _steps(rig_without)

    rig = _rig(tmp_path, monkeypatch, REPO_ROOT, "with", green=True)
    status_with, count_with = _drive_claiming_completion(rig)
    assert status_with == "completed"
    assert rig.report()["passed"] is True, "the observation must not fail a passing run"

    # The claim was contradicted, by name.
    evidence = _evidence(rig)
    assert evidence["verdict"] == "unconfirmed"
    assert evidence["state"] == "measured" and evidence["confirmed"] is False
    assert evidence["missing"], "an unconfirmed verdict must say what is missing"
    assert "code_changes.json" in json.dumps(evidence["missing"]), evidence["missing"]

    # ... and the run went exactly where it would have gone without the step.
    assert count_with == count_without
    assert [s for s in _steps(rig) if s != STEP_ID] == without_steps
    assert [t for t in _transitions_taken(rig) if t[0] != STEP_ID] \
        == _transitions_taken(rig_without)
    # Same checkpoint semantics: neither graph pauses, and the engine answers
    # "Run '<id>' is not paused" (SkillFlowError) about BOTH finished runs.
    assert _checkpoint_outcome(rig) == _checkpoint_outcome(rig_without) \
        == ("raised", "SkillFlowError")

    if os.environ.get("IMPLEMENT_EVIDENCE_UNCONFIRMED_DIR"):
        _dump_evidence(os.environ["IMPLEMENT_EVIDENCE_UNCONFIRMED_DIR"], rig,
                       status=status_with, count=count_with)


def test_a_red_candidate_routes_identically_with_and_without_the_step(tmp_path, monkeypatch):
    """The loop, the lap budget and the terminal all survive the added step."""
    data = _without_the_step(_raw_config())
    rig_without = _rig(tmp_path, monkeypatch, _fixture_root(tmp_path, data), "without")
    status_without, count_without = rig_without.drive(useful_change)

    rig = _rig(tmp_path, monkeypatch, REPO_ROOT, "with")
    status_with, count_with = rig.drive(useful_change)

    assert (status_with, count_with) == (status_without, count_without)
    assert [s for s in _steps(rig) if s != STEP_ID] == _steps(rig_without)
    assert [t for t in _transitions_taken(rig) if t[0] != STEP_ID] \
        == _transitions_taken(rig_without)
    # The step really did run in the middle of that sequence, once per implement.
    assert _steps(rig).count(STEP_ID) == count_with
    run_with = rig.sf.get_run(rig.rid)
    run_without = rig_without.sf.get_run(rig_without.rid)
    assert run_with["status"] == run_without["status"]
    assert run_with["current_node"] == run_without["current_node"]
