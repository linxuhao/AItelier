# tests/integration/test_full_pipeline_real_runner.py
#
# TRUE end-to-end DPE pipeline test: the REAL graph (configs/dpe_default.yaml)
# driven through the REAL AgentStepRunner + REAL PipelineEngine, start to
# finish, with ONLY the agent LLM and the environment-dependent leaf tools
# (lint / pytest / repo_apply / git) faked. Mocked agents return instantly, so
# the whole pipeline — every node, every checkpoint, the task loop, the final
# verify — runs in well under a second.
#
# This is the strongest no-regression guard: a config/routing/engine change
# that breaks the pipeline end-to-end fails here. Tool execution for agent
# writes is REAL (skillflow's generated write_* tools), output validation is
# REAL (file_exists / json_schema), only the three env-bound tools are stubbed.

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from skillflow import SkillFlow, PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.tool_loader import ToolLoader

from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from aitelier.runner import AgentStepRunner

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Stubs for the three environment-dependent tools ──────────────────────
# skillflow calls tools as fn(**filtered) and reads success from result["passed"]
# (core.py:1306). Stubs accept anything and report success.

def _stub_pass(*args, **kwargs):
    return {"passed": True, "summary": "stubbed", "returncode": 0}

def _stub_repo_apply(*args, **kwargs):
    return {"passed": True, "applied": True, "committed": True}

def _stub_git_synced(*args, **kwargs):
    return {"synced": True, "passed": True}


def _build_real_pipeline(tmp_path):
    """Wire an isolated, fully-real DPE pipeline with stubbed env tools."""
    ws_base = tmp_path / "ws"
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")

    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(ws_base),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)

    # Override the env-dependent tools (lint=ruff, run_tests=pytest,
    # repo_apply=git, git_sync_pre=git) with instant-pass stubs.
    loader.register_dynamic_tool("lint", {}, _stub_pass)
    loader.register_dynamic_tool("run_tests", {}, _stub_pass)
    loader.register_dynamic_tool("repo_apply", {}, _stub_repo_apply)
    loader.register_dynamic_tool("git_sync_pre", {}, _stub_git_synced)

    # The tool-NODE tools (git_sync_pre, run_tests) must run inline during
    # advance_run. skillflow only runs a tool node inline when is_native()
    # is true; run_tests is a custom (non-native) tool, so advance would
    # otherwise delegate it and stall. Treat both as native so our stubs run
    # inline — exactly as the real native git_sync_pre already does.
    _inline_tools = {"git_sync_pre", "run_tests"}
    _orig_is_native = loader.is_native
    loader.is_native = lambda name: name in _inline_tools or _orig_is_native(name)

    for f in sorted((_REPO_ROOT / "agent_configs").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            try:
                sf.register_agent_config_from_dict(name, cfg)
            except Exception:
                pass

    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / "dpe_default.yaml")
    sf.register_graph(graph)

    db = DBManager(str(tmp_path / "aitelier.db"))
    db.ensure_project("p", name="E2E")
    ws = WorkspaceManager(base_path=str(ws_base))

    # Seed the finalized brief that step 1 reads as a REQUIRED context source
    # ({config: meta_conversation, step: finalize, output: step1_goals.json},
    # required: true). In a real run the meta_conversation finalize step emits
    # this; without it the researcher fails loud (RequiredContextMissing) — the
    # fail-loud-on-missing-brief guard. Mirror that artifact here.
    finalize = ws_base / "p" / "meta_conversation" / "finalize"
    finalize.mkdir(parents=True, exist_ok=True)
    (finalize / "step1_goals.json").write_text(json.dumps({
        "mvp_goals": ["Add two numbers"],
        "non_goals": ["No UI"],
        "user_stories": ["As a user, I can add two numbers"],
    }), encoding="utf-8")

    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, db, ws, run_id


# ── Agent response builder: instant canned output per step ───────────────
# Built from the step's injected _tool_schemas so it always uses the exact
# constrained write_* tool skillflow generated for that step.

def _action(tool, **params):
    return {"tool": tool, "params": params}


def _build_agent_response(step_id, tool_schemas, *, review_passed=True):
    """Return the JSON string a mocked agent would produce for this step."""
    writes = [k for k in tool_schemas if k.startswith("write_")]

    if step_id.endswith("_review"):
        verdict = json.dumps({"passed": review_passed, "feedback": "ok",
                              "suggestions": [] if review_passed else ["redo it"]})
        return json.dumps({"thoughts": "review",
                           "actions": [_action("write_verdict", content=verdict)]})

    if step_id == "3":
        manifest = json.dumps({"total": 1, "execution_order": ["t1"],
                               "tasks": [{"id": "t1", "description": "implement add"}]})
        return json.dumps({"thoughts": "decompose", "actions": [
            _action("write_tasks_manifest", content=manifest),
            _action("write_task_card", id="t1", content=json.dumps({"id": "t1"})),
            {"tool": "end_step", "params": {"summary": "1 task"}},
        ]})

    # Single-output planning/impl steps: prefer the specific write_* tool,
    # fall back to generic write (t_impl writes free-form code).
    if "write_sota" in writes:
        return json.dumps({"thoughts": "sota", "actions": [_action("write_sota", content="# SOTA")]})
    if "write_design" in writes:
        return json.dumps({"thoughts": "design", "actions": [_action("write_design", content="# Design")]})
    if "write_plan" in writes:
        return json.dumps({"thoughts": "plan", "actions": [_action("write_plan", content="# Plan")]})
    if "write_report" in writes:
        # verify_report.json is a structured .json slot — string content is
        # json.loads-validated at write time, so it must be real JSON.
        report = json.dumps({"all_goals_met": True, "verified_subtasks": [],
                             "issues": [], "ready_for_deploy": True})
        return json.dumps({"thoughts": "verify",
                           "actions": [_action("write_report", content=report)]})

    specific = [w for w in writes if not w.startswith("write_linter")]
    if specific:
        return json.dumps({"thoughts": "out", "actions": [_action(specific[0], content="# output")]})

    # Generic write (t_impl): emit valid Python so any non-stubbed check is happy.
    return json.dumps({"thoughts": "implement", "actions": [
        _action("write", file="main.py", content="def add(a, b):\n    return a + b\n")]})


