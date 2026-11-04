"""The deployment gate reads a real SkillFlow database across projects."""
from __future__ import annotations

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core import deployment_quiescence as dq


def test_real_skillflow_cross_project_measurement_then_quiescence(tmp_path):
    sf = SkillFlow(str(tmp_path / "skillflow.sqlite"))
    sf.register_graph(PipelineGraph(name="fixture", begin="work",
                                    steps=[StepNode(id="work")]))
    run_a = sf.create_run("fixture", project_id="project-a")
    run_b = sf.create_run("fixture", project_id="project-b")
    sf.start_run(run_a)
    sf.start_run(run_b)

    # Which runs BLOCK a restart is measured, not assumed (core/deployment
    # _quiescence._runtime_inventory, 2026-10-07): a run that owns no admitted
    # operation and has no live/lost/unknown process is RESUME work — the host
    # rebuilds it from its trace after a restart — so it is reported in
    # `resumable_runs` and does NOT block. A run that owns an admitted operation
    # still blocks, and that is the protection this test has to keep asserting.
    # Hold a real operation on run A (the same admission the host's own tool
    # steps take) and let run B stay truly idle.
    held = sf._admit_op("run_tests", run_a, detail="deployment-gate fixture")

    active = dq.measure(skillflow=sf, external_probe=list)
    assert active["digest"] == dq._digest(
        {key: value for key, value in active.items() if key != "digest"})
    assert dq._validate_observation(active) is None
    assert active["projects"] == ["project-a", "project-b"]
    assert {row["run_id"] for row in active["blockers"]["active_runs"]} == {run_a}
    # …and the idle one is measured as work a restart would resume, not dropped.
    assert {row["run_id"] for row in active["resumable_runs"]} == {run_b}
    assert active["quiescent"] is False

    sf._retire_op(held)
    sf.fail_run(run_a, "fixture stop")
    sf.fail_run(run_b, "fixture stop")
    quiet = dq.measure(skillflow=sf, external_probe=list)
    assert quiet["projects"] == ["project-a", "project-b"]
    assert quiet["blockers"]["active_runs"] == []
    assert quiet["resumable_runs"] == []
    assert quiet["quiescent"] is True
    assert dq._validate_observation(quiet) is None
    clearance = dq.authorize(
        "restart", quiet, journal=tmp_path / "deployment-journal.json")
    dq.finalize(clearance, success=True,
                journal=tmp_path / "deployment-journal.json")
    sf._conn.close()
