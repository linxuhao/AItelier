# tests/unit/test_max_tool_turns.py
# v2: AgentFactory.get_max_tool_turns() reads from skillflow AgentRegistry.

import pytest
from core.agents import AgentFactory
from skillflow.agent_registry import AgentRegistry


@pytest.fixture
def registry():
    reg = AgentRegistry()
    reg.register("task_implementer", model="minimax/MiniMax-M3",
                 template="task_implementer.md",
                 tools=["read_file", "write"],
                 max_tool_turns=15)
    reg.register("researcher", model="deepseek/deepseek-v4-flash",
                 template="step1_5_researcher.md",
                 tools=["web_search"])
    return reg


@pytest.fixture
def factory(registry):
    return AgentFactory(registry=registry)


class TestGetMaxToolTurns:
    def test_returns_configured_value(self, factory):
        assert factory.get_max_tool_turns("task_implementer") == 15

    def test_returns_default_when_not_configured(self, factory):
        assert factory.get_max_tool_turns("researcher") == 10

    def test_returns_default_for_unknown(self, factory):
        assert factory.get_max_tool_turns("nonexistent") == 10

    def test_default_constant_value(self):
        assert AgentFactory.DEFAULT_MAX_TOOL_TURNS == 10
