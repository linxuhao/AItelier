from pathlib import Path

import yaml

from core.agents import AgentFactory
from skillflow.agent_registry import AgentRegistry
from skillflow.graph import PipelineGraph


ROOT = Path(__file__).resolve().parent.parent.parent

IMPLEMENTERS = {
    "subagent": ("subagent_worker", "work"),
    "coding_impl": ("offload_implementer", "implement"),
    "dpe_default": ("task_implementer", "t_impl"),
}


def test_implementer_turn_budgets_are_100_through_config_and_registry():
    registry = AgentRegistry()

    for config_name, (role, _step_id) in IMPLEMENTERS.items():
        roles = yaml.safe_load(
            (ROOT / "agent_configs" / f"{config_name}.yaml").read_text(
                encoding="utf-8"))
        role_config = dict(roles[role])
        assert role_config["max_tool_turns"] == 100

        registry.register(
            role,
            model=role_config.pop("model"),
            tools=role_config.pop("tools"),
            **role_config,
        )

    factory = AgentFactory(registry=registry)
    for role, _step_id in IMPLEMENTERS.values():
        assert factory.get_max_tool_turns(role) == 100


def test_dpe_runtime_override_matches_the_implementer_budget():
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "dpe_default.yaml")
    step = next(step for step in graph.steps if step.id == "t_impl")

    assert step.agent_config == "task_implementer"
    assert step.max_tool_turns == 100
