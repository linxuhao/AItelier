# tests/integration/test_full_pipeline_mocked.py
#
# Full-pipeline no-regression test driven entirely by MOCKED agent responses.
#
# This replaces the deleted real-LLM e2e (tests/e2e/test_integration_e2e.py,
# which required ZHIPU_API_KEY): every DPE role and the pre-pipeline meta
# conversation are exercised through the REAL engine + REAL deterministic
# response-parsing/dispatch, with only the LLM call faked. Because nothing
# touches the network, this runs in the default suite and guards against
# feature-breaking regressions on every commit.
#
# What is real here: PipelineEngine.run_step dispatch, JSON-mode response
# parsing, write-action detection, retry/feedback loops, native-vs-JSON mode.
# What is faked: the LLM text (factory.get_agent(...).run) and the leaf tool
# execution (_exec_tool), so the test is deterministic and offline.

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded
from core.meta_conversation import MetaConversationAgent, format_brief_as_markdown

from skillflow import SkillFlow, PipelineGraph
from skillflow.core import StepResult
from skillflow.tool_loader import ToolLoader
import skillflow as _skillflow_pkg
import yaml


# ── Mocked-agent harness (mirrors tests/integration/test_dpe_pipeline.py) ──

# Read+write tool schema: drives the multi-turn _run_tool_content_step path,
# which is what every production DPE agent step uses.
TS = {
    "read_file": {"description": "Read a file", "parameters": {"path": {"type": "string"}}},
    "list_tree": {"description": "List dir", "parameters": {"path": {"type": "string"}}},
    "write": {"description": "Write a file", "parameters": {"file": {"type": "string"}, "content": {"type": "string"}}},
}


class MockWorkspace:
    def __init__(self, tmp_path: Path):
        self.base_path = tmp_path
        self.projects_base = tmp_path / "projects"
        self.written_drafts = {}

    def _get_secure_path(self, project_id: str) -> Path:
        return self.base_path / project_id

    def get_code_path(self, project_id: str) -> Path:
        code_path = self.projects_base / project_id
        code_path.mkdir(parents=True, exist_ok=True)
        return code_path

    def write_draft(self, project_id, step_id, filename, content, graph_name=None):
        self.written_drafts[f"{step_id}/{filename}"] = content

    def get_final_path(self, project_id: str, step_id: str) -> Path:
        return self.base_path / project_id / step_id

    def _get_git_hash(self, project_path: Path) -> str:
        return "fake_hash"


@pytest.fixture
def engine():
    """A PipelineEngine whose AgentFactory and leaf-tool executor are mocked.

    is_native defaults to False so the deterministic JSON tool-mode path runs
    (the native path is covered explicitly in TestEdgeCases)."""
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        eng = PipelineEngine()
        eng.factory = MagicMock()
        eng.factory.is_native.return_value = False
        eng.factory.get_fallback_to_json.return_value = True
        eng.factory.get_max_retries.return_value = 3
        eng.factory.get_max_tool_turns.return_value = 5
        # Deterministic leaf-tool execution: every write "succeeds" and reports
        # the file it wrote; every read returns a stub. Keeps the test offline
        # while still flowing through real write-detection / success logic.
        def fake_exec(action):
            tool = action.get("tool", "")
            params = action.get("params", {})
            if tool == "write" or tool.startswith(("write_", "create_", "append_")):
                return {"written": params.get("file") or params.get("filename") or "output"}
            return {"output": "stub"}
        eng._exec_tool = MagicMock(side_effect=fake_exec)
        return eng


def _setup_workspace(tmp_path, project_id="default"):
    (tmp_path / project_id).mkdir(parents=True, exist_ok=True)
    code_path = tmp_path / "projects" / project_id
    code_path.mkdir(parents=True, exist_ok=True)
    (code_path / "README.md").write_text("# Project\n")
    return tmp_path / project_id


def _mock_agent(response):
    """Build a mock agent whose .run() returns `response` (str) or replays a
    list of responses across multi-turn calls."""
    mg = MagicMock()
    mg.gateway.litellm_model = "mock-model"
    if isinstance(response, list):
        mg.run.side_effect = response
    else:
        mg.run.return_value = response
    return mg


def _write_action(filename: str, content: str = "x") -> dict:
    return {"tool": "write", "params": {"file": filename, "content": content}}


