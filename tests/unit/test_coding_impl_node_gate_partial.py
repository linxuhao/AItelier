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
# THE DELIVERY EDGE IS NOW A VERDICT, NOT A FILENAME (the fail-open card this
# module used to describe as "a separate card for this config"). `test`'s
# `{written: "test_report.json"} -> test_evidence` edge fired for ANY return
# that named the report, INCLUDING one carrying no `node_gate_complete` at all —
# a stale producer, a run spanning a deploy, a hand-made report. Everything
# downstream (`test_evidence` -> `test_outcome` -> `done`) concludes success on
# `passed: true` alone, so the ABSENT verdict still concluded DONE. The edge now
# requires the verdict POSITIVELY (`{node_gate_complete: true, written: ...}`)
# and an absent verdict on a GREEN round falls through to the step's
# unconditional `test_evidence_missing` fallback, whose end condition is
# `result: failed`.
#
# A RED is a SEPARATE edge to the same target (`{passed: false, written: ...}`),
# and that is a measurement, not a convenience: one AND-matched edge cannot say
# "green requires the verdict" AND "red keeps its route", and keying the red on
# the verdict too moved a MEASURED pytest red beside an unprovisionable Node leg
# (`node_gate_complete: false`, honestly) onto the absence terminal — measured,
# tests/unit/test_measured_red_repair_loop.py went red in three places. A red
# cannot conclude success in any case: `test_outcome` requires `passed: true`.
#
# THE FOUR CONTROLS on that narrowed edge, each driven by the shipped config's
# own resolver through `_route` above (the names are the section below). These
# are the CARD's (a)-(d) and number a DIFFERENT list from the (a)/(b1)/(b2)
# inventory above, which numbers this file's earlier history:
#   (a) test_control_a_a_real_partial_node_gate_still_ends_at_the_partial_terminal
#   (b) test_control_b_a_plain_green_repo_still_reaches_done
#   (c) test_control_c_a_python_only_repo_still_reaches_done
#   (d) test_control_d_a_green_return_without_the_verdict_reaches_no_success
# beside them, two pins that are not controls: the NON-DELIVERY return
# (`written: None`, `passed: false`, no verdict) still lands
# `test_evidence_missing` — no existing test pinned that TRANSITION, see its
# docstring — and the edge TEXT itself
# (test_the_delivery_edge_requires_the_verdict_positively).
# (a) and (b) restate the two shapes already pinned further down this file, now
# as controls on the NARROWED edge: a revert has to show up in both places, and
# the two older tests keep their own history (the defect and its measurement).
#
# THE TWO OTHER CANDIDATE SITES ARE REJECTED BY MEASUREMENT, not preference —
# both re-measured on this tree (2026-11-06), and recorded beside the edge in
# `configs/coding_impl.yaml` as well:
#   S1. THE INLINE SCHEMAS of `test_evidence` / `test_outcome` validate the FILE
#       `$CONFIG_DIR/test/test_report.json`, and the producer writes
#       `report["node_gate_complete"]` only INSIDE `if node is not None:`
#       (aitelier/tools/run_tests/impl.py:3327-3338) — its own comment says it is
#       "Absent when there is no node section at all (a Python-only repository)",
#       and `_run_node_checks` returns None before running anything for such a
#       repository (impl.py:1016-1018). MEASURED: the real producer's report for
#       a repo with no package.json has no `node` key and no
#       `node_gate_complete`, while its RETURN answers `True`. Requiring the key
#       in either schema would refuse every Python-only GREEN round.
#   S2. A TRANSITION IS EVALUATED AGAINST THE EVALUATING STEP'S OWN FLAGS. The
#       engine routes a tool step with `step_flags = tool_result` — that step's
#       return — and `test_evidence` / `test_outcome` are `json_schema` steps
#       whose return is `{all_passed, results}` (skillflow core
#       `_complete_tool_step` -> `resolve_transition`; the native tool's return
#       is `skillflow/tools/json_schema/impl.py:47`). They never see `test`'s
#       RETURN, so a positive `node_gate_complete` match there can never fire: it
#       would send every green round to `implement` and burn the three laps.
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
#                 no node section does not match the `false` edge — and that
#                 `True` IS what the narrowed delivery edge requires, which is
#                 what keeps a Python-only green round reaching `done`
#                 (control (c)). A return that OMITTED the key would route
#                 nowhere (control (d)).
#     :3086       the upstream-blocked fail-fast return carries the verdict and
#                 `passed: false` — cannot match the new edge.
#     :3666       the main return — the value that becomes `test`'s flags.
#     :2868       `_report_write_failure` OMITS the key (the docstring paragraph
#                 at :2877-2882, its payload at :2916-2930), so a non-delivery
#                 return cannot match; it still ends on `test_evidence_missing`.
#     :3371       the flattened `node_unmeasured` (read by gate_evidence below).
#     :3346       `unmeasured_projects = node.get("unmeasured") or []`: the list that
#                 makes `executed.node_gate_ran` false (:3348) and, outside the
#                 nothing-measured branch, becomes `node_unmeasured` (:3371).
#     :3350       `if node.get("skipped") and not node.get("measured_dirs")`: the
#                 nothing-measured branch, which sets the whole-report `skipped`,
#                 `infrastructure_unavailable` and `evidence_state`.
#     :3347       the same two node keys decide `executed.node_gate_ran`.
#     :1146       `_aggregate_node_projects`: `skipped = bool(unmeasured)` — the
#                 invariant behind D1.
#   configs — the graphs that READ the verdict. Six configs invoke `run_tests`;
#   the steps that read none of these three fields are listed at the end.
#     fix_tests.yaml:112-113 / :129   partial -> terminal; success REQUIRES
#                                     `node_gate_complete: true` + `passed: true`.
#     subagent.yaml:124-125 / :139    the same pair.
#     coding_impl.yaml                the new edge `{node_gate_complete: false,
#                                     passed: true}` -> `node_gate_incomplete`
#                                     (same id as the other two, declared
#                                     `failed`). NARROWER than theirs on purpose:
#                                     here the red path is the
#                                     `{passed: false, written: ...}` edge, so a
#                                     one-term edge would capture a measured red
#                                     (control (b1)).
#                                     DIFFERENCE, and it is now CLOSED: the
#                                     refusal above is only the `false` half.
#                                     `done` is reached through a second
#                                     validator step whose flags are its own
#                                     (`all_passed`), so the POSITIVE form
#                                     belongs on `test`'s `written` edge — see
#                                     the delivery-edge paragraph at the top of
#                                     this docstring: `{written: ...}` fired for
#                                     a return that named the report and carried
#                                     no verdict at all, so the absent verdict
#                                     still concluded DONE. It now requires
#                                     `node_gate_complete: true` and an absent
#                                     verdict lands `test_evidence_missing`
#                                     (control (d)). The scripted producer in
#                                     tests/skillflow/test_coding_impl_gate.py
#                                     returns the verdict for the same reason.
#     coding_impl.yaml:422-425 `test_evidence` `node: {skipped: {const: false}}`
#                                     — a config-side reader of `node["skipped"]`:
#                                     a node gate that skipped is refused as
#                                     evidence. Left untouched (see D2); it loses
#                                     the partial-green case to the earlier edge
#                                     and gains nothing.
#     gen_coop_shell_ui60_20260907.yaml:124-127
#                                     the SAME `node: {skipped: {const: false}}`
#                                     clause in a shipped GENERATED config (its
#                                     header still says coding_impl) — a second
#                                     config-side reader of `node.skipped`. Listed
#                                     because an inventory of only the
#                                     hand-written graphs missed it. Unchanged and
#                                     UNAFFECTED: that graph has no
#                                     `node_gate_complete` edge at all, so its
#                                     partial-green route is `test_evidence` ->
#                                     `test_evidence_missing` before and after.
#   aitelier/gate_evidence.py:323 — `report_state` answers `partial` when a GREEN
#     report carries `node_unmeasured` (the FLATTENED key, not `node[...]`, and
#     not `node_gate_complete`: that name occurs once in the module, as prose in
#     its own docstring at :488). Gains, for the REAL producer's partial green:
#     the graph now refuses it at the step that owns the verdict and names the
#     partial gate (`node_gate_incomplete`), where before this edge it was parked
#     one step later as evidence-missing (the `node.skipped` const; see D1). The
#     shape that DID reach `done` before (hand-built or older producer:
#     `node.unmeasured` beside `node.skipped: false`) is closed too. The real
#     producer's report never reached `done`. Loses nothing.
#   core/coding_impl_outcome.py:147-153 — `measurement` reads the NODE leg's
#     `skipped` (and `passed`) through a loop variable, which is why a literal
#     `node["skipped"]` grep does not find it: `for key in ("node", "repo_gate")`
#     -> `leg = report.get(key)` -> `ValueError("unsupported nonpassing leg")`
#     when `leg.get("skipped")` is truthy or `leg.get("passed")` is not True.
#     It measures a candidate's pre-change report (`finish_measurement` ->
#     `measurement`, called from run_tests' prechange path at impl.py:3571, and
#     `coding_impl`'s own `baseline` step sets `prechange_capture: true`), so a
#     report whose node leg was skipped or nonpassing cannot become that
#     baseline — the capture degrades to `prechange_unavailable`. Gains/loses
#     nothing: this card moves which terminal a partial-green run reaches, not
#     one byte of the report this reader parses.
#   core/gate_deferral.py:428 — reads `repo_gate_unmeasured`, a DIFFERENT key
#     (the repository gate, not the node gate). Untouched.
#   NOT node readers (named so the next `skipped` sweep need not re-derive them):
#     core/coding_impl_outcome.py:142-145 and :240-243 and
#     core/release_gate_migration.py:373 read the WHOLE-REPORT `skipped` off
#     test_report.json; core/tool_routes.py:152-159 counts skipped TEST CASES in
#     a session; core/state_driver_guide.py:193 is prose about a `test` node's
#     deliverable, and docs/repo-gate-unmeasured-protocol.md:48 names
#     `node.skipped` in prose.
#   configs that run `run_tests` while reading none of these three fields:
#     coding_impl.yaml:67-81 `baseline` (the prechange capture this card does not
#     touch), dpe_default.yaml:906 `5_test` (one unconditional edge; its verdict
#     is folded later) and addons/game_harness.yaml:475 `5_final_test` (routes on
#     `release_evidence`). No `node_gate_complete` term is added to any of them.
import errno
import json
from pathlib import Path

