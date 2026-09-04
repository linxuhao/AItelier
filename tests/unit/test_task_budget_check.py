"""Unit tests for the task_budget_check gate.

The defect it exists for: on `boltons` the PM emitted 33 tasks, the 5_review
goal loop appended 21 more, and the run was killed by `max_total_steps: 200`
having discarded every task the loop had already implemented. The arithmetic
(4 steps per task + the linear chain) was computable the moment the manifest
existed, so a deterministic gate now does it between the PM's reviewer and the
loop.

Covers: the step accounting derived from the real graph, a list that fits, a
list that does not (and whether the "cut to K" it recommends actually fits),
the degenerate counts, and the never-fail fallbacks. Plus the graph wiring —
the gate is worthless if 3_review no longer routes through it.
"""

import json
from pathlib import Path

import pytest
from skillflow.graph import GraphResolver, PipelineGraph

from aitelier.tools.task_budget_check.impl import (
    _graph_shape, _largest_fitting_count, _required_steps, task_budget_check)

ROOT = Path(__file__).resolve().parents[2]
DPE_CONFIG = ROOT / "configs" / "dpe_default.yaml"
GRAPH = "dpe_default_v2"


@pytest.fixture
def dpe_graph():
    return PipelineGraph.from_yaml(DPE_CONFIG)


@pytest.fixture
def live(monkeypatch, dpe_graph):
    """Serve the real dpe_default graph in place of the registry lookup."""
    import aitelier.tools.task_budget_check.impl as impl
    monkeypatch.setattr(impl, "_live_graph", lambda config_name: dpe_graph)
    return dpe_graph


def _workspace(tmp_path, tasks, *, field="execution_order") -> Path:
    """A workspace holding the PM's manifest; returns the gate's $STEP_DIR."""
    gdir = tmp_path / "ws" / GRAPH
    (gdir / "3").mkdir(parents=True, exist_ok=True)
    (gdir / "3" / "tasks_manifest.json").write_text(
        json.dumps({field: tasks}), encoding="utf-8")
    return gdir / "3_budget"


def _call(out_dir) -> dict:
    return task_budget_check(out_dir=str(out_dir), config_name=GRAPH)


def _tasks(n, prefix="t"):
    """One wave of n task ids, in the list-of-lists shape the PM emits."""
    return [[f"{prefix}{i}" for i in range(n)]]


GRAPH_BUDGET = _graph_shape(GRAPH)["budget"]


def _first_overflowing_count() -> int:
    """The smallest task count the CURRENT cap cannot carry through one fix
    round. Derived, so raising or lowering the cap moves the test with it."""
    shape = _graph_shape(GRAPH)
    n = 1
    while _required_steps(shape, n) <= shape["budget"]:
        n += 1
    return n


# ── Step accounting ────────────────────────────────────────────────────────

def test_shape_is_derived_from_the_real_graph(live):
    """Hardcoding 4 and 13 would go wrong the moment an addon splices a step in."""
    shape = _graph_shape(GRAPH)
    assert shape["body"] == 4, "the task loop body is t_plan/t_plan_review/t_impl/t_impl_review"
    # Every agent/tool node outside the body, plus the loop node itself (it is
    # marked completed when it drains). Gates leave no row and are excluded.
    # 14 since git_push_post joined the chain (2026-08-27): a finished round
    # pushes to its remote branch before `done`.
    assert shape["linear"] == 14
    # Read, never pinned: the cap is an operational knob (200 -> 300 on
    # 2026-09-04, when a legitimate 21-card round died at step 204 inside the
    # final verification chain). A test that pins the number turns every future
    # adjustment into a red test that says nothing about the gate.
    assert shape["budget"] == GRAPH_BUDGET
    assert shape["source"] == {"step": "3", "file": "tasks_manifest.json",
                               "field": "execution_order"}


def test_the_boltons_list_is_the_one_that_did_not_fit(live):
    """The boltons shape: a clean pass is linear + 4 per task; one fix round
    is what overflows. 33 tasks measured 146 clean / 248 with the fix round
    against the cap of 200 that was in force then."""
    shape = _graph_shape(GRAPH)
    assert shape["linear"] + shape["body"] * 33 == 146
    assert _required_steps(shape, 33) == 248


# ── The verdict ────────────────────────────────────────────────────────────

def test_a_list_that_fits_passes(tmp_path, live):
    out = _workspace(tmp_path, _tasks(10))
    result = _call(out)
    assert result["within_budget"] is True
    assert result["task_count"] == 10
    assert result["required_steps"] == _required_steps(_graph_shape(GRAPH), 10)
    assert result["max_total_steps"] == GRAPH_BUDGET


