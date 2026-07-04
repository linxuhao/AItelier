# tests/integration/test_new_pipelines_mocked_agent.py
#
# MOCKED-AGENT execution tests for the reusable pipelines (investigate,
# code_review, coding_impl, fix_tests, subagent). Unlike the graph-ROUTING
# tests in tests/skillflow/ (which bypass the agent with a manual confirm),
# these run the REAL AgentStepRunner with a canned LLM — so per-config prompt
# assembly, context delivery, the write-tool wiring, output production and
# validation all execute. Reuses the DPE full-pipeline harness pattern.
#
# Env-dependent leaf tools (run_tests / repo_apply / lint) are stubbed to pass
# so the happy path exercises the AGENT path, not the toolchain; loop/failure
# behaviour is covered by the routing tests.

import json
from pathlib import Path

import pytest
import yaml

from skillflow import SkillFlow, PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.tool_loader import ToolLoader

from aitelier.runner import AgentStepRunner
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _stub_pass(*a, **k):
    return {"passed": True, "summary": "ok", "returncode": 0}


def _stub_run_tests(*a, out_dir="", **k):
    # Write test_report.json so the tool-fed gate transition (from_file) can
    # read the result — the real run_tests does this too.
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "test_report.json").write_text(
            json.dumps({"passed": True, "returncode": 0, "summary": "ok",
                        "failures": []}), encoding="utf-8")
    return {"written": "test_report.json", "passed": True}


def _stub_repo_apply(*a, **k):
    return {"passed": True, "applied": True, "committed": True}


def _build(tmp_path, config_name, seeds):
    """Real SkillFlow for one config, env tools stubbed, seeds written to the
    config's _seed dir (so {config: X, output: Y} context sources resolve)."""
    ws_base = tmp_path / "ws"
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(ws_base), projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)
    loader.register_dynamic_tool("lint", {}, _stub_pass)
    loader.register_dynamic_tool("run_tests", {}, _stub_run_tests)
    loader.register_dynamic_tool("repo_apply", {}, _stub_repo_apply)
    _orig = loader.is_native
    loader.is_native = lambda n: n in {"run_tests"} or _orig(n)

    for f in sorted((_REPO_ROOT / "agent_configs").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            try:
                sf.register_agent_config_from_dict(name, cfg)
            except Exception:
                pass
    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / f"{config_name}.yaml")
    sf.register_graph(graph)

    db = DBManager(str(tmp_path / "aitelier.db"))
    db.ensure_project("p", name="E2E")
    ws = WorkspaceManager(base_path=str(ws_base))

    seed_dir = sf._workspace.get_config_path("p", config_name) / "_seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    for fname, content in seeds.items():
        (seed_dir / fname).write_text(content, encoding="utf-8")

    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, db, ws, run_id


def _response(step_id, tool_schemas):
    """Canned agent output built from the step's actual injected write tools —
    so it always calls the exact write_* tool skillflow generated."""
    names = set(tool_schemas)
    if "write_findings" in names:
        return json.dumps({"thoughts": "explored", "actions": [
            {"tool": "write_findings", "params": {"content": "# Findings\nTwo functions."}}]})
    if "write_verdict" in names:
        verdict = json.dumps({"passed": True, "feedback": "lgtm", "findings": []})
        return json.dumps({"thoughts": "review", "actions": [
            {"tool": "write_verdict", "params": {"content": verdict}}]})
    if "write_summary" in names:
        return json.dumps({"thoughts": "done", "actions": [
            {"tool": "write_summary", "params": {"content": "# Summary\ndid it"}}]})
    # output.mode: write → create a new file, then finish.
    if "create" in names:
        return json.dumps({"thoughts": "implement", "actions": [
            {"tool": "create", "params": {"file": "impl.py", "content": "x = 1\n"}},
            {"tool": "finish_step", "params": {"summary": "done"}}]})
    if "write" in names:
        return json.dumps({"thoughts": "implement", "actions": [
            {"tool": "write", "params": {"file": "impl.py", "content": "x = 1\n"}},
            {"tool": "finish_step", "params": {"summary": "done"}}]})
    return json.dumps({"thoughts": "noop", "actions": [
        {"tool": "finish_step", "params": {"summary": "nothing"}}]})