import skillflow as _skillflow_pkg
import yaml
from skillflow.graph import GraphResolver, PipelineGraph
from skillflow.tool_loader import ToolLoader

import aitelier.tools.run_tests.impl as rt
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


# ════════════════════════════════════════════════════════════════════════════
# THE FOUR CONTROLS ON THE NARROWED DELIVERY EDGE
#
# `test`'s `{written: "test_report.json"} -> test_evidence` edge used to fire for
# any RETURN that NAMED the report, including one carrying no
# `node_gate_complete` at all. Everything downstream (`test_evidence` ->
# `test_outcome` -> `done`) concludes success on `passed: true` alone, so the
# absent verdict still concluded DONE. The edge now requires the verdict
# positively; an absent verdict falls through to the unconditional
# `test_evidence_missing`, declared `failed`.
#
# Every route below is the shipped config's own resolver over
# `configs/coding_impl.yaml`, on a RETURN the REAL `run_tests` produced (only
# the non-delivery pin below hands it a different payload, and that payload is
# the producer's OWN function, not a hand-built dict).
# ════════════════════════════════════════════════════════════════════════════

# ── control (a): a real partial Node gate still ends at the partial terminal ──
def test_control_a_a_real_partial_node_gate_still_ends_at_the_partial_terminal(
        tmp_path, monkeypatch):
    """Nine node projects, no declaration, so the gate visits eight and the
    ninth is a real `capped` absence. The narrowing must not disturb the
    `false` edge: this round still stops at `node_gate_incomplete`, declared
    `failed`, and `implement` is not reachable from it."""
    repo = tmp_path / "repo"
    order = _nine(repo)

    report, ret, out = _run(tmp_path, monkeypatch, repo)

    assert [e["dir"] for e in report["node_unmeasured"]] == [order[8]]
    assert report["node_gate_complete"] is False
    assert ret["node_gate_complete"] is False
    assert ret["passed"] is True

    path = _route(out, ret)

    assert path == [PARTIAL_TARGET], path
    assert "done" not in path, path
    assert _terminal_declared(PipelineGraph.from_yaml(CONFIG), PARTIAL_TARGET) == "failed"


