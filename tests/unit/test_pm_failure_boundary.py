"""PM failure must not bypass its checkpoint, budget and review gates.

Real base/addon graph transitions and SkillFlow state machine; upstream content
assembly/validation is replaced only to enter the PM boundary without models.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import yaml
from skillflow import SkillFlow, PipelineGraph
from skillflow.core import StepResult
from skillflow.graph import Transition
from skillflow.workspace import WorkspaceManager
from core import scheduler
from core.dpe_pipeline import NativeTurnBudgetExhausted, NativeOutputCapExhausted

ROOT = Path(__file__).resolve().parents[2]


def engine(tmp_path, game, old_edge=False):
    sf = SkillFlow(":memory:")
    for path in (ROOT / "agent_configs").glob("*.yaml"):
        for name, cfg in (yaml.safe_load(path.read_text()) or {}).items():
            sf.register_agent_config_from_dict(name, cfg)
    sf.register_graph(PipelineGraph.from_yaml(ROOT / "configs/dpe_default.yaml"))
    name = "dpe_default_v2"
    if game:
        spec = yaml.safe_load((ROOT / "configs/addons/game_harness.yaml").read_text())
        sf.register_overlay("game_harness", spec)
        sf.compose_config(name, ["game_harness"], name="dpe_game")
        name = "dpe_game"
    graph = deepcopy(sf._graphs[name])
    graph.name = "pm_boundary"
    pm = next(n for n in graph.steps if n.id == "3")
    # Isolate the real transition/approval seam from agent content production.
    pm.context = []
    pm.validation = []
    pm.output = {}
    pm.output_mode = "write"
    if old_edge:
        pm.transitions = [t for t in pm.transitions if not (t.match or {}).get("_error")]
        pm.transitions.append(Transition(to="task_loop", match={"_error": True}))
    sf.register_graph(graph)
    sf._workspace = WorkspaceManager(str(tmp_path / "workspace"))
    run = sf.create_run(graph.name, {"project_id": "p"}, project_id="p")
    sf.start_run(run)
    # Temporary fixture setup: preceding phases are outside this PM seam.
    sf._conn.execute("UPDATE skillflow_steps SET status='completed' WHERE run_id=? AND step_id IN ('git_sync_pre','gh_scaffold','1','1_review','2','2_review')", (run,))
    sf._conn.execute("UPDATE skillflow_runs SET current_node='3' WHERE id=?", (run,))
    sf._conn.commit()
    return sf, run


def row(sf, run, step):
    return sf._conn.execute(
        "SELECT * FROM skillflow_steps WHERE run_id=? AND step_id=? ORDER BY id DESC LIMIT 1",
        (run, step)).fetchone()


@pytest.mark.parametrize("game", [False, True], ids=["base", "game"])
@pytest.mark.parametrize("error", [NativeTurnBudgetExhausted, NativeOutputCapExhausted])
async def test_scheduler_exhaustion_fails_closed(tmp_path, monkeypatch, game, error):
    sf, run = engine(tmp_path, game)
    calls = []
    draft = Path(sf._workspace.get_step_tmp_dir("p", "pm_boundary", "3"))
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "draft.txt").write_text("incomplete retained evidence")
    prior = draft.parent / "3"
    prior.mkdir(exist_ok=True)
    (prior / "tasks_manifest.json").write_text('{"execution_order":[["old"]]}')

    class Runner:
        async def execute(self, claim):
            calls.append(claim.step_id)
            raise error("PM exhausted; retained draft")
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda p: run)
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    async def boundary(*a): return "3"
    monkeypatch.setattr(scheduler, "_advance_off_the_loop", boundary)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda p: None)
    monkeypatch.setattr("aitelier.runner.AgentStepRunner", lambda **kw: Runner())
    monkeypatch.setattr(scheduler, "tick_log", lambda *a, **kw: None)
    confirm = MagicMock(wraps=sf.confirm_step)
    monkeypatch.setattr(sf, "confirm_step", confirm)

    await scheduler._run_skillflow_tick("p", None)
    assert sf.get_run(run)["status"] == "failed"
    assert row(sf, run, "3")["status"] == "failed"
    assert row(sf, run, "3")["last_error"] == "PM exhausted; retained draft"
    assert row(sf, run, "3_budget")["status"] == "pending"
    assert row(sf, run, "3_review")["status"] == "pending"
    assert sf.claim_next_step(run) is None
    assert calls == ["3"]
    confirm.assert_not_called()
    assert (draft / "draft.txt").read_text() == "incomplete retained evidence"
    assert (prior / "tasks_manifest.json").read_text() == '{"execution_order":[["old"]]}'
    assert not sf._conn.execute(
        "SELECT 1 FROM skillflow_steps WHERE run_id=? AND step_id IN ('t_plan','t_impl') AND status!='pending'",
        (run,)).fetchone()


def test_old_error_edge_reproduces_bypass(tmp_path):
    sf, run = engine(tmp_path, True, old_edge=True)
    claim = sf.claim_next_step(run)
    sf.fail_step(claim.token, "PM exhausted", retryable=False)
    assert sf.get_run(run)["status"] == "running"
    assert sf.get_run(run)["current_node"] == "task_loop"
    assert row(sf, run, "3")["status"] == "failed"
    assert row(sf, run, "3_budget")["status"] == "pending"
    assert row(sf, run, "3_review")["status"] == "pending"


@pytest.mark.parametrize("game", [False, True], ids=["base", "game"])
def test_pm_success_still_requires_explicit_approval(tmp_path, game):
    sf, run = engine(tmp_path, game)
    claim = sf.claim_next_step(run)
    draft = Path(sf._workspace.get_step_tmp_dir("p", "pm_boundary", "3"))
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "tasks_manifest.json").write_text('{"execution_order":[["reviewed"]]}')
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    sf.advance_run(run)
    assert sf.get_run(run)["status"] == "paused"
    assert row(sf, run, "3_budget")["status"] == "pending"
    assert row(sf, run, "3_review")["status"] == "pending"
    sf.approve_checkpoint(run)
    assert sf.get_run(run)["current_node"] == "3_budget"
    resolver = sf._get_resolver_for_run(run)
    assert resolver.next_node("3_budget", {"within_budget": True}, {}) == "3_review"
