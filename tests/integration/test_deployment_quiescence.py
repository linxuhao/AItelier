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

    active = dq.measure(skillflow=sf, external_probe=list)
    assert active["projects"] == ["project-a", "project-b"]
    assert {row["run_id"] for row in active["blockers"]["active_runs"]} == {run_a, run_b}
    assert active["quiescent"] is False

    sf.fail_run(run_a, "fixture stop")
    sf.fail_run(run_b, "fixture stop")
    quiet = dq.measure(skillflow=sf, external_probe=list)
    assert quiet["projects"] == ["project-a", "project-b"]
    assert quiet["blockers"]["active_runs"] == []
    assert quiet["quiescent"] is True
    sf._conn.close()
