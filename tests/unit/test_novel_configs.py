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


def test_novel_init_reviews_before_human_checkpoint():
    # Order must be design → design_review(Red) → design_gate(human checkpoint)
    # → scaffold. The checkpoint lives on design_gate (reached only on Red-pass),
    # NOT on design — so the human is asked once Red is clean, not every loop.
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_init.yaml")
    resolver = GraphResolver(graph)

    # design no longer checkpoints; it flows straight to review.
    design = resolver.get_node("design")
    assert design.checkpoint is False
    assert resolver.next_node("design", {}, {}) == "design_review"

    # Red verdict edges match on the review_verdict.json file (not raw flags),
    # so assert them structurally: pass → human gate, fail → autonomous re-design.
    review = resolver.get_node("design_review")
    routes = {t.match.get("value"): t.to for t in review.transitions
              if t.match and t.match.get("from_file")}
    assert routes.get(True) == "design_gate"
    assert routes.get(False) == "design"

    # The human checkpoint is on design_gate (a restage tool step), reject→design.
    gate = resolver.get_node("design_gate")
    assert gate.checkpoint is True and gate.step_type == "tool"
    assert gate.tool_name == "restage" and gate.checkpoint_reject_to == "design"
    approve_edge = [t for t in gate.transitions
                    if t.match and t.match.get("from") == "checkpoint"][0]
    assert approve_edge.to == "scaffold"


def test_novel_chapter_reviews_outline_before_human_checkpoint():
    # Same review-before-checkpoint order as novel_init: outline →
    # outline_review(Red) → outline_gate(human checkpoint) → draft. The checkpoint
    # is on outline_gate (reached only on Red-pass), NOT on outline.
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)

    outline = resolver.get_node("outline")
    assert outline.checkpoint is False
    assert resolver.next_node("outline", {}, {}) == "outline_review"

    review = resolver.get_node("outline_review")
    routes = {t.match.get("value"): t.to for t in review.transitions
              if t.match and t.match.get("from_file")}
    assert routes.get(True) == "outline_gate"
    assert routes.get(False) == "outline"

    gate = resolver.get_node("outline_gate")
    assert gate.checkpoint is True and gate.step_type == "tool"
    assert gate.tool_name == "restage" and gate.checkpoint_reject_to == "outline"
    approve = [t for t in gate.transitions
               if t.match and t.match.get("from") == "checkpoint"][0]
    assert approve.to == "draft"

    # finalize's human checkpoint (CP#2) is reached after the reviews + polish.
    assert resolver.get_node("finalize").checkpoint is True


def test_novel_chapter_reviews_substance_before_humanize():
    # Fail-fast order: draft → draft_review(substance) → humanize(去AI味) →
    # continuity(machine gate) → finalize. Review BEFORE polish so a substance
    # reject re-writes the draft without wasting a humanize pass.
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)

    assert resolver.next_node("draft", {}, {}) == "draft_review"
    dr = {t.match.get("value"): t.to
          for t in resolver.get_node("draft_review").transitions
          if t.match and t.match.get("from_file")}
    assert dr.get(True) == "humanize"   # substance pass → polish
    assert dr.get(False) == "draft"     # substance fail → rewrite (no wasted humanize)
    assert resolver.next_node("humanize", {}, {}) == "continuity"


def test_continuity_gate_loops_back_to_humanize():
    # continuity is now AFTER humanize: pass → finalize, fail → humanize
    # (re-polish; AI-ism is humanize's job, not a re-write).
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)
    assert resolver.next_node("continuity", {"passed": True}, {}) == "finalize"
    assert resolver.next_node("continuity", {"passed": False}, {}) == "humanize"
    node = resolver.get_node("continuity")
    fail_edge = [t for t in node.transitions if t.to == "humanize"][0]
    assert fail_edge.max_loop == 2 and fail_edge.feedback is True
