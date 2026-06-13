"""Tests for v2 config loading and validation."""

import pytest
from pathlib import Path
from skillflow.graph import PipelineGraph


HERE = Path(__file__).parent
ROOT = HERE.parent.parent


class TestV2ConfigLoading:
    def test_agent_config_references_in_graph(self):
        """All agent_config references in the graph use known agent names."""
        import yaml
        path = ROOT / "configs" / "dpe_default.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        agent_names = {
            "researcher", "researcher_reviewer", "architect", "architect_reviewer",
            "pm", "pm_reviewer", "task_planner", "task_planner_reviewer",
            "task_implementer", "task_implementer_reviewer",
            "task_verifier", "task_verifier_reviewer",
            "final_verifier", "final_verifier_reviewer",
        }
        for step in data["steps"]:
            ac = step.get("agent_config")
            if ac:
                assert ac in agent_names, f"Unknown agent_config: {ac}"

    def test_v2_graph_parses(self):
        """configs/dpe_default.yaml loads as a valid PipelineGraph."""
        path = ROOT / "configs" / "dpe_default.yaml"
        graph = PipelineGraph.from_yaml(path)
        assert graph.name == "dpe_default_v2"
        assert graph.begin == "1"
        assert len(graph.steps) > 0

    def test_v2_graph_has_review_steps(self):
        """v2 graph includes review steps for green/red pattern."""
        path = ROOT / "configs" / "dpe_default.yaml"
        graph = PipelineGraph.from_yaml(path)
        step_ids = {s.id for s in graph.steps}
        for review_id in ("1_review", "2_review", "3_review", "5_review"):
            assert review_id in step_ids, f"Missing review step: {review_id}"

    def test_v2_graph_review_loops(self):
        """Every *_review node with fail transition loops back with max_loop."""
        path = ROOT / "configs" / "dpe_default.yaml"
        graph = PipelineGraph.from_yaml(path)
        for s in graph.steps:
            if s.id.endswith("_review"):
                fail_transitions = [
                    t for t in s.transitions
                    if t.match and t.match.get("passed") is False
                ]
                for t in fail_transitions:
                    assert t.to is not None
                    assert t.max_loop is not None, (
                        f"{s.id} → {t.to} fail edge missing max_loop"
                    )

    def test_v2_graph_validates(self):
        """v2 graph passes structural validation."""
        path = ROOT / "configs" / "dpe_default.yaml"
        graph = PipelineGraph.from_yaml(path)
        issues = graph.validate()
        assert issues == [], f"Graph validation issues: {issues}"

    def test_meta_conversation_graph_parses(self):
        """configs/meta_conversation.yaml loads as a valid PipelineGraph."""
        path = ROOT / "configs" / "meta_conversation.yaml"
        graph = PipelineGraph.from_yaml(path)
        assert graph.name == "meta_conversation"
        step_ids = {s.id for s in graph.steps}
        assert "intent_detect" in step_ids
        assert "meta" in step_ids

    def test_red_template_self_contained(self):
        """Red review templates contain review criteria sections."""
        tmpl_dir = ROOT / "templates"
        for tmpl_path in sorted(tmpl_dir.glob("*_red.md")):
            content = tmpl_path.read_text(encoding="utf-8")
            has_criteria = "审查" in content or "Review" in content or "review" in content
            assert has_criteria, (
                f"Red template {tmpl_path.name} has no review criteria"
            )
