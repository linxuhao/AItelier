# tests/unit/test_coding_impl_node_gate_partial.py
#
# `coding_impl` must not conclude DONE on a pytest-green round whose Node
# projects were NOT all measured.
#
# THE PRODUCER IS UNAMBIGUOUS: `aitelier/tools/run_tests/impl.py:3337-3338`
# writes `report["node_gate_complete"] = not (node.get("skipped") or
# node.get("unmeasured"))` on every path out of the node block, and the same
# boolean travels on the tool's RETURN (`_node_gate_verdict`, impl.py:2934-2971;
# main return impl.py:3666) — which is what the engine merges into `test`'s
# flags. `passed` is honestly true there: the projects that RAN passed.
#
# DRIVEN, NOT INFERRED — nothing here re-implements the matcher or the schema:
#   * the report AND the return come from the REAL `rt.run_tests`, with only the
#     process boundary (`repository_executor.execute`) served for the npm leg —
#     the same seam, and the same fixture repository (`_nine` / `_run`), that
#     `tests/unit/test_partial_gate_cannot_reach_done.py` uses for the other two
#     configs; imported, not copied;
#   * every routing decision is the ENGINE's own
#     `GraphResolver.resolve_transition` on the real `configs/coding_impl.yaml`,
#     with the engine's `file_reader` contract for the edges that read the file;
#   * every `json_schema` validator step on the way (`test_evidence`,
#     `test_outcome`) is EXECUTED from that step's own shipped `tool_params`,
#     with only `$CONFIG_DIR/test` substituted for the `test` step's output dir.
#     Its return is the flags the engine would hand the next step.
#
# THE THREE CONTROLS, driven identically:
#   (a) fully measured green                     -> `done`
#   (*) the defect: green + an unmeasured project -> `node_gate_incomplete`,
#       declared `failed`, never `done`
#   (b1) a MEASURED red (every project measured)  -> `test_outcome` -> `implement`
#   (b2) a measured red beside an unmeasured one  -> neither `done` nor the new
#        terminal: on this base it is still refused one step later as EVIDENCE
#        MISSING (measured, see the docstring of that test).
#
# DEVIATIONS FOUND BY MEASURING THIS BASE (2026-11-06), not assumed:
#   D1. The partial GREEN report the real producer writes has
#       `node.skipped: true` (impl.py:1146 sets `skipped = bool(unmeasured)`),
#       so `test_evidence`'s inline schema — `node: {skipped: {const: false}}`,
#       which this card does not change — already refused it ONE STEP LATER.
#       The pre-fix path is therefore `['test_evidence', 'test_evidence_missing']`
#       (measured), not `['test_evidence', 'test_outcome', 'done']`. The
#       `done`-reaching shape has `node.unmeasured` beside `skipped: false`,
#       which this producer never writes (a hand-built or older-producer shape).
#       The new edge is keyed on the RETURN, so it refuses BOTH shapes, at the
#       step that owns the verdict instead of inside another step's shape check —
#       and this file pins the routing that now names the partial gate.
#   D2. Consequently a partial RED did not reach the repair loop on this base
#       either: the same `node.skipped` const refused it as evidence. That is a
#       separate defect with a pending change on another attempt (it widens
#       `test_evidence` for a MEASURED pytest red, keyed on `returncode: 1`).
#       This card must not duplicate it, so control (b1) is the red that reaches
#       `implement` here, and (b2) asserts only what is true both before and
#       after that pending change: the new refusal does not capture a red.
#
# CONSUMER INVENTORY — every reader of `node_gate_complete`, `node_unmeasured`
# and `node["skipped"]` in this tree (grep'd; the same list, with what each loses
# or gains, is the comment beside the new edge in `configs/coding_impl.yaml`):
#   producer — aitelier/tools/run_tests/impl.py
#     :3337-3338  the report's copy, `not (node["skipped"] or node["unmeasured"])`.
#                 Gains: its `false` can no longer be carried into a success.
#     :2934-2971  `_node_gate_verdict` — the RETURN's copy; `True` when the report
#                 carries no key (Python-only repository). Gains its first routing
#                 reader here; the absent-key answer stays `True`, so a repo with
#                 no node section does not match the new edge.
#     :3086       the upstream-blocked fail-fast return carries the verdict and
#                 `passed: false` — cannot match the new edge.
#     :3666       the main return — the value that becomes `test`'s flags.
#     :2877-2881  `_report_write_failure` OMITS the key, so a non-delivery return
#                 cannot match; it still ends on `test_evidence_missing`.
#     :3371       the flattened `node_unmeasured` (read by gate_evidence below).
#     :3347       the same two node keys decide `executed.node_gate_ran`.
#     :1146       `_aggregate_node_projects`: `skipped = bool(unmeasured)` — the
#                 invariant behind D1.
#   configs — the three shipped graphs that run `run_tests`
#     fix_tests.yaml:112-113 / :129   partial -> terminal; success REQUIRES
#                                     `node_gate_complete: true` + `passed: true`.
#     subagent.yaml:124-125 / :139    the same pair.
#     coding_impl.yaml                the new edge `{node_gate_complete: false,
#                                     passed: true}` -> `node_gate_incomplete`
#                                     (same id as the other two, declared
#                                     `failed`). NARROWER than theirs on purpose:
#                                     here the red path is the fall-through
#                                     `written` edge, so a one-term edge would
#                                     capture a measured red (control (b1)).
#                                     DIFFERENCE, named rather than papered over:
#                                     `done` is NOT additionally conditioned on a
#                                     positive verdict here — coding_impl's success
#                                     is decided by a second validator step whose
#                                     flags are its own (`all_passed`), so the
#                                     positive form would have to go on `test`'s
#                                     `written` edge and would fail-close a return
#                                     that omits the key (the shape
#                                     tests/skillflow/test_coding_impl_gate.py's
#                                     scripted producer returns today). That is a
#                                     separate fail-open card for this config.
#     coding_impl.yaml `test_evidence` `node: {skipped: {const: false}}` — the
#                                     config-side reader of `node["skipped"]`:
#                                     a node gate that skipped is refused as
#                                     evidence. Left untouched (see D2); it loses
#                                     the partial-green case to the earlier edge
#                                     and gains nothing.
#   aitelier/gate_evidence.py:323 — `report_state` answers `partial` when a GREEN
#     report carries `node_unmeasured` (the FLATTENED key, not `node[...]`, and
#     not `node_gate_complete`). Gains: the graph now agrees with the state the
#     hold/deferral layer already derived for the same report. Loses nothing.
#   aitelier/gate_deferral.py:428 — reads `repo_gate_unmeasured`, a DIFFERENT key
#     (the repository gate, not the node gate). Untouched.
#   core/ — NO reader of `node_gate_complete` or `node_unmeasured` (grep: zero
#     hits), and no reader of `node["skipped"]` (the only `node`+`skipped` hit is
#     prose in core/state_driver_guide.py:193). This edge changes which terminal a
#     run reaches, not the report any core reader sees.
import json
from pathlib import Path