async def _drive(sf, db, ws, run_id, monkeypatch, max_ticks=40):
    """Run the real AgentStepRunner with a mocked LLM until termination.
    Returns (status, executed_step_ids)."""
    from unittest.mock import MagicMock
    import api.dependencies as deps
    import core.agents as agents_mod
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    current = {"response": "{}"}

    def fake_get_agent(self, name):
        mg = MagicMock()
        mg.gateway.litellm_model = "mock-model"
        mg.run.side_effect = lambda *a, **k: current["response"]
        return mg

    monkeypatch.setattr(agents_mod.AgentFactory, "get_agent", fake_get_agent)
    monkeypatch.setattr(agents_mod.AgentFactory, "is_native", lambda self, n: False)
    monkeypatch.setattr(agents_mod.AgentFactory, "get_max_retries", lambda self, s: 1)
    monkeypatch.setattr(agents_mod.AgentFactory, "get_max_tool_turns", lambda self, s: 3)

    runner = AgentStepRunner(db_manager=db, workspace_manager=ws,
                             agent_factory=None, prompt_assembler=None, event_bus=None)
    executed = []
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] in ("running", "paused"):
                if run["status"] == "paused":
                    sf.approve_checkpoint(run_id)
                continue
            return run["status"], executed
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        current["response"] = _response(
            claimed.step_id, claimed.inputs.get("_tool_schemas", {}))
        executed.append(claimed.step_id)
        result = await runner.execute(claimed)
        sf.confirm_step(claimed.token, result)
    return "TIMEOUT", executed


def _final(sf, run_id, config, step, fname):
    """Read a promoted step output file."""
    p = sf._workspace.get_step_dir("p", config, step) / fname
    return p.read_text(encoding="utf-8") if p.exists() else None


class TestMockedAgentPipelines:
    async def test_investigate(self, tmp_path, monkeypatch):
        sf, db, ws, run_id = _build(tmp_path, "investigate",
                                    {"task.md": "what functions exist?"})
        status, steps = await _drive(sf, db, ws, run_id, monkeypatch)
        assert status == "completed"
        assert steps == ["investigate"]
        assert "Findings" in (_final(sf, run_id, "investigate", "investigate", "findings.md") or "")

    async def test_code_review(self, tmp_path, monkeypatch):
        sf, db, ws, run_id = _build(tmp_path, "code_review",
                                    {"review_request.md": "task\n\ndiff --git a/x b/x\n+x"})
        status, steps = await _drive(sf, db, ws, run_id, monkeypatch)
        assert status == "completed"
        verdict = _final(sf, run_id, "code_review", "review", "review_verdict.json")
        assert verdict and json.loads(verdict)["passed"] is True

    async def test_coding_impl(self, tmp_path, monkeypatch):
        # implement (agent) runs through the real runner; `test`/`done` are
        # inline tool/gate nodes (never claimed), so completion proves the gate
        # ran and passed.
        sf, db, ws, run_id = _build(tmp_path, "coding_impl",
                                    {"plan.md": "## Goal\nadd a thing"})
        status, steps = await _drive(sf, db, ws, run_id, monkeypatch)
        assert status == "completed"
        assert "implement" in steps

    async def test_fix_tests(self, tmp_path, monkeypatch):
        sf, db, ws, run_id = _build(tmp_path, "fix_tests",
                                    {"task.md": "make tests pass"})
        status, steps = await _drive(sf, db, ws, run_id, monkeypatch)
        assert status == "completed"
        assert "fix" in steps

    async def test_subagent(self, tmp_path, monkeypatch):
        sf, db, ws, run_id = _build(tmp_path, "subagent",
                                    {"task.md": "do the thing"})
        status, steps = await _drive(sf, db, ws, run_id, monkeypatch)
        assert status == "completed"
        assert "work" in steps and "review" in steps
