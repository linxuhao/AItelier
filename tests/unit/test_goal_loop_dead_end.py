"""Regression: a goal loop that has spent its budget must STAY dead.

NL2Repo benchmark, 2026-08-17, task `arxiv-mcp-server`: dpe_default's `5_review`
kept returning passed:false after the `5_review → 3` goal-loop edge had spent its
`max_loop: 2`. skillflow did the right thing — it failed the run with "Cycle limit
exceeded … '5_review' -> '3' (max_loop=2 reached)". The HOST then undid it: for a
failed run `_get_checkpoint_info` falls back to "the most recent completed
checkpoint step" (step 3, approved 100 minutes earlier), the harness's
auto-approver approved that phantom, and `approve_checkpoint`'s failed-run branch
called `reactivate_run` + `resume_run`, which re-opened `5_review` — the step named
in error_reason — and ran it again. 229 executions of one step over 100 minutes,
227 of them recycling a single step-instance row, ended only by the 3-hour wall
clock. Approving changes nothing a re-run can act on: a step's own re-execution
can never lower an edge count, so the same verdict hits the same spent edge.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from skillflow.exceptions import CycleLimitExceeded

DPE_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "dpe_default.yaml"

# The reason string skillflow actually wrote for this run (skillflow_runs.error_reason,
# truncated); the host's terminal test has to recognise the real text, not a paraphrase.
LIVE_REASON = (
    "Cycle limit exceeded — review_verdict.json feedback: BLOCKING: 4 unit tests FAIL "
    "(edges: All transitions from '5_review' are exhausted: "
    "'5_review' -> '3' (max_loop=2 reached))"
)


def _dpe_resolver():
    from skillflow.graph import GraphResolver, PipelineGraph
    return GraphResolver(PipelineGraph.from_yaml(DPE_CONFIG))


def _goal_loop_budget(resolver) -> int:
    """How many times the shipped config lets `5_review` send the run back to 3.

    Read from the graph, never restated. This test used to hardcode 2, so
    raising the edge's `max_loop` to 4 — a deliberate change, because three
    dpe_game runs died one repair pass short of converging — broke a test about
    something else entirely. What is being pinned here is that a SPENT budget
    dead-ends, whatever the budget happens to be.
    """
    for node in resolver.graph.steps:
        if node.id != "5_review":
            continue
        for t in node.transitions:
            if t.to == "3" and t.max_loop is not None:
                return t.max_loop
    raise AssertionError(
        "the 5_review -> 3 goal-loop edge, or its max_loop, is gone from "
        f"{DPE_CONFIG.name} — this regression no longer has a subject")


def test_spent_goal_loop_dead_ends_in_the_shipped_config():
    """The precondition, over the real graph: there IS no edge left to take.

    Not a defect — the goal loop's budget is the bound, and skillflow raising here
    is the run ending. The bug was everything the host did afterwards.
    """
    resolver = _dpe_resolver()
    failing = lambda p: json.dumps({"passed": False})   # noqa: E731
    budget = _goal_loop_budget(resolver)
    assert budget >= 1, "a goal loop with no budget is not a goal loop"

    # Unspent: the failing verdict routes back to the PM, which is the loop.
    assert resolver.next_node("5_review", {}, {}, file_reader=failing) == "3"
    # One short of the budget: still open — the bound must not fire early.
    assert resolver.next_node("5_review", {}, {("5_review", "3"): budget - 1},
                              file_reader=failing) == "3"
    # Spent: there is no edge left to take, and skillflow says so.
    with pytest.raises(CycleLimitExceeded):
        resolver.next_node("5_review", {}, {("5_review", "3"): budget},
                           file_reader=failing)


def test_passing_verdict_still_reaches_done_with_the_loop_spent():
    """The terminal test must not shadow the one edge that is still open.

    The spent count comes from the config for the same reason as above, and
    here it was load-bearing in a quieter way: hardcoded at 2 against a budget
    of 4 this test still passed, while no longer testing what its name says —
    the loop it claimed was spent had two firings left.
    """
    resolver = _dpe_resolver()
    passing = lambda p: json.dumps({"passed": True})    # noqa: E731
    spent = {("5_review", "3"): _goal_loop_budget(resolver)}
    assert resolver.next_node(
        "5_review", {}, spent, file_reader=passing) == "done"


# ── host side: a routing failure must not be offered as a checkpoint ──────────

def _sf_stub(status: str, reason: str = ""):
    """A skillflow stub with dpe_default's real resolver and a plausible history."""
    sf = MagicMock()
    sf.get_run_by_project.return_value = {
        "id": "run1", "graph_name": "dpe_default_v2", "status": status,
        "current_node": "5_review", "error_reason": reason,
    }
    sf._get_resolver.return_value = _dpe_resolver()
    sf.get_steps.return_value = [
        {"step_id": "3", "status": "completed"},        # the only checkpoint step
        {"step_id": "5", "status": "completed"},
        {"step_id": "5_review", "status": "completed"},
    ]
    return sf


