from pathlib import Path

import yaml

from core.agents import AgentFactory
from skillflow.agent_registry import AgentRegistry
from skillflow.graph import PipelineGraph


ROOT = Path(__file__).resolve().parent.parent.parent

# The reviewed implementer budget. Raised 100 -> 200 on 2026-09-22 after six
# rounds died at the wall in one day. A literal is correct HERE — this test's job
# is to pin the reviewed number in one place — but nowhere else.
REVIEWED_BUDGET = 200

IMPLEMENTERS = {
    "subagent": ("subagent_worker", "work"),
    "coding_impl": ("offload_implementer", "implement"),
    "dpe_default": ("task_implementer", "t_impl"),
}


def test_implementer_turn_budgets_match_the_reviewed_budget():
    registry = AgentRegistry()

    for config_name, (role, _step_id) in IMPLEMENTERS.items():
        roles = yaml.safe_load(
            (ROOT / "agent_configs" / f"{config_name}.yaml").read_text(
                encoding="utf-8"))
        role_config = dict(roles[role])
        assert role_config["max_tool_turns"] == REVIEWED_BUDGET

        registry.register(
            role,
            model=role_config.pop("model"),
            tools=role_config.pop("tools"),
            **role_config,
        )

    factory = AgentFactory(registry=registry)
    for role, _step_id in IMPLEMENTERS.values():
        assert factory.get_max_tool_turns(role) == REVIEWED_BUDGET


def test_dpe_runtime_override_matches_the_implementer_budget():
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "dpe_default.yaml")
    step = next(step for step in graph.steps if step.id == "t_impl")

    roles = yaml.safe_load(
        (ROOT / "agent_configs" / "dpe_default.yaml").read_text(encoding="utf-8"))

    assert step.agent_config == "task_implementer"
    # Compare the two SIDES, not each side against a literal. A step-level
    # override beats the role config, so a literal here let the graph keep 100
    # while the role moved to 200 and this test — named for the match — passed.
    assert step.max_tool_turns == roles["task_implementer"]["max_tool_turns"]