def test_a_list_that_does_not_fit_is_rejected_with_the_numbers(tmp_path, live):
    n = _first_overflowing_count()
    out = _workspace(tmp_path, _tasks(n))
    result = _call(out)
    assert result["within_budget"] is False
    assert result["task_count"] == n
    assert result["required_steps"] == _required_steps(_graph_shape(GRAPH), n)
    assert result["max_total_steps"] == GRAPH_BUDGET
    assert result["max_tasks"] == n - 1
    # The PM only ever sees `reason` (it rides the feedback banner), so the
    # numbers have to be in it.
    for fragment in (str(n), str(result["required_steps"]),
                     str(GRAPH_BUDGET), str(result["max_tasks"])):
        assert fragment in result["reason"]


def test_the_recommended_count_actually_fits(tmp_path, live):
    """A recommendation that overshoots would just re-run the same death."""
    shape = _graph_shape(GRAPH)
    result = _call(_workspace(tmp_path, _tasks(_first_overflowing_count())))
    k = result["max_tasks"]
    assert _required_steps(shape, k) <= shape["budget"]
    assert _required_steps(shape, k + 1) > shape["budget"]


def test_the_recommendation_never_exceeds_what_the_pm_asked_for(live):
    """`max_tasks` is advice for cutting a list, never for padding one."""
    shape = _graph_shape(GRAPH)
    assert _largest_fitting_count(shape, shape["budget"], 3) == 3


def test_the_report_is_written_next_to_the_step(tmp_path, live):
    out = _workspace(tmp_path, _tasks(33))
    result = _call(out)
    assert json.loads((out / "budget_report.json").read_text(encoding="utf-8")) == result


def test_waves_are_flattened_and_deduped(tmp_path, live):
    """The loop iterates flattened items; a task named twice runs once."""
    out = _workspace(tmp_path, [["a", "b"], ["b", "c"]])
    assert _call(out)["task_count"] == 3


# ── Degenerate counts ──────────────────────────────────────────────────────

def test_an_empty_task_list_passes(tmp_path, live):
    result = _call(_workspace(tmp_path, []))
    assert result["within_budget"] is True
    assert result["task_count"] == 0


def test_a_single_task_passes(tmp_path, live):
    result = _call(_workspace(tmp_path, _tasks(1)))
    assert result["within_budget"] is True
    assert result["task_count"] == 1


def test_a_manifest_with_the_wrong_field_passes(tmp_path, live):
    """Not this gate's business: 3_review reviews the manifest's shape."""
    out = _workspace(tmp_path, _tasks(33), field="tasks")
    assert _call(out)["within_budget"] is True


def test_the_smallest_list_that_overshoots_is_still_rejected(tmp_path, live):
    """Off-by-one at the boundary decides whether a run dies at the cap."""
    shape = _graph_shape(GRAPH)
    fits = _largest_fitting_count(shape, shape["budget"], 100)
    assert _call(_workspace(tmp_path, _tasks(fits)))["within_budget"] is True
    over = _call(_workspace(tmp_path, _tasks(fits + 1)))
    assert over["within_budget"] is False
    assert over["max_tasks"] == fits


# ── Never fails the run ────────────────────────────────────────────────────

def test_an_unresolvable_graph_passes(tmp_path, monkeypatch):
    import aitelier.tools.task_budget_check.impl as impl
    monkeypatch.setattr(impl, "_live_graph", lambda config_name: None)
    result = _call(_workspace(tmp_path, _tasks(33)))
    assert result["within_budget"] is True
    assert "not resolvable" in result["reason"]


def test_a_graph_without_max_total_steps_passes(tmp_path, monkeypatch, dpe_graph):
    import aitelier.tools.task_budget_check.impl as impl
    dpe_graph.end_conditions.conditions = [
        c for c in dpe_graph.end_conditions.conditions
        if c.type != "max_total_steps"]
    monkeypatch.setattr(impl, "_live_graph", lambda config_name: dpe_graph)
    result = _call(_workspace(tmp_path, _tasks(33)))
    assert result["within_budget"] is True
    assert "no max_total_steps" in result["reason"]


def test_a_missing_manifest_passes(tmp_path, live):
    out = tmp_path / "ws" / GRAPH / "3_budget"
    out.mkdir(parents=True)
    result = _call(out)
    assert result["within_budget"] is True
    assert "not readable" in result["reason"]


def test_an_unparseable_manifest_passes(tmp_path, live):
    out = _workspace(tmp_path, _tasks(33))
    (out.parent / "3" / "tasks_manifest.json").write_text("{not json", encoding="utf-8")
    assert _call(out)["within_budget"] is True