async def _drive_to_completion(sf, db, ws, run_id, monkeypatch, max_ticks=120,
                               reject_once_at=None):
    """Run the real scheduler loop with mocked agents until the run terminates."""
    from unittest.mock import MagicMock
    import api.dependencies as deps
    import core.agents as agents_mod

    # Every get_skillflow() (engine._exec_tool, runner) resolves to THIS sf.
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)

    # Hold the response for the step currently executing; the mocked agent
    # reads it. Steps run one at a time so a single slot is race-free.
    current = {"response": "{}"}

    def fake_get_agent(self, name):
        mg = MagicMock()
        mg.gateway.litellm_model = "mock-model"
        mg.run.side_effect = lambda *a, **k: current["response"]
        return mg

    monkeypatch.setattr(agents_mod.AgentFactory, "get_agent", fake_get_agent)
    monkeypatch.setattr(agents_mod.AgentFactory, "is_native", lambda self, name: False)
    monkeypatch.setattr(agents_mod.AgentFactory, "get_max_retries", lambda self, sid: 2)
    monkeypatch.setattr(agents_mod.AgentFactory, "get_max_tool_turns", lambda self, sid: 3)

    runner = AgentStepRunner(db_manager=db, workspace_manager=ws,
                             agent_factory=None, prompt_assembler=None, event_bus=None)

    executed = []
    checkpoints = 0
    rejected = set()
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        if node is None:
            run = sf.get_run(run_id)
            status = run["status"]
            if status == "paused":
                checkpoints += 1
                sf.approve_checkpoint(run_id)
                continue
            if status == "running":
                # advance_run reached an internal node (tool/gate/loop) and
                # set current_node but hasn't produced a claimable agent step
                # yet — e.g. it just routed 5 → 5_test. Call advance again so
                # the tool fast-path runs the node inline. The max_ticks bound
                # is the stall guard if it never progresses.
                continue
            return status, executed, checkpoints  # completed / failed
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        review_passed = True
        if claimed.step_id == reject_once_at and reject_once_at not in rejected:
            review_passed = False
            rejected.add(reject_once_at)
        current["response"] = _build_agent_response(
            claimed.step_id, claimed.inputs.get("_tool_schemas", {}),
            review_passed=review_passed)
        executed.append(claimed.step_id)
        result = await runner.execute(claimed)
        sf.confirm_step(claimed.token, result)
    return "TIMEOUT", executed, checkpoints


class TestRealRunnerFullPipeline:
    async def test_pipeline_runs_start_to_finish(self, tmp_path, monkeypatch):
        """The entire real DPE graph completes via the real runner + engine
        with mocked agents — git_sync_pre → planning → task loop → final verify."""
        sf, db, ws, run_id = _build_real_pipeline(tmp_path)
        status, executed, checkpoints = await _drive_to_completion(
            sf, db, ws, run_id, monkeypatch)

        assert status == "completed", f"pipeline did not complete: {status}; ran {executed}"
        # Every major phase actually executed.
        for sid in ("1", "1_review", "2", "2_review", "3", "3_review",
                    "t_plan", "t_impl", "t_impl_review", "5", "5_review"):
            assert sid in executed, f"{sid} never ran: {executed}"
        # The three green checkpoints (steps 1, 2, 3) each paused the run.
        assert checkpoints == 3, f"expected 3 checkpoints, got {checkpoints}"

    async def test_red_team_rejection_loops_back_then_completes(self, tmp_path, monkeypatch):
        """A red-team rejection at t_impl_review must loop back to t_impl,
        re-implement, and the pipeline must still drive through to completion —
        the green/red adversarial guarantee, end-to-end through the real runner."""
        sf, db, ws, run_id = _build_real_pipeline(tmp_path)
        status, executed, _ = await _drive_to_completion(
            sf, db, ws, run_id, monkeypatch, reject_once_at="t_impl_review")

        assert status == "completed", f"did not recover from rejection: {executed}"
        # t_impl ran twice (initial + after the rejection looped back).
        assert executed.count("t_impl") >= 2, f"no loop-back on rejection: {executed}"
        assert executed.count("t_impl_review") >= 2