import skillflow as _skillflow_pkg
import yaml
from skillflow.graph import GraphResolver, PipelineGraph
from skillflow.tool_loader import ToolLoader

from tests.unit.test_partial_gate_cannot_reach_done import _nine, _run

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "coding_impl.yaml"
PARTIAL_TARGET = "node_gate_incomplete"


def _validator():
    """The SHIPPED `json_schema` implementation, loaded the way the engine does."""
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    return loader.load_fn("json_schema")


def _route(report_dir: Path, ret: dict) -> list:
    """The steps `configs/coding_impl.yaml` takes from `test`, in order.

    `ret` is `run_tests`' return — what the engine merges into `test`'s flags.
    Each `json_schema` step on the way is EXECUTED from its own `tool_params`
    (only `$CONFIG_DIR/test` is substituted: that is the `test` step's output
    dir, where the report this walk routes on lives), and its return becomes the
    next step's flags. The walk stops at the first step that is not such a
    validator: a terminal gate or the agent `implement`.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    steps = {step.id: step for step in graph.steps}
    resolver = GraphResolver(graph)

    def read(path: str) -> str:
        return (report_dir / path).read_text(encoding="utf-8")

    flags, node, path = dict(ret), "test", []
    while True:
        transition, target = resolver.resolve_transition(
            node, flags, {}, file_reader=read)
        assert transition is not None, (
            f"no transition from {node!r} for flags {sorted(flags)}")
        path.append(target)
        if target is None or target not in steps:
            return path
        step = steps[target]
        if step.step_type != "tool" or step.tool_name != "json_schema":
            return path
        params = dict(step.tool_params)
        params["workspace_root"] = str(report_dir)
        flags = _validator()(**params)
        node = target


def _terminal_declared(graph: PipelineGraph, node: str) -> str | None:
    """The `result` an end condition assigns `node`, or None."""
    for c in graph.end_conditions.conditions:
        if c.type == "node_reached" and c.node == node:
            return c.result
    return None


# ── the defect: a green round whose Node projects were not all measured ────
def test_a_green_report_with_an_unmeasured_node_project_cannot_reach_done(
        tmp_path, monkeypatch):
    """The defect, driven on the real producer's report. It says `passed: true`
    (the projects that RAN passed) and `node_gate_complete: false` (one never
    did). The run must not conclude success.

    Pre-fix measurement (this base, before the edge): the same walk was
    `['test_evidence', 'test_evidence_missing']` — refused one step later by
    `test_evidence`'s `node.skipped` const, under a terminal that says the
    EVIDENCE is missing rather than that the node gate is partial. The assertion
    below fails on the base for exactly that reason, and the new terminal names
    the real cause.
    """
    repo = tmp_path / "repo"
    order = _nine(repo)  # nine node projects; with no declaration the ninth is capped

    report, ret, out = _run(tmp_path, monkeypatch, repo)

    # The defect's precondition, asserted rather than assumed.
    assert report["passed"] is True, report["failures"]
    assert [e["dir"] for e in report["node_unmeasured"]] == [order[8]]
    assert report["node_gate_complete"] is False
    assert report["node"]["skipped"] is True          # the D1 shape, measured
    assert ret["node_gate_complete"] is False
    assert ret["passed"] is True

    path = _route(out, ret)

    assert path == [PARTIAL_TARGET], path
    assert "done" not in path
    assert _terminal_declared(PipelineGraph.from_yaml(CONFIG), PARTIAL_TARGET) == "failed", (
        "the partial-gate terminal must end the run as FAILED, not park it")


# ── control (a): a fully measured green still reaches `done` ──────────────
def test_a_fully_measured_green_still_reaches_done(tmp_path, monkeypatch):
    """Every node project measured, all green: the refusal must be invisible."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, ret, out = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert "node_unmeasured" not in report
    assert report["node_gate_complete"] is True
    assert ret["node_gate_complete"] is True

    assert _route(out, ret) == ["test_evidence", "test_outcome", "done"]


