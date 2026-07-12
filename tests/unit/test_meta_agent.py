# tests/unit/test_meta_agent.py
# Unit tests for core/meta_agent.py — MetaAgent tool dispatch and streaming.

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

from core.meta_agent import (
    MetaAgent, TOOL_DEFINITIONS, SYSTEM_PROMPT,
    _load_meta_agent_config, _resolve_provider,
)


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.list_projects_with_stats.return_value = [
        {"project_id": "test-proj", "name": "Test", "status": "planning", "task_summary": {}}
    ]
    db.get_project.return_value = None
    db.ensure_project.return_value = {"project_id": "test-proj"}
    return db


@pytest.fixture
def mock_ws(tmp_path):
    ws = MagicMock()
    ws._get_secure_path.return_value = tmp_path / "ws" / "test-proj"
    ws.get_final_path.return_value = tmp_path / "ws" / "test-proj" / "1"
    return ws


@pytest.fixture
def agent(mock_db, mock_ws):
    return MetaAgent(mock_db, mock_ws, owner_email="test@local")


class TestToolDefinitions:
    def test_tool_count(self):
        assert len(TOOL_DEFINITIONS) == 34  # +generate_addon

    def test_all_tools_have_required_fields(self):
        for td in TOOL_DEFINITIONS:
            assert td["type"] == "function"
            func = td["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

    def test_tool_names_unique(self):
        names = [td["function"]["name"] for td in TOOL_DEFINITIONS]
        assert len(names) == len(set(names))

    def test_required_tools_present(self):
        names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        required = {
            "list_projects", "get_project", "update_project",
            "start_new_project", "start_from_aitelier_project",
            "start_existing_project", "start_from_git_url",
            "answer_project_conversation", "approve_project_brief",
            "retry_project", "refresh_planning",
            "list_tasks", "get_task", "retry_task", "get_step_output",
            "list_code_tree", "read_code_file", "search_code",
            "list_workspace_tree", "read_workspace_file",
            "retrieve_previous_context",
            "approve_checkpoint", "reject_checkpoint", "get_pipeline_status",
            "generate_pipeline", "generate_addon",
        }
        assert required.issubset(names)


class TestConfigLoading:
    def test_load_meta_agent_config(self):
        cfg = _load_meta_agent_config()
        assert "model" in cfg
        assert "template" in cfg

    def test_load_meta_agent_config_missing_file(self):
        cfg = _load_meta_agent_config("/nonexistent/path.yaml")
        assert isinstance(cfg, dict)
        assert "model" in cfg  # falls back to default config

    def test_resolve_provider(self):
        model, base, key = _resolve_provider("deepseek/deepseek-v4-flash")
        assert model == "openai/deepseek-v4-flash"
        assert base is not None

    def test_resolve_provider_unknown(self):
        model, base, key = _resolve_provider("unknown/model")
        # Unknown provider — not in llm_providers.json, so model name unchanged
        assert model == "unknown/model"
        assert base is None

    def test_resolve_provider_no_slash(self):
        model, base, key = _resolve_provider("gpt-4")
        assert model == "gpt-4"
        assert base is None


class TestSystemPrompt:
    def test_system_prompt_format(self):
        prompt = SYSTEM_PROMPT.format(current_project="my-proj", owner_email="user@test.com")
        assert "my-proj" in prompt
        assert "user@test.com" in prompt

    def test_system_prompt_no_project(self):
        prompt = SYSTEM_PROMPT.format(current_project="none", owner_email="cli@local")
        assert "none" in prompt


class TestToolDispatch:
    async def test_list_projects(self, agent, mock_db):
        result = await agent._execute_tool("list_projects", {})
        assert "projects" in result
        mock_db.list_projects_with_stats.assert_called_once()

    async def test_get_project_not_found(self, agent, mock_db):
        mock_db.get_project.return_value = None
        result = await agent._execute_tool("get_project", {"project_id": "missing"})
        assert "error" in result

    async def test_get_project_found(self, agent, mock_db):
        mock_db.get_project.return_value = {"project_id": "test-proj", "name": "Test"}
        result = await agent._execute_tool("get_project", {"project_id": "test-proj"})
        assert "project" in result

    async def test_update_project(self, agent, mock_db):
        result = await agent._execute_tool("update_project", {
            "project_id": "test-proj", "name": "Updated"
        })
        assert result["status"] == "updated"

    async def test_unknown_tool(self, agent):
        result = await agent._execute_tool("nonexistent_tool", {})
        assert "error" in result

    async def test_list_tasks(self, agent, mock_db):
        mock_db.list_tasks_by_project.return_value = []
        result = await agent._execute_tool("list_tasks", {"project_id": "test-proj"})
        assert "tasks" in result

    async def test_get_task_not_found(self, agent, mock_db):
        mock_db.get_connection.return_value.__enter__ = MagicMock(
            return_value=MagicMock(
                execute=MagicMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))
            )
        )
        mock_db.get_connection.return_value.__exit__ = MagicMock(return_value=False)
        result = await agent._execute_tool("get_task", {"task_id": 999})
        assert "error" in result

    async def test_retry_task(self, agent, mock_db):
        mock_db.retry_task.return_value = True
        result = await agent._execute_tool("retry_task", {"task_id": 1})
        assert result["status"] == "retried"

    async def test_retry_task_not_found(self, agent, mock_db):
        mock_db.retry_task.return_value = False
        result = await agent._execute_tool("retry_task", {"task_id": 999})
        assert "error" in result


