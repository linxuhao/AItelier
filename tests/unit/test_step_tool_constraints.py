# tests/unit/test_step_tool_constraints.py
# Verify skillflow generates correct _tool_schemas per step from graph config.
# Tool definitions are now handled by skillflow (native function calling or
# JSON-mode dispatch).  The prompt assembler no longer formats tools —
# skillflow's _tool_schemas injection carries everything the host needs.

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


# ── Helpers ──────────────────────────────────────────────────────────

_AGENT_TOOLS = {
    "researcher":            ["web_search", "web_fetch"],
    "researcher_reviewer":   [],
    "architect":             ["web_search", "web_fetch", "read_file", "list_tree"],
    "architect_reviewer":    [],
    "pm":                    ["web_search", "web_fetch", "read_file", "list_tree"],
    "pm_reviewer":           [],
    "task_planner":          ["web_search", "web_fetch", "read_file", "list_tree"],
    "task_planner_reviewer": [],
    "task_implementer":      ["read_file", "list_tree", "write"],
    "task_implementer_reviewer": [],
    "task_verifier":         ["read_file", "list_tree"],
    "task_verifier_reviewer": [],
    "final_verifier":        ["read_file", "list_tree"],
    "final_verifier_reviewer": [],
}


def _get_tool_schemas_for_step(step_id: str) -> dict:
    """Compute merged tool_schemas skillflow would generate for a step."""
    from skillflow import PipelineGraph
    from skillflow.agent_registry import AgentRegistry
    from skillflow.write_tools import generate_write_tool_schemas

    graph = PipelineGraph.from_yaml(ROOT / "configs" / "dpe_default.yaml")
    registry = AgentRegistry()
    for name, tools in _AGENT_TOOLS.items():
        registry.register(name, tools=tools)

    node = next((s for s in graph.steps if s.id == step_id), None)
    if not node:
        return {}

    schemas: dict = {}
    if node.agent_config and node.agent_config in registry:
        ac = registry.get(node.agent_config)
        if ac:
            from skillflow.tool_loader import ToolLoader
            import skillflow as sf_pkg
            loader = ToolLoader(
                Path(sf_pkg.__file__).parent / "tools",       # skillflow native
                ROOT / "aitelier" / "tools",                   # AItelier custom
            )
            for tname in ac.tools:
                try:
                    schemas[tname] = loader.load_schema(tname)
                except Exception:
                    pass

    if node.output_mode and node.output_fixed:
        for ws in generate_write_tool_schemas(node.output_mode, node.output_fixed):
            schemas[ws["name"]] = ws

    return schemas


# ── Expected constrained write tools per step ────────────────────────

CONSTRAINED_STEPS = {
    "1": ["write_sota"],
    "2": ["write_design", "write_linter_manifest"],
    # PM owns the project README (moved here from the verifier so step 5 stays
    # purely verification — emits only its verdict).
    "3": ["write_tasks_manifest", "write_task_card", "write_readme"],
    "5": ["write_report"],
    "t_plan": ["write_plan", "write_subtask_manifest", "write_subtask_card", "write_research_notes"],
}


class TestSkillflowToolSchemas:
    """Verify skillflow generates correct _tool_schemas from graph config."""

    @pytest.mark.parametrize("step_id", list(CONSTRAINED_STEPS.keys()))
    def test_constrained_step_has_write_tools(self, step_id):
        ts = _get_tool_schemas_for_step(step_id)
        write_tools = sorted(k for k in ts if k.startswith("write_"))
        expected = sorted(CONSTRAINED_STEPS[step_id])
        assert write_tools == expected, f"{step_id}: expected {expected}, got {write_tools}"

    @pytest.mark.parametrize("step_id", ["2", "3", "5", "t_plan"])
    def test_constrained_step_has_read_tools(self, step_id):
        ts = _get_tool_schemas_for_step(step_id)
        read_tools = {k for k in ts if not k.startswith("write")}
        assert read_tools, f"{step_id}: should have read/exploration tools"

    def test_t_impl_has_generic_write(self):
        ts = _get_tool_schemas_for_step("t_impl")
        assert "write" in ts, f"t_impl should have generic write tool, got {sorted(ts)}"
        assert "read_file" in ts
        assert "list_tree" in ts

    def test_agent_config_tools_are_included(self):
        """Tools from agent config (web_search, web_fetch) are in schemas."""
        ts = _get_tool_schemas_for_step("1")  # researcher has web_search + web_fetch
        assert "web_search" in ts
        assert "web_fetch" in ts