def test_no_out_dir_still_returns_a_verdict(live):
    """$STEP_DIR is always injected; a caller without one must not crash."""
    assert task_budget_check(config_name=GRAPH)["within_budget"] is True


# ── Graph wiring ───────────────────────────────────────────────────────────

def _resolver():
    return GraphResolver(PipelineGraph.from_yaml(DPE_CONFIG))


def test_the_approved_task_breakdown_routes_into_the_gate():
    """The PM's checkpoint used to hand straight to 3_review."""
    assert _resolver().next_node("3", {"_checkpoint_approved": True}, {}) == "3_budget"


def test_the_gate_routes_on_its_own_flag():
    resolver = _resolver()
    assert resolver.next_node("3_budget", {"within_budget": True}, {}) == "3_review"
    assert resolver.next_node("3_budget", {"within_budget": False}, {}) == "3"


def test_the_gate_gives_up_forward_once_the_replan_rounds_are_spent():
    """An exhausted reject edge must not dead-end a run that has work to do."""
    resolver = _resolver()
    counts = {("3_budget", "3"): 2}
    assert resolver.next_node("3_budget", {"within_budget": False}, counts) == "3_review"


def test_no_gate_edge_points_at_a_loop_node():
    """An inline tool step routes through `_complete_tool_step`, which sets
    current_node and returns it WITHOUT resolving a loop — only advance_run's own
    paths do that. An edge from here to `task_loop` therefore gets the loop id
    claimed as an agent step, which is how the first placement of this gate
    stalled the whole task phase."""
    graph = PipelineGraph.from_yaml(DPE_CONFIG)
    loops = {s.id for s in graph.steps if s.step_type == "loop"}
    gate = next(s for s in graph.steps if s.id == "3_budget")
    assert loops and not ({t.to for t in gate.transitions} & loops)


def test_the_rejection_reaches_the_pm_as_feedback():
    """Without `feedback: true` the PM re-plans blind — it has no context source
    on this step, so the banner is the only channel the numbers travel on."""
    node = _resolver().get_node("3_budget")
    reject = next(t for t in node.transitions if t.to == "3")
    assert reject.feedback is True


# ── stale ids: a re-plan made only of already-dispatched ids runs nothing ──────

def test_a_manifest_of_only_already_dispatched_ids_is_rejected(tmp_path, live, monkeypatch):
    import aitelier.tools.task_budget_check.impl as impl
    monkeypatch.setattr(impl, "_completed_loop_items", lambda pid: {"t0", "t1"})
    out = _workspace(tmp_path, _tasks(2))
    r = task_budget_check(out_dir=str(out), config_name=GRAPH, project_id="p")
    assert r["within_budget"] is False
    assert r["stale_ids"] == ["t0", "t1"]
    assert "NEW id" in r["reason"]


def test_a_manifest_with_one_new_id_passes_and_names_the_skipped(tmp_path, live, monkeypatch):
    import aitelier.tools.task_budget_check.impl as impl
    monkeypatch.setattr(impl, "_completed_loop_items", lambda pid: {"t0"})
    out = _workspace(tmp_path, _tasks(2))
    r = task_budget_check(out_dir=str(out), config_name=GRAPH, project_id="p")
    assert r["within_budget"] is True
    assert r["stale_ids"] == ["t0"]
    assert "skipped: t0" in r["reason"]


def test_completed_loop_items_reads_the_live_run_by_project(monkeypatch):
    import sqlite3
    from unittest.mock import patch
    from aitelier.tools.task_budget_check.impl import _completed_loop_items
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE skillflow_runs (id TEXT, project_id TEXT, status TEXT)")
    conn.execute("CREATE TABLE skillflow_loop_state (run_id TEXT, loop_step_id TEXT, completed_items TEXT)")
    conn.execute("INSERT INTO skillflow_runs VALUES ('live', 'p', 'paused')")
    conn.execute("INSERT INTO skillflow_runs VALUES ('old', 'p', 'completed')")
    conn.execute("INSERT INTO skillflow_loop_state VALUES ('live', 'task_loop', '[\"a\", \"b\"]')")
    conn.execute("INSERT INTO skillflow_loop_state VALUES ('old', 'task_loop', '[\"z\"]')")

    class _SF:
        _conn = conn

    with patch("api.dependencies.get_skillflow", return_value=_SF()):
        assert _completed_loop_items("p") == {"a", "b"}
        assert _completed_loop_items("other") == set()
    assert _completed_loop_items("") == set()
