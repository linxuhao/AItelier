# tests/unit/test_novel_configs.py
# Static validation of the novel pipeline graphs (novel_init / novel_chapter):
# graph structure, transition targets, cycle safety, agent_config references
# resolving to agent_configs/*.yaml roles, referenced templates and tools
# actually existing on disk.

from pathlib import Path

import pytest
import yaml

from skillflow.graph import PipelineGraph, GraphResolver

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ["novel_init.yaml", "novel_chapter.yaml"]


def _load_agent_roles() -> dict:
    roles = {}
    for p in (ROOT / "agent_configs").glob("*.yaml"):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        roles.update(data)
    return roles


@pytest.mark.parametrize("cfg", CONFIGS)
def test_graph_validates(cfg):
    graph = PipelineGraph.from_yaml(ROOT / "configs" / cfg)
    issues = GraphResolver(graph).validate()
    assert issues == [], f"{cfg}: {issues}"


@pytest.mark.parametrize("cfg", CONFIGS)
def test_agent_configs_and_templates_exist(cfg):
    graph = PipelineGraph.from_yaml(ROOT / "configs" / cfg)
    roles = _load_agent_roles()
    for step in graph.steps:
        if step.step_type != "agent":
            continue
        assert step.agent_config in roles, \
            f"{cfg}: step '{step.id}' references unknown agent_config " \
            f"'{step.agent_config}'"
        template = roles[step.agent_config].get("template")
        assert template and (ROOT / "templates" / template).is_file(), \
            f"{cfg}: role '{step.agent_config}' template '{template}' missing"


@pytest.mark.parametrize("cfg", CONFIGS)
def test_tool_steps_exist_on_disk(cfg):
    graph = PipelineGraph.from_yaml(ROOT / "configs" / cfg)
    for step in graph.steps:
        if step.step_type != "tool":
            continue
        tool_dir = ROOT / "aitelier" / "tools" / step.tool_name
        assert (tool_dir / "impl.py").is_file() and (tool_dir / "tool.yaml").is_file(), \
            f"{cfg}: step '{step.id}' tool '{step.tool_name}' not found in aitelier/tools/"


def test_probe_routes_to_outline():
    # arc_plan/ctx_pull removed — probe now assembles context then goes straight
    # to the outline (which does the full per-chapter planning).
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)
    assert resolver.next_node("probe", {}, {}) == "outline"
    ids = {s.id for s in graph.steps}
    assert "arc_plan" not in ids and "ctx_pull" not in ids and "apply_arc" not in ids


def test_continuity_gate_loops_back_with_limit():
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)
    assert resolver.next_node("continuity", {"passed": True}, {}) == "draft_review"
    assert resolver.next_node("continuity", {"passed": False}, {}) == "draft"
    node = resolver.get_node("continuity")
    fail_edge = [t for t in node.transitions if t.to == "draft"][0]
    assert fail_edge.max_loop == 2 and fail_edge.feedback is True