class TestWorkspaceTools:
    async def test_list_workspace_tree(self, agent, mock_ws, tmp_path):
        ws_path = tmp_path / "ws" / "test-proj"
        ws_path.mkdir(parents=True)
        (ws_path / "test.py").write_text("print('hello')")
        mock_ws._get_secure_path.return_value = ws_path

        result = await agent._execute_tool("list_workspace_tree", {"project_id": "test-proj"})
        assert "tree" in result
        assert "test.py" in result["tree"]

    async def test_list_workspace_tree_not_found(self, agent, mock_ws, tmp_path):
        mock_ws._get_secure_path.return_value = tmp_path / "nonexistent"
        result = await agent._execute_tool("list_workspace_tree", {"project_id": "test-proj"})
        assert "error" in result

    async def test_read_workspace_file(self, agent, mock_ws, tmp_path):
        ws_path = tmp_path / "ws" / "test-proj"
        ws_path.mkdir(parents=True)
        (ws_path / "hello.py").write_text("print('hello')")
        mock_ws._get_secure_path.return_value = ws_path

        result = await agent._execute_tool("read_workspace_file", {
            "project_id": "test-proj", "path": "hello.py"
        })
        assert result["content"] == "print('hello')"

    async def test_read_workspace_file_traversal(self, agent, mock_ws, tmp_path):
        ws_path = tmp_path / "ws" / "test-proj"
        ws_path.mkdir(parents=True)
        mock_ws._get_secure_path.return_value = ws_path

        result = await agent._execute_tool("read_workspace_file", {
            "project_id": "test-proj", "path": "../../etc/passwd"
        })
        assert "error" in result


