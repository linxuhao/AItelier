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
        assert len(TOOL_DEFINITIONS) == 25

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
            "list_code_tree", "read_code_file",
            "list_workspace_tree", "read_workspace_file",
            "retrieve_previous_context",
            "approve_checkpoint", "reject_checkpoint", "get_pipeline_status",
            "generate_pipeline",
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


class TestContextTools:
    async def test_retrieve_no_context(self, agent, tmp_path):
        result = await agent._execute_tool("retrieve_previous_context", {"project_id": "test-proj"})
        assert "error" in result

    async def test_retrieve_with_context(self, agent, tmp_path):
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        data = [{"role": "user", "content": "hello"}]
        (meta_dir / "test-proj_context_1234.json").write_text(json.dumps(data))

        with patch("core.meta_agent._META_DIR", meta_dir):
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

    def test_with_tool_calls(self, agent):
        tcs = [{"id": "tc1", "name": "list_projects", "args": {}}]
        msg = agent._build_assistant_msg("", tcs)
        assert "tool_calls" in msg
        assert msg["tool_calls"][0]["function"]["name"] == "list_projects"