# ── control (b1): a measured red still reaches the repair loop ────────────
def test_a_measured_red_still_reaches_the_repair_loop(tmp_path, monkeypatch):
    """`web` fails its npm test with every project measured. The report is a
    MEASURED red (`node_gate_complete: true`), and it must keep the
    `test_outcome -> implement` repair loop."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, ret, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert report["passed"] is False, report["failures"]
    assert report["node_gate_complete"] is True
    assert ret["node_gate_complete"] is True
    assert ret["passed"] is False

    assert _route(out, ret) == ["test_evidence", "test_outcome", "implement"]
    # The loop is still the shipped `test_outcome -> implement` edge, budgeted:
    # the refusal added NO second repair path and no second lap budget.
    outcome = next(s for s in PipelineGraph.from_yaml(CONFIG).steps
                   if s.id == "test_outcome")
    repair = next(t for t in outcome.transitions if t.to == "implement")
    assert repair.match == {"all_passed": False} and repair.max_loop == 3


# ── control (b2): the refusal may not capture a red beside an absence ─────
def test_a_measured_red_beside_an_unmeasured_project_is_not_captured(
        tmp_path, monkeypatch):
    """`web` fails while the ninth project is capped out, so the report is BOTH
    red and partial (`node_gate_complete: false`, `node.skipped: true`).

    This is the case the new edge must NOT capture — it carries `passed: true`
    for exactly this reason. Measured on this base: the report is refused one
    step later as EVIDENCE MISSING by `test_evidence`'s `node.skipped` const (the
    D2 deviation), so it reaches NEITHER `done` NOR the new terminal. Widening
    that gate for a measured red is the pending measured-red attempt's change;
    this assertion is written to hold before and after it (a repaired red is
    `implement`, never `done` and never the partial-gate terminal)."""
    repo = tmp_path / "repo"
    order = _nine(repo)

    report, ret, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert report["passed"] is False, report["failures"]
    assert report["node_gate_complete"] is False
    assert report["node"]["skipped"] is True
    assert ret["node_gate_complete"] is False
    assert ret["passed"] is False

    path = _route(out, ret)

    assert PARTIAL_TARGET not in path, path
    assert "done" not in path, path
    # The measured answer on this base; named so a future widening is noticed.
    assert path == ["test_evidence", "test_evidence_missing"], path


# ── the artifact half: the edge, its place in the order, and its terminal ──
def test_the_partial_edge_is_a_flag_match_ordered_before_the_evidence_edge():
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in data["steps"]}
    edges = steps["test"]["transitions"]
    matches = [e.get("match") for e in edges]

    assert {"node_gate_complete": False, "passed": True} in matches, (
        "the partial-gate refusal must be a FLAG match on the `test` step: the "
        "verdict travels on run_tests' RETURN, and a `from_file` reader is "
        "step-bound and crashes the resolver on a non-object report")
    # `_flags_match` reads any match carrying BOTH `field` and `value` as the
    # indirect single-flag form and IGNORES every other key, so a two-term edge
    # must use the direct form or its second term would be silently dead.
    for match in matches:
        assert not (match and {"field", "value"} <= set(match)
                    and set(match) - {"field", "value"}), match

    targets = [e["to"] for e in edges]
    # The `_error` edge stays first (a failed invocation may not consume a prior
    # attempt's report); the refusal sits among the decision edges, BEFORE the
    # `written` edge that hands a report to validation — otherwise the partial
    # green would be validated and could reach `done` through `test_outcome`.
    assert targets[0] == "test_evidence_missing", targets
    assert targets.index(PARTIAL_TARGET) < targets.index("test_evidence"), targets
    assert targets[-1] == "test_evidence_missing", targets

    terminal = steps[PARTIAL_TARGET]
    assert terminal["step_type"] == "gate"
    assert [t["to"] for t in terminal["transitions"]] == [None], (
        "the terminal must have no outgoing edge: `implement` must not be "
        "reachable from a partial gate")


def test_the_refusal_does_not_touch_the_success_or_evidence_paths():
    """A refusal of success, not a new gate: `test_outcome` still routes
    `all_passed: true` -> `done` and `all_passed: false` -> `implement` with the
    same lap budget, and `test_evidence` keeps its shipped accept-set."""
    graph = PipelineGraph.from_yaml(CONFIG)
    steps = {step.id: step for step in graph.steps}
    assert [(t.to, t.match, t.max_loop) for t in steps["test_outcome"].transitions] == [
        ("test_evidence_missing", {"_error": True}, None),
        ("done", {"all_passed": True}, None),
        ("implement", {"all_passed": False}, 3),
    ]
    assert [(t.to, t.match) for t in steps["test_evidence"].transitions] == [
        ("test_evidence_missing", {"_error": True}),
        ("test_outcome", {"all_passed": True}),
        ("test_evidence_missing", None),
    ]

    assert _terminal_declared(graph, PARTIAL_TARGET) == "failed"
    assert _terminal_declared(graph, "done") == "completed"
    assert _terminal_declared(graph, "test_evidence_missing") == "failed"
    # The three shipped configs agree on the terminal's NAME, which is what makes
    # the three readable side by side.
    for other in ("fix_tests", "subagent"):
        other_graph = PipelineGraph.from_yaml(ROOT / "configs" / f"{other}.yaml")
        assert any(step.id == PARTIAL_TARGET for step in other_graph.steps), other
