# tests/unit/test_agents_factory.py
# v2: AgentFactory reads from skillflow's AgentRegistry.

import pytest
from unittest.mock import patch, MagicMock
from core.agents import AgentFactory
from skillflow.agent_registry import AgentRegistry

MOCK_TEMPLATE_CONTENT = "# Test Template\nYou are a test agent."


@pytest.fixture
def registry():
    reg = AgentRegistry()
    reg.register("researcher", model="deepseek/deepseek-v4-flash",
                 template="step1_5_researcher.md",
                 tools=["web_search", "web_fetch"],
                 thinking={"enable": True, "effort": "max"})
    reg.register("researcher_reviewer", model="minimax/MiniMax-M3",
                 template="step1_5_researcher_red.md",
                 tools=[])
    reg.register("task_implementer", model="minimax/MiniMax-M3",
                 template="task_implementer.md",
                 tools=["read_file", "list_tree", "write"],
                 max_tool_turns=15)
    return reg


@pytest.fixture
def factory(registry):
    f = AgentFactory(registry=registry)
    f._load_template = MagicMock(return_value=MOCK_TEMPLATE_CONTENT)
    return f


class TestAgentFactoryV2:
    def test_get_agent_by_name(self, factory):
        agent = factory.get_agent("researcher")
        assert MOCK_TEMPLATE_CONTENT in agent.system_prompt
        assert agent.gateway.litellm_model.endswith("deepseek-v4-flash")

    def test_get_agent_missing_config(self, factory):
        with pytest.raises(ValueError, match="not found"):
            factory.get_agent("nonexistent")

    def test_get_agent_tools(self, factory):
        tools = factory.get_agent_tools("researcher")
        assert "web_search" in tools
        assert "write" not in tools

    def test_get_agent_tools_empty(self, factory):
        tools = factory.get_agent_tools("researcher_reviewer")
        assert tools == []

    def test_get_max_tool_turns(self, factory):
        assert factory.get_max_tool_turns("task_implementer") == 15

    def test_get_max_tool_turns_default(self, factory):
        assert factory.get_max_tool_turns("researcher") == 10

    def test_get_agent_missing_config_raises(self, factory):
        with pytest.raises(ValueError, match="not found"):
            factory.get_agent("nonexistent_role")

    def test_get_max_retries(self, factory):
        assert factory.get_max_retries("any_step") == 3