def _content_response(filename: str, content: str = "x") -> str:
    return json.dumps({"thoughts": f"writing {filename}", "actions": [_write_action(filename, content)]})


def _verdict_response(passed: bool, suggestions=None) -> str:
    return json.dumps({
        "thoughts": "reviewing",
        "actions": [_write_action(
            "review_verdict.json",
            json.dumps({"passed": passed, "suggestions": suggestions or []}),
        )],
    })


# The full DPE role sequence (configs/dpe_default.yaml), minus the inline
# tool nodes git_sync_pre / 5_test which run real git/pytest and are not
# agent steps. Each tuple: (step_id, agent_config, canned_response, expected_file).
PIPELINE_STEPS = [
    ("1", "researcher", _content_response("step1_sota.md", "# SOTA"), "step1_sota.md"),
    ("1_review", "researcher_reviewer", _verdict_response(True), "review_verdict.json"),
    ("2", "architect", _content_response("step2_design.md", "# Design"), "step2_design.md"),
    ("2_review", "architect_reviewer", _verdict_response(True), "review_verdict.json"),
    # Step 3 (PM) is multi-file: it accumulates task cards and only terminates
    # on an explicit end_step action (dpe_pipeline.py:726), so the mock emits both.
    ("3", "pm", json.dumps({"thoughts": "decomposing", "actions": [
        _write_action("tasks_manifest.json", '{"total": 1}'),
        _write_action("tasks/task_1.json", '{"id": "task_1"}'),
        {"tool": "end_step", "params": {"summary": "1 task"}},
    ]}), "tasks_manifest.json"),
    ("3_review", "pm_reviewer", _verdict_response(True), "review_verdict.json"),
    ("t_plan", "task_planner", _content_response("task_plan.md", "# Plan"), "task_plan.md"),
    ("t_plan_review", "task_planner_reviewer", _verdict_response(True), "review_verdict.json"),
    ("t_impl", "task_implementer", _content_response("main.py", "def add(a, b): return a + b"), "main.py"),
    ("t_impl_review", "task_implementer_reviewer", _verdict_response(True), "review_verdict.json"),
    ("5", "final_verifier", _content_response("verification_report.md", "# OK"), "verification_report.md"),
    ("5_review", "final_verifier_reviewer", _verdict_response(True), "review_verdict.json"),
]


# ════════════════════════════════════════════════════════════════════════
# Meta conversation → project brief (replaces the meta phase of the old e2e)
# ════════════════════════════════════════════════════════════════════════

class TestMetaConversationToBrief:
    @patch("core.meta_conversation.AIGateway")
    def test_multi_turn_then_brief(self, mock_gw_cls):
        """Mocked meta agent gathers requirements over turns → a valid brief."""
        mock_gw = MagicMock()
        mock_gw.generate.side_effect = [
            json.dumps({"status": "asking", "message": "What language?",
                        "analysis_so_far": "wants an adder"}),
            json.dumps({"status": "complete", "message": "Done!", "project_brief": {
                "project_name": "Adder",
                "description": "Adds two numbers",
                "user_stories": ["As a user I add two numbers"],
                "goals": ["correct addition", "unit tests"],
                "non_goals": ["no GUI"],
                "tech_constraints": ["Python 3.12"],
                "target_users": "Developers",
                "success_criteria": "add(2,3)==5",
            }}),
        ]
        mock_gw_cls.return_value = mock_gw

        answers = iter(["Python 3.12"])
        brief = MetaConversationAgent().converse(
            "write add(a, b)", io_handler=lambda m: next(answers))

        assert brief["project_name"] == "Adder"
        assert mock_gw.generate.call_count == 2
        md = format_brief_as_markdown(brief)
        assert "# Project Brief: Adder" in md
        assert "correct addition" in md


# ════════════════════════════════════════════════════════════════════════
# Full DPE pipeline, every role, mocked agents, happy path
# ════════════════════════════════════════════════════════════════════════