# ── control (b): a plain green repo still reaches `done` ─────────────────────
def test_control_b_a_plain_green_repo_still_reaches_done(tmp_path, monkeypatch):
    """Every node project measured and green. It reaches `done` BECAUSE the
    return states the completeness verdict — the term the narrowed edge added."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, ret, out = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert "node_unmeasured" not in report
    assert ret["node_gate_complete"] is True
    assert ret["written"] == "test_report.json"

    assert _route(out, ret) == ["test_evidence", "test_outcome", "done"]


# ── control (c): a Python-only repo still reaches `done` ────────────────────
def test_control_c_a_python_only_repo_still_reaches_done(tmp_path, monkeypatch):
    """No `package.json` anywhere: `_run_node_checks` returns None before
    running anything (impl.py:1016-1018), so the report has NO `node` section
    and NO `node_gate_complete` — the producer's own comment says the key is
    "Absent when there is no node section at all (a Python-only repository)".

    Requiring the verdict in the FILE would therefore refuse this green round
    (rejected site S1). THIS CONTROL IS NOT SATISFIABLE BY AN ABSENCE: the
    RETURN answers for the repository, as a real boolean `True`, and that is
    what the narrowed edge matches. A return that omitted it would route nowhere
    (control (d))."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")

    report, ret, out = _run(tmp_path, monkeypatch, repo)

    # The absence, asserted rather than assumed.
    assert "node" not in report
    assert "node_gate_complete" not in report
    assert report["passed"] is True, report["failures"]
    # And the producer's own answer on the payload the engine routes with.
    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)

    assert _route(out, ret) == ["test_evidence", "test_outcome", "done"]