class TestCodeReadTools:
    """read_code_file paging + search_code grep (no-regression)."""

    def _code_repo(self, mock_ws, tmp_path, files: dict):
        code = tmp_path / "code" / "test-proj"
        code.mkdir(parents=True)
        for name, body in files.items():
            fp = code / name
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(body)
        mock_ws.get_code_path.return_value = code
        return code

    async def test_read_code_file_whole_small(self, agent, mock_ws, tmp_path):
        self._code_repo(mock_ws, tmp_path, {"a.py": "L1\nL2\nL3"})
        r = await agent._execute_tool("read_code_file",
                                      {"project_id": "test-proj", "path": "a.py"})
        assert r["total_lines"] == 3
        assert r["truncated"] is False
        assert r["start_line"] == 1 and r["end_line"] == 3
        # Line-numbered content
        assert "1\tL1" in r["content"] and "3\tL3" in r["content"]

    async def test_read_code_file_large_not_silently_truncated(self, agent, mock_ws, tmp_path):
        # The original bug: a >2000-line file returned a fixed prefix with no
        # signal. Now it must page and flag truncation.
        body = "\n".join(f"line{i}" for i in range(1, 5001))
        self._code_repo(mock_ws, tmp_path, {"big.py": body})
        r = await agent._execute_tool("read_code_file",
                                      {"project_id": "test-proj", "path": "big.py"})
        assert r["total_lines"] == 5000
        assert r["truncated"] is True
        assert r["end_line"] == 2000  # _MAX_READ_LINES window

    async def test_read_code_file_range(self, agent, mock_ws, tmp_path):
        body = "\n".join(f"line{i}" for i in range(1, 101))
        self._code_repo(mock_ws, tmp_path, {"big.py": body})
        r = await agent._execute_tool("read_code_file", {
            "project_id": "test-proj", "path": "big.py",
            "start_line": 90, "end_line": 95,
        })
        assert r["start_line"] == 90 and r["end_line"] == 95
        assert r["truncated"] is True
        assert "90\tline90" in r["content"]
        assert "96\tline96" not in r["content"]

    async def test_read_code_file_traversal(self, agent, mock_ws, tmp_path):
        self._code_repo(mock_ws, tmp_path, {"a.py": "x"})
        r = await agent._execute_tool("read_code_file", {
            "project_id": "test-proj", "path": "../../etc/passwd",
        })
        assert "error" in r

    async def test_search_code_finds_matches(self, agent, mock_ws, tmp_path):
        self._code_repo(mock_ws, tmp_path, {
            "a.py": "def foo():\n    return TARGET\n",
            "b.py": "x = 1\nTARGET = 2\n",
        })
        r = await agent._execute_tool("search_code",
                                      {"project_id": "test-proj", "pattern": "TARGET"})
        assert r["truncated"] is False
        files = {m["file"] for m in r["matches"]}
        assert files == {"a.py", "b.py"}
        assert all("line" in m and "text" in m for m in r["matches"])

    async def test_search_code_glob_filter(self, agent, mock_ws, tmp_path):
        self._code_repo(mock_ws, tmp_path, {
            "a.py": "TARGET\n", "notes.md": "TARGET\n",
        })
        r = await agent._execute_tool("search_code", {
            "project_id": "test-proj", "pattern": "TARGET", "glob": "*.py",
        })
        assert {m["file"] for m in r["matches"]} == {"a.py"}

    async def test_search_code_max_results_truncates(self, agent, mock_ws, tmp_path):
        body = "\n".join("HIT" for _ in range(50))
        self._code_repo(mock_ws, tmp_path, {"a.py": body})
        r = await agent._execute_tool("search_code", {
            "project_id": "test-proj", "pattern": "HIT", "max_results": 10,
        })
        assert len(r["matches"]) == 10
        assert r["truncated"] is True

    async def test_search_code_literal_fallback_on_bad_regex(self, agent, mock_ws, tmp_path):
        self._code_repo(mock_ws, tmp_path, {"a.py": "cost = price * (1 + tax)\n"})
        # "(1 +" is an invalid regex → must fall back to literal substring
        r = await agent._execute_tool("search_code", {
            "project_id": "test-proj", "pattern": "(1 +",
        })
        assert len(r["matches"]) == 1

    async def test_search_code_repo_not_found(self, agent, mock_ws, tmp_path):
        mock_ws.get_code_path.return_value = tmp_path / "nope"
        r = await agent._execute_tool("search_code",
                                      {"project_id": "test-proj", "pattern": "x"})
        assert "error" in r