@pytest.fixture
def checkpoint_info(monkeypatch):
    from api import meta_routers

    def _call(status, reason=""):
        monkeypatch.setattr(meta_routers, "get_skillflow",
                            lambda: _sf_stub(status, reason))
        return meta_routers._get_checkpoint_info("p1")
    return _call


def test_routing_dead_end_offers_no_checkpoint(checkpoint_info):
    """No phantom checkpoint → nothing to approve → no resurrection."""
    assert checkpoint_info("failed", LIVE_REASON) == ("", "", "", "")


def test_unmatched_transition_is_a_dead_end_too(checkpoint_info):
    """The other routing terminal skillflow writes (advance_run's dead end)."""
    assert checkpoint_info(
        "failed", "No matching transition from '5_review' with flags {}") == ("", "", "", "")


def test_other_failures_keep_the_rescue_checkpoint(checkpoint_info):
    """A3 is preserved: a crash IS worth re-running, so it still offers step 3."""
    step_id, _label, run_id, _graph = checkpoint_info(
        "failed", "Step '5_review' timed out after 300s")
    assert (step_id, run_id) == ("3", "run1")


def test_paused_checkpoint_is_unaffected(checkpoint_info):
    """The normal path — a paused run has no error_reason at all."""
    step_id, _label, run_id, _graph = checkpoint_info("paused")
    assert (step_id, run_id) == ("3", "run1")


# ── general guard: one instance re-executed forever is a resumed terminal ─────

@pytest.fixture
def tick_sf(monkeypatch):
    """A skillflow stub wired past everything the tick does before the valve."""
    from core import scheduler
    sf = MagicMock()
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *a: None)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    return sf


def _trace(total: int, worst: tuple[str, int, int] | None):
    def query(run_id, sql, params=()):
        return [list(worst)] if "GROUP BY" in sql else [[total]]
    return query


async def test_one_instance_re_executed_forever_fails_the_run(tick_sf):
    """5_review instance 256: 227 claims, while the whole-run valve sat at 301/300."""
    from core import scheduler
    tick_sf.trace_query.side_effect = _trace(280, ("5_review", 256, 227))

    await scheduler._run_skillflow_tick("p1", None)

    tick_sf.fail_run.assert_called_once()
    run_id, reason = tick_sf.fail_run.call_args[0]
    assert run_id == "run1"
    assert "5_review" in reason and "227" in reason


async def test_a_normal_task_loop_does_not_trip_the_guard(tick_sf):
    """A 40-task run claims t_impl 40 times — across 40 INSTANCES, one claim each."""
    from core import scheduler
    tick_sf.trace_query.side_effect = _trace(200, ("t_impl", 512, 1))

    await scheduler._run_skillflow_tick("p1", None)

    tick_sf.fail_run.assert_not_called()


# ── The reviewer must be able to see what the user demanded ─────────────────
def _dpe_steps():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = yaml.safe_load((root / "configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in d["steps"]}


def test_every_checkpointed_maker_has_a_reviewer_that_reads_its_reject_feedback():
    """A reviewer downstream of a checkpoint enforces "every blocking issue must
    have a NEW repair task" — while the one message that can RETIRE a blocking
    issue (the user's checkpoint rejection) went only to the maker. The two
    deadlock, and the deadlock burns the plan loop until the run dies.

    jinyong-encounter 2026-08-23: two of 5_review's three blocking issues were
    gate defects fixed outside the repo. The user rejected the plan and said,
    with reasons, to drop the task for one of them; the PM complied; 3_review
    then failed the run for covering "only ONE of the three hard-gate failures".
    `3_review -> 3` hit 3/3 with `5_review -> 3` still at 2 of 4 — the goal loop
    had budget left and never got to use it.
    """
    steps = _dpe_steps()
    checkpointed = [sid for sid, s in steps.items() if s.get("checkpoint")]
    assert checkpointed, "no checkpoints in dpe_default — this test is vacuous"

    for maker in checkpointed:
        reviewer = f"{maker}_review"
        assert reviewer in steps, f"{maker} is checkpointed but has no {reviewer}"
        sources = [c.get("source", c) for c in (steps[reviewer].get("context") or [])]
        assert any(src.get("feedback_of") == maker for src in sources), (
            f"{reviewer} judges {maker} but cannot see the user's rejection of "
            f"{maker} — it will enforce rules against instructions it never read")