# ── control (d): the green RETURN without the verdict reaches no success ────
def test_control_d_a_green_return_without_the_verdict_reaches_no_success(
        tmp_path, monkeypatch):
    """The defect itself, driven: a fully measured GREEN round whose RETURN
    carries no `node_gate_complete` — a stale producer, a run spanning a deploy,
    a hand-made report.

    The verdict is DELETED from the RETURN only; the FILE still has it, which is
    the point: this config routes on the payload the engine hands the step, and
    the file's copy is not read here (rejected site S2 is the same fact from the
    other end). REVERT MEASUREMENT: with the old `{written: "test_report.json"}`
    edge this walk is `['test_evidence', 'test_outcome', 'done']` — success — and
    this test is the one that goes red."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, ret, out = _run(tmp_path, monkeypatch, repo)
    assert report["node_gate_complete"] is True          # the file has it
    assert ret["node_gate_complete"] is True             # the producer wrote it

    del ret["node_gate_complete"]

    path = _route(out, ret)

    assert path == ["test_evidence_missing"], path
    assert "done" not in path, path
    assert "test_evidence" not in path, path
    assert _terminal_declared(PipelineGraph.from_yaml(CONFIG),
                              "test_evidence_missing") == "failed", (
        "the fall-through must END the run as failed, not park it or conclude")


# ── beside the controls: the non-delivery RETURN is still an absence ────────
def test_the_non_delivery_return_still_lands_on_test_evidence_missing(
        tmp_path, monkeypatch):
    """A report that could not be STORED: `written: None`, `passed: false`, and
    deliberately no `node_gate_complete` (impl.py `_report_write_failure`, whose
    docstring says so and whose payload is used here — not hand-built).

    No existing test pins the TRANSITION for this shape:
    tests/skillflow/test_coding_impl_gate.py::
    test_failed_tool_return_cannot_consume_existing_passing_artifact drives a
    hand-built `written: None` return through the ENGINE and pins the step-
    FAILURE path (the tool's `error` fails the step), not which edge the config
    takes. So the route is pinned here."""
    repo = tmp_path / "repo"
    _nine(repo)
    report, _, out = _run(tmp_path, monkeypatch, repo)

    ret = rt._report_write_failure(
        dict(report), out, "write",
        OSError(errno.ENOSPC, "No space left on device"))

    assert ret["written"] is None
    assert ret["passed"] is False
    assert "node_gate_complete" not in ret

    assert _route(out, ret) == ["test_evidence_missing"]


# ── the artifact half of the narrowing ─────────────────────────────────────
def test_the_delivery_edge_requires_the_verdict_positively():
    """The edge text itself, so a silently reverted match is a red test rather
    than a behaviour nobody re-measured."""
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in data["steps"]}
    edges = steps["test"]["transitions"]

    delivery = [e for e in edges if (e.get("match") or {}).get("written")]
    assert len(delivery) == 2, delivery
    matches = [e["match"] for e in delivery]
    assert {"node_gate_complete": True, "written": "test_report.json"} in matches, (
        "a DELIVERED GREEN round must state the completeness verdict: without "
        "this term a return that merely NAMED the report reached test_evidence "
        "and concluded DONE on `passed: true` alone")
    assert {"passed": False, "written": "test_report.json"} in matches, (
        "a DELIVERED RED keeps its route to the evidence validators, which own "
        "the measured-red acceptance: keying the red on the completeness verdict "
        "instead moved a measured red beside an unmeasured Node leg onto the "
        "absence terminal (measured: tests/unit/"
        "test_measured_red_repair_loop.py went red in three places)")
    for match in matches:
        assert match["written"] == "test_report.json", match

    # The fall-through it relies on is still UNCONDITIONAL and still last: that
    # is the terminal an absent verdict reaches.
    assert edges[-1].get("match") is None and edges[-1]["to"] == "test_evidence_missing"
    assert _terminal_declared(PipelineGraph.from_yaml(CONFIG),
                              "test_evidence_missing") == "failed"