class TestContextTools:
    async def test_retrieve_no_context(self, agent, tmp_path):
        result = await agent._execute_tool("retrieve_previous_context", {"project_id": "test-proj"})
        assert "error" in result

    async def test_retrieve_with_context(self, agent, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        data = [{"role": "user", "content": "hello"}]
        (meta_dir / "test-proj_context_1234.json").write_text(json.dumps(data))

        with patch("core.meta_agent._meta_dir", return_value=meta_dir):
            result = await agent._execute_tool("retrieve_previous_context", {
                "project_id": "test-proj", "which": 1
            })
        assert "messages" in result
        assert result["which"] == 1


class TestBuildMessages:
    def test_build_messages_with_history(self, agent):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        messages = agent._build_messages(history, "my-proj")
        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert "my-proj" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"

    def test_build_messages_empty_history(self, agent):
        messages = agent._build_messages([], None)
        assert len(messages) == 1
        assert messages[0]["role"] == "system"


class TestBuildAssistantMsg:
    def test_text_only(self, agent):
        msg = agent._build_assistant_msg("hello", [])
        assert msg["role"] == "assistant"
        assert msg["content"] == "hello"
        assert "tool_calls" not in msg


class TestSearchProjects:
    def _agent(self, mock_db, mock_ws):
        return MetaAgent(mock_db, mock_ws, owner_email="t@local")

    _PROJECTS = [
        {"project_id": "flappy-bird-unity", "name": "Flappy Bird", "status": "completed"},
        {"project_id": "capsule-dash-3d", "name": "Capsule Dash", "status": "running"},
        {"project_id": "capsule-dash-3d-2", "name": "Capsule Dash 2", "status": "failed"},
    ]

    def test_query_matches_id_and_name_substring(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        mock_db.list_projects_with_stats.return_value = list(self._PROJECTS)
        out = agent._tool_search_projects({"query": "capsule"})
        ids = [p["project_id"] for p in out["projects"]]
        assert ids == ["capsule-dash-3d", "capsule-dash-3d-2"]
        assert out["total_matches"] == 2 and out["truncated"] is False

    def test_status_filter(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        mock_db.list_projects_with_stats.return_value = list(self._PROJECTS)
        out = agent._tool_search_projects({"status": "failed"})
        assert [p["project_id"] for p in out["projects"]] == ["capsule-dash-3d-2"]

    def test_limit_caps_and_flags_truncation(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        mock_db.list_projects_with_stats.return_value = list(self._PROJECTS)
        out = agent._tool_search_projects({"query": "", "limit": 2})
        assert len(out["projects"]) == 2
        assert out["total_matches"] == 3 and out["truncated"] is True


class TestLayer3PipelineTools:
    """Generic pipeline-control toolset: list / wait / result / stop."""

    def _agent(self, mock_db, mock_ws):
        return MetaAgent(mock_db, mock_ws, owner_email="t@local", session_id="s1")

    def _registry(self, manifests):
        from core.config_registry import ConfigRegistry
        reg = MagicMock()
        reg.list.return_value = manifests
        reg.get.side_effect = lambda name: next(
            (m for m in manifests if m.config_name == name), None)
        # list_pipelines / describe_pipeline delegate to the real methods over
        # the mocks; bind the real static/instance methods (a MagicMock `self`
        # would otherwise resolve _entry to an auto-mock).
        reg._manifests = {m.config_name: m for m in manifests}
        reg._entry = ConfigRegistry._entry
        reg.catalog = lambda full=False: ConfigRegistry.catalog(reg, full=full)
        reg.describe = lambda q: ConfigRegistry.describe(reg, q)
        return reg

    def test_list_pipelines_surfaces_registry(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        m1 = MagicMock(config_name="code_review", label="Code Review",
                       description="Adversarial review of a code diff",
                       scheduler_owned=False, seed_file="review_request.md",
                       input_hint="the verbatim git diff", has_task_loop=False)
        m2 = MagicMock(config_name="dpe_default_v2", label="DPE",
                       description="Full DPE build",
                       scheduler_owned=True, seed_file=None,
                       input_hint="", has_task_loop=True)
        with patch("api.dependencies.get_config_registry",
                   return_value=self._registry([m2, m1])):
            out = agent._tool_list_pipelines({})
        names = [p["config_name"] for p in out["pipelines"]]
        assert names == ["code_review", "dpe_default_v2"]  # sorted
        assert out["count"] == 2
        cr = out["pipelines"][0]
        assert cr["drive"] == "inline" and cr["takes_seed"] is True
        assert "git diff" in cr["input_hint"]

    def test_describe_pipeline_targeted_lookup(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        m1 = MagicMock(config_name="code_review",
                       description="Adversarial review of a code diff",
                       scheduler_owned=False, seed_file="review_request.md",
                       input_hint="the verbatim git diff", has_task_loop=False)
        m2 = MagicMock(config_name="dpe_default_v2", description="Full DPE build",
                       scheduler_owned=True, seed_file=None,
                       input_hint="", has_task_loop=True)
        with patch("api.dependencies.get_config_registry",
                   return_value=self._registry([m2, m1])):
            # exact name → just that one's full contract (no full-catalog pull)
            out = agent._tool_describe_pipeline({"name": "code_review"})
            assert out["count"] == 1
            assert out["pipelines"][0]["config_name"] == "code_review"
            assert "git diff" in out["pipelines"][0]["input_hint"]
            # unknown → error that points back to list_pipelines
            miss = agent._tool_describe_pipeline({"name": "zzz"})
            assert "list_pipelines" in miss["error"]
            # missing arg → error
            assert "required" in agent._tool_describe_pipeline({})["error"]

    def test_stop_pipeline_fails_run(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "status": "running"}
        with patch("api.dependencies.get_skillflow", return_value=sf):
            out = agent._tool_stop_pipeline({"run_id": "r1"})
        assert out["status"] == "stopped"
        sf.fail_run.assert_called_once()

    def test_stop_pipeline_noop_when_finished(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "status": "completed"}
        with patch("api.dependencies.get_skillflow", return_value=sf):
            out = agent._tool_stop_pipeline({"run_id": "r1"})
        assert out["status"] == "completed"
        sf.fail_run.assert_not_called()

    def test_get_pipeline_result_parses_output_step_json(self, mock_db, mock_ws, tmp_path):
        agent = self._agent(mock_db, mock_ws)
        out_dir = tmp_path / "review_out"
        out_dir.mkdir()
        (out_dir / "review_verdict.json").write_text(
            json.dumps({"passed": True, "findings": []}), encoding="utf-8")
        mock_ws.get_final_path.return_value = out_dir
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "status": "completed",
                                   "graph_name": "code_review", "project_id": "p1"}
        sf.get_steps.return_value = [{"step_id": "review", "status": "completed"}]
        reg = self._registry([MagicMock(config_name="code_review", output_step="review")])
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("api.dependencies.get_config_registry", return_value=reg):
            out = agent._tool_get_pipeline_result({"run_id": "r1"})
        assert out["status"] == "completed"
        # JSON parsed into structured data, not a string
        assert out["result"]["review_verdict.json"] == {"passed": True, "findings": []}

    def test_get_pipeline_result_rejects_unfinished(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "status": "running"}
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("api.dependencies.get_config_registry", return_value=self._registry([])):
            out = agent._tool_get_pipeline_result({"run_id": "r1"})
        assert out["status"] == "running" and "not completed" in out["message"]

    async def test_wait_butler_driven_drives_inline(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "graph_name": "code_review"}
        reg = self._registry([MagicMock(config_name="code_review", scheduler_owned=False)])
        driven = {"status": "completed", "run_id": "r1"}
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("api.dependencies.get_config_registry", return_value=reg), \
             patch.object(agent, "_run_pipeline_until_checkpoint",
                          new=AsyncMock(return_value=driven)) as drive:
            out = await agent._tool_wait_until_checkpoint({"run_id": "r1"})
        drive.assert_awaited_once_with("r1")
        assert out is driven

    async def test_wait_scheduler_owned_times_out_to_running(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"id": "r1", "graph_name": "dpe_default_v2",
                                   "status": "running"}
        reg = self._registry([MagicMock(config_name="dpe_default_v2", scheduler_owned=True)])
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("api.dependencies.get_config_registry", return_value=reg), \
             patch("core.meta_agent.asyncio.sleep", new=AsyncMock()):
            out = await agent._tool_wait_until_checkpoint({"run_id": "r1", "timeout": 5})
        assert out["status"] == "running"

    async def test_wait_scheduler_owned_returns_on_pause(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        # first poll running, then paused
        sf.get_run.side_effect = [
            {"id": "r1", "graph_name": "dpe_default_v2", "status": "running"},  # tool entry
            {"id": "r1", "graph_name": "dpe_default_v2", "status": "running"},  # loop 1
            {"id": "r1", "graph_name": "dpe_default_v2", "status": "paused"},   # loop 2
        ]
        reg = self._registry([MagicMock(config_name="dpe_default_v2", scheduler_owned=True)])
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("api.dependencies.get_config_registry", return_value=reg), \
             patch("core.meta_agent.asyncio.sleep", new=AsyncMock()), \
             patch.object(agent, "_summarize_run_state",
                          return_value={"status": "checkpoint", "run_id": "r1"}) as summ:
            out = await agent._tool_wait_until_checkpoint({"run_id": "r1", "timeout": 30})
        assert out["status"] == "checkpoint"
        summ.assert_called_once_with("r1")


class TestActiveMetaRunResolution:
    """#4: the agent must never have to search for the run_id — the approve/answer
    tools self-resolve the live meta run for this turn."""

    def test_approve_and_answer_no_longer_require_run_id(self):
        by_name = {td["function"]["name"]: td["function"] for td in TOOL_DEFINITIONS}
        assert "run_id" not in by_name["approve_project_brief"]["parameters"]["required"]
        assert by_name["answer_project_conversation"]["parameters"]["required"] == ["answer"]

    def _agent(self, mock_db, mock_ws):
        return MetaAgent(mock_db, mock_ws, owner_email="t@local", session_id="sess-1")

    def test_resolves_via_session_link(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        mock_db.get_runs_for_session.return_value = ["run-9"]
        sf = MagicMock()
        sf.get_run.return_value = {
            "id": "run-9", "graph_name": "meta_conversation",
            "status": "paused", "project_id": "p1"}
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch("core.meta_run.read_gather_state",
                   return_value={"need_input": False, "brief": {}}):
            active = agent._active_meta_run()
        assert active["run_id"] == "run-9"
        assert active["project_id"] == "p1"

    def test_falls_back_to_project_scope_on_drifted_session(self, mock_db, mock_ws):
        """Reload minted a new session id → the link is empty; project-scoped
        lookup recovers the run so approval still works."""
        agent = self._agent(mock_db, mock_ws)
        mock_db.get_runs_for_session.return_value = []
        sf = MagicMock()
        sf.get_run_by_project.return_value = {"id": "run-7", "status": "paused"}
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch.object(agent, "_find_active_project", return_value="p2"), \
             patch("core.meta_run.read_gather_state", return_value={}):
            active = agent._active_meta_run()
        assert active["run_id"] == "run-7"
        assert active["project_id"] == "p2"
        sf.get_run_by_project.assert_called_once_with("p2", "meta_conversation")

    def test_returns_none_when_no_live_run(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        mock_db.get_runs_for_session.return_value = []
        sf = MagicMock()
        sf.get_run_by_project.return_value = None
        with patch("api.dependencies.get_skillflow", return_value=sf), \
             patch.object(agent, "_find_active_project", return_value=None):
            assert agent._active_meta_run() is None

    def test_with_tool_calls(self, agent):
        tcs = [{"id": "tc1", "name": "list_projects", "args": {}}]
        msg = agent._build_assistant_msg("", tcs)
        assert "tool_calls" in msg
        assert msg["tool_calls"][0]["function"]["name"] == "list_projects"


class TestResolvePipelineRun:
    """The approve/reject/wait tools must resolve the run by the stable
    project_id, so a stale/wrong run_id (e.g. the completed meta_conversation
    run) self-corrects to the project's active build run instead of no-opping.
    Regression for a live dpe_game drive where the butler approved the finished
    meta run, saw no progress, and spawned a duplicate project.
    """

    def _agent(self, mock_db, mock_ws):
        return MetaAgent(mock_db, mock_ws, owner_email="t@local")

    def test_prefers_active_run_over_stale_run_id(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"project_id": "flappy", "status": "completed"}
        sf.get_run_by_project.return_value = {"id": "dpe-run", "status": "paused"}
        with patch("api.dependencies.get_skillflow", return_value=sf):
            rid, note = agent._resolve_pipeline_run(
                {"run_id": "meta-run-completed"})
        assert rid == "dpe-run"
        assert "meta-run-completed" in note  # flagged the correction

    def test_resolves_from_project_id_alone(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run_by_project.return_value = {"id": "dpe-run", "status": "paused"}
        with patch("api.dependencies.get_skillflow", return_value=sf):
            rid, note = agent._resolve_pipeline_run({"project_id": "flappy"})
        assert rid == "dpe-run"
        assert note == ""

    def test_run_id_used_when_no_active_run(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        sf.get_run.return_value = {"project_id": "flappy", "status": "running"}
        sf.get_run_by_project.return_value = None
        with patch("api.dependencies.get_skillflow", return_value=sf):
            rid, note = agent._resolve_pipeline_run({"run_id": "only-run"})
        assert rid == "only-run"

    def test_empty_when_nothing_resolvable(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        with patch("api.dependencies.get_skillflow", return_value=sf):
            rid, note = agent._resolve_pipeline_run({})
        assert rid == ""


class TestResolveBuildConfig:
    """approve_project_brief is composition-native: it selects the build
    pipeline from an `addons` list (not a baked config-name string). Regression
    for the awkward config_name seam + the multi-addon gap."""

    def _agent(self, mock_db, mock_ws):
        return MetaAgent(mock_db, mock_ws, owner_email="t@local")

    def test_no_addons_is_base(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        cfg, err = agent._resolve_build_config(MagicMock(), {})
        assert cfg == "dpe_default_v2" and err == ""

    def test_single_addon_reuses_alias(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        with patch("core.addon_registry.list_addons",
                   return_value=[{"name": "game_harness", "alias": "dpe_game"}]), \
             patch("core.addon_registry.register_addon_combo",
                   return_value="dpe_game") as reg, \
             patch("api.dependencies.get_config_registry", return_value=MagicMock()):
            cfg, err = agent._resolve_build_config(sf, {"addons": ["game_harness"]})
        assert cfg == "dpe_game" and err == ""
        # composed with the alias name (blessed single-addon combo)
        assert reg.call_args.kwargs.get("name") == "dpe_game"

    def test_multi_addon_composes_emergent(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        sf = MagicMock()
        with patch("core.addon_registry.list_addons",
                   return_value=[{"name": "game_harness", "alias": "dpe_game"}]), \
             patch("core.addon_registry.register_addon_combo",
                   return_value="dpe_default_v2__game_harness+i18n") as reg, \
             patch("api.dependencies.get_config_registry", return_value=MagicMock()):
            cfg, err = agent._resolve_build_config(sf, {"addons": ["game_harness", "i18n"]})
        assert cfg == "dpe_default_v2__game_harness+i18n" and err == ""
        assert reg.call_args.kwargs.get("name") is None  # emergent, no alias

    def test_deprecated_config_name_still_works(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        cfg, err = agent._resolve_build_config(MagicMock(), {"config_name": "dpe_game"})
        assert cfg == "dpe_game" and err == ""

    def test_bad_addon_returns_error_and_base(self, mock_db, mock_ws):
        agent = self._agent(mock_db, mock_ws)
        with patch("core.addon_registry.list_addons", return_value=[]), \
             patch("core.addon_registry.register_addon_combo",
                   side_effect=ValueError("unknown addon 'nope'")), \
             patch("api.dependencies.get_config_registry", return_value=MagicMock()):
            cfg, err = agent._resolve_build_config(MagicMock(), {"addons": ["nope"]})
        assert cfg == "dpe_default_v2" and "Could not compose" in err
