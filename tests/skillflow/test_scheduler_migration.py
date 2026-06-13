"""Tests for the skillflow-based scheduler."""

import json
import pytest
from unittest.mock import MagicMock, patch

from skillflow.core import SkillFlow, StepResult, ClaimedStep, ClaimToken
from skillflow.graph import PipelineGraph, StepNode, Transition, EndCondition, EndConditions


def _agent(id: str, transitions=None, checkpoint=False):
    return StepNode(
        id=id, step_type="agent",
        transitions=transitions or [],
        checkpoint=checkpoint,
    )


def _trans(to: str, match=None, max_loop=None):
    return Transition(to=to, match=match, max_loop=max_loop)


def _simple_dpe_graph():
    return PipelineGraph(
        name="dpe_default", begin="1_5",
        steps=[
            _agent("1_5", [_trans("2")], checkpoint=True),
            _agent("2", [_trans("3")], checkpoint=True),
            _agent("3", [_trans("done")]),
            _agent("done", []),
        ],
        end_conditions=EndConditions(
            conditions=[EndCondition(type="node_reached", node="done", result="completed")],
        ),
    )


def test_get_or_create_run(sf: SkillFlow):
    """A skillflow run is created for a project."""
    graph = _simple_dpe_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_default", {"project_id": "test-proj"})
    assert run_id is not None
    sf.start_run(run_id)

    # Advance to first step
    next_node = sf.advance_run(run_id)
    assert next_node == "1_5"


def test_pipeline_completes(sf: SkillFlow):
    """A simple pipeline runs from start to completion."""
    graph = _simple_dpe_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_default", {"project_id": "test-proj"})
    sf.start_run(run_id)

    steps_executed = []
    while True:
        n = sf.advance_run(run_id)
        if n is None:
            run = sf.get_run(run_id)
            if run["status"] == "paused":
                sf.resume_run(run_id)
                continue
            # completed or failed
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        steps_executed.append(claimed.step_id)
        sf.confirm_step(claimed.token, StepResult(flags={}))

    run = sf.get_run(run_id)
    assert run["status"] == "completed", f"Expected completed, got {run['status']}"
    assert "1_5" in steps_executed
    assert "2" in steps_executed
    assert "3" in steps_executed
    # "done" triggers end_condition node_reached → run completes without executing


def test_checkpoint_pause_and_resume(sf: SkillFlow):
    """Pipeline pauses at checkpoint and resumes."""
    graph = PipelineGraph(
        name="dpe_default", begin="a",
        steps=[
            _agent("a", [_trans("b")], checkpoint=True),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_default", {"project_id": "test"})
    sf.start_run(run_id)

    # Execute step a
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())

    # Checkpoint pauses
    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "paused"

    # Resume
    sf.resume_run(run_id)
    n = sf.advance_run(run_id)
    assert n == "b"


def test_runner_integration_with_skillflow(sf: SkillFlow):
    """Skillflow and runner flags produce correct transitions."""
    graph = PipelineGraph(
        name="dpe_default", begin="a",
        steps=[
            _agent("a", [_trans("g")]),
            StepNode(id="g", step_type="gate", transitions=[
                _trans("b", match={"go": True}),
                _trans("c", match={"go": False}),
            ]),
            _agent("b", []),
            _agent("c", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_default", {"project_id": "test"})
    sf.start_run(run_id)

    # Step a with flag go=True → gate → b
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={"go": True}))
    next_node = sf.advance_run(run_id)
    assert next_node == "b"


def test_failed_project_maps_to_failed_run(sf: SkillFlow):
    """When skillflow run fails, project can detect it."""
    graph = PipelineGraph(
        name="dpe_default", begin="a",
        steps=[_agent("a", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_default", {"project_id": "test"})
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    # Fail with no error transition → run fails
    sf.fail_step(claimed.token, "fatal error", retryable=False)

    run = sf.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "fatal error"