class TestFullPipelineHappyPath:
    def test_every_role_runs_and_writes(self, engine):
        """Drive all 12 DPE agent steps in order; each parses its mocked
        response, dispatches, and writes its artifact through the real engine."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)

        written = []
        for i, (step_id, agent, response, expected_file) in enumerate(PIPELINE_STEPS):
            engine.factory.get_agent.return_value = _mock_agent(response)
            result = engine.run_step(
                task_id=i + 1, step_id=step_id, workspace=ws,
                project_id="default", agent_config_name=agent, tool_schemas=TS,
            )
            assert result is True, f"step {step_id} ({agent}) did not succeed"
            files_this_step = [a.args[0]["params"]["file"]
                               for a in engine._exec_tool.call_args_list
                               if a.args[0].get("tool") == "write"]
            assert expected_file in files_this_step, (
                f"step {step_id} did not write {expected_file}")
            written.append(expected_file)

        # Every planning artifact + every review verdict was produced.
        assert "step1_sota.md" in written
        assert "step2_design.md" in written
        assert "tasks_manifest.json" in written
        assert "main.py" in written
        assert "verification_report.md" in written
        assert written.count("review_verdict.json") == 6  # one per *_review step


# ════════════════════════════════════════════════════════════════════════
# Edge cases — the failure / unusual paths that break silently
# ════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_reviewer_rejects_writes_failing_verdict(self, engine):
        """A red-team rejection still produces a well-formed verdict artifact
        with passed=false + suggestions (what drives the graph's loop-back)."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)

        engine.factory.get_agent.return_value = _mock_agent(
            _verdict_response(False, ["missing tests", "no error handling"]))
        result = engine.run_step(
            task_id=1, step_id="1_review", workspace=ws, project_id="default",
            agent_config_name="researcher_reviewer", tool_schemas=TS)

        assert result is True
        write_call = next(a.args[0] for a in engine._exec_tool.call_args_list
                          if a.args[0].get("tool") == "write")
        verdict = json.loads(write_call["params"]["content"])
        assert verdict["passed"] is False
        assert len(verdict["suggestions"]) == 2

    def test_explore_then_write_multi_turn(self, engine):
        """Agent reads context on turn 1, writes on turn 2 — the multi-turn
        loop must keep going until a write happens, not bail after exploration."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)

        agent = _mock_agent([
            json.dumps({"thoughts": "explore", "actions": [
                {"tool": "list_tree", "params": {"path": ".", "depth": 1}},
                {"tool": "read_file", "params": {"path": "README.md"}}]}),
            _content_response("main.py", "def f(): pass"),
        ])
        engine.factory.get_agent.return_value = agent

        result = engine.run_step(
            task_id=1, step_id="t_impl", workspace=ws, project_id="default",
            agent_config_name="task_implementer", tool_schemas=TS)

        assert result is True
        assert agent.run.call_count == 2

    def test_no_write_exhausts_retries(self, engine):
        """An agent that only ever explores (never writes) must raise
        MaxRetriesExceeded, not silently succeed with empty output."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)
        engine.factory.get_max_tool_turns.return_value = 2
        engine.factory.get_max_retries.return_value = 2

        engine.factory.get_agent.return_value = _mock_agent(json.dumps({
            "thoughts": "just looking", "actions": [
                {"tool": "list_tree", "params": {"path": ".", "depth": 1}}]}))

        with pytest.raises(MaxRetriesExceeded):
            engine.run_step(task_id=1, step_id="t_impl", workspace=ws,
                            project_id="default",
                            agent_config_name="task_implementer", tool_schemas=TS)

    def test_malformed_json_with_code_fences_is_repaired(self, engine):
        """LLMs often wrap JSON in ```json fences; the engine must still parse
        and execute the write rather than treating the whole thing as garbage."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)

        fenced = "```json\n" + _content_response("step1_sota.md", "# SOTA") + "\n```"
        engine.factory.get_agent.return_value = _mock_agent(fenced)

        result = engine.run_step(
            task_id=1, step_id="1", workspace=ws, project_id="default",
            agent_config_name="researcher", tool_schemas=TS)

        assert result is True
        assert any(a.args[0].get("tool") == "write"
                   for a in engine._exec_tool.call_args_list)

    def test_native_mode_falls_back_to_json_on_failure(self, engine):
        """When native tool-calling is enabled but fails, the engine must fall
        back to JSON mode (fallback_to_json=True) and still complete the step."""
        tmp_path = Path(tempfile.mkdtemp())
        ws = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path)

        engine.factory.is_native.return_value = True
        engine.factory.get_fallback_to_json.return_value = True
        # Native agent blows up; JSON-mode agent (get_agent) succeeds.
        engine.factory.get_native_agent.return_value = MagicMock(
            run=MagicMock(side_effect=RuntimeError("native boom")))
        engine.factory.get_agent.return_value = _mock_agent(
            _content_response("step1_sota.md", "# SOTA"))

        result = engine.run_step(
            task_id=1, step_id="1", workspace=ws, project_id="default",
            agent_config_name="researcher", tool_schemas=TS)

        assert result is True


# ════════════════════════════════════════════════════════════════════════
# Real-graph routing — drives the ACTUAL configs/dpe_default.yaml through
# skillflow so config/routing regressions (checkpoint pauses, green/red
# loop-backs, gate transitions) are caught. Agent execution is faked by
# writing each step's required output file directly; only the planning phase
# is driven (file_exists / json_schema validation), stopping at the task
# loop so the env-dependent lint / pytest tool nodes never run.
# ════════════════════════════════════════════════════════════════════════

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_VERDICT_PASS = json.dumps({"passed": True, "feedback": "ok", "suggestions": []})
_VERDICT_FAIL = json.dumps({"passed": False, "feedback": "no", "suggestions": ["fix"]})


def _build_real_run(tmp_path):
    """An isolated SkillFlow with the REAL dpe graph + agent configs + tools."""
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)
    for f in sorted((_REPO_ROOT / "agent_configs").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            try:
                sf.register_agent_config_from_dict(name, cfg)
            except Exception:
                pass  # duplicate names across files — first wins, fine for routing
    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / "dpe_default.yaml")
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, run_id


def _emit_step_output(output_dir, step_id, verdict=_VERDICT_PASS):
    """Write the file a step's validation requires (faking the agent)."""
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    if step_id == "1":
        (p / "step1_sota.md").write_text("# SOTA", encoding="utf-8")
    elif step_id == "2":
        (p / "step2_design.md").write_text("# Design", encoding="utf-8")
    elif step_id == "3":
        (p / "tasks_manifest.json").write_text(
            json.dumps({"total": 1, "execution_order": ["t1"], "tasks": [{"id": "t1"}]}),
            encoding="utf-8")
    elif step_id.endswith("_review"):
        (p / "review_verdict.json").write_text(verdict, encoding="utf-8")


def _drive(sf, run_id, *, reject_at=None, stop_at="t_plan", max_ticks=40):
    """Drive the run, auto-approving checkpoints and passing reviews (except a
    one-time rejection at `reject_at`). Returns (executed_steps, n_checkpoints)."""
    executed = []
    checkpoints = 0
    rejected = set()
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        if node is None:
            run = sf.get_run(run_id)
            if run["status"] == "paused":
                checkpoints += 1
                sf.approve_checkpoint(run_id)
                continue
            break  # completed / failed
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        sid = claimed.step_id
        executed.append(sid)
        verdict = _VERDICT_PASS
        if sid == reject_at and sid not in rejected:
            verdict = _VERDICT_FAIL
            rejected.add(sid)
        _emit_step_output(claimed.inputs.get("_output_dir"), sid, verdict)
        sf.confirm_step(claimed.token, StepResult(flags={"synced": True}))
        if sid == stop_at:
            break
    return executed, checkpoints


class TestRealGraphRouting:
    def test_planning_phase_reaches_task_loop_with_checkpoints(self):
        """The real config routes git_sync_pre → 1 → 1_review → 2 → 2_review →
        3 → 3_review → task_loop → t_plan, pausing at each green checkpoint."""
        tmp_path = Path(tempfile.mkdtemp())
        sf, run_id = _build_real_run(tmp_path)
        executed, checkpoints = _drive(sf, run_id, stop_at="t_plan")

        assert "t_plan" in executed, f"never reached task phase: {executed}"
        for sid in ("1", "1_review", "2", "2_review", "3", "3_review", "t_plan"):
            assert sid in executed, f"{sid} missing from {executed}"
        assert executed.index("1") < executed.index("2") < executed.index("3")
        # Steps 1, 2, 3 are checkpoints → the run paused (we approved) thrice.
        assert checkpoints == 3, f"expected 3 checkpoint pauses, got {checkpoints}"

    def test_review_rejection_loops_back_to_producer(self):
        """A red-team rejection (passed=false) at 1_review must route back to
        step 1 — the core green/red adversarial guarantee on the real config."""
        tmp_path = Path(tempfile.mkdtemp())
        sf, run_id = _build_real_run(tmp_path)
        executed, _ = _drive(sf, run_id, reject_at="1_review", stop_at="t_plan")

        assert executed.count("1") >= 2, f"no loop-back on rejection: {executed}"
        assert "t_plan" in executed
