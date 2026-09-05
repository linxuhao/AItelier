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
        assert graph.begin == "git_sync_pre"
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
        # Conversational redesign (2026-06-17): intent_detect -> gather
        # (checkpoint loop) -> finalize. The old single "meta" step is gone.
        assert "intent_detect" in step_ids
        assert "gather" in step_ids
        assert "finalize" in step_ids

    def test_coding_impl_graph_parses_and_validates(self):
        """configs/coding_impl.yaml loads as a valid PipelineGraph."""
        path = ROOT / "configs" / "coding_impl.yaml"
        graph = PipelineGraph.from_yaml(path)
        assert graph.name == "coding_impl"
        assert graph.begin == "implement"
        assert graph.validate() == []

    def test_coding_impl_test_step_gates_on_run_tests(self):
        """Only a written report enters validation; evidence and verdict are separate."""
        graph = PipelineGraph.from_yaml(ROOT / "configs" / "coding_impl.yaml")
        test = next(s for s in graph.steps if s.id == "test")
        assert test.tool_name == "run_tests"
        assert [(t.to, t.match) for t in test.transitions] == [
            ("test_evidence_missing", {"_error": True}),
            ("test_evidence", {"written": "test_report.json"}),
            ("test_evidence_missing", None),
        ]
        evidence = next(s for s in graph.steps if s.id == "test_evidence")
        assert evidence.tool_name == "json_schema"
        assert evidence.tool_params["workspace_root"] == "$CONFIG_DIR/test"
        assert evidence.tool_params["files"] == ["test_report.json"]
        assert [(t.to, t.match) for t in evidence.transitions] == [
            ("test_evidence_missing", {"_error": True}),
            ("test_outcome", {"all_passed": True}),
            ("test_evidence_missing", None),
        ]
        outcome = next(s for s in graph.steps if s.id == "test_outcome")
        assert outcome.tool_name == "json_schema"
        assert outcome.tool_params["workspace_root"] == "$CONFIG_DIR/test"
        assert outcome.tool_params["files"] == ["test_report.json"]
        assert [(t.to, t.match) for t in outcome.transitions] == [
            ("test_evidence_missing", {"_error": True}),
            ("done", {"all_passed": True}),
            ("implement", {"all_passed": False}),
        ]

    def test_coding_impl_end_condition_is_outside_the_loop(self):
        """Success and evidence failure have distinct loop-external terminals."""
        graph = PipelineGraph.from_yaml(ROOT / "configs" / "coding_impl.yaml")
        conditions = graph.end_conditions.conditions
        assert [(c.type, c.node, c.result) for c in conditions] == [
            ("node_reached", "done", "completed"),
            ("node_reached", "test_evidence_missing", "failed"),
        ]
        for terminal in ("done", "test_evidence_missing"):
            node = next(s for s in graph.steps if s.id == terminal)
            assert node.step_type == "gate"
            assert [t.to for t in node.transitions] == [None]

    def test_coding_impl_loop_is_bounded(self):
        """Evidence-free runs stop; genuine failures retain three repair attempts."""
        graph = PipelineGraph.from_yaml(ROOT / "configs" / "coding_impl.yaml")
        outcome = next(s for s in graph.steps if s.id == "test_outcome")
        fail_edge = next(t for t in outcome.transitions if t.to == "implement")
        assert fail_edge.match == {"all_passed": False}
        assert fail_edge.max_loop == 3

    def test_coding_impl_implement_receives_test_feedback(self):
        """On loop-back, implement must see the prior run's test report so it
        fixes the real failure — a context source referencing the `test` step."""
        import yaml
        path = ROOT / "configs" / "coding_impl.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        implement = next(s for s in data["steps"] if s["id"] == "implement")
        sources = [c.get("source", {}) for c in implement.get("context", [])]
        assert any(s.get("step") == "test" for s in sources), (
            "implement must pull the `test` step output as loop-back feedback")

    def test_red_template_self_contained(self):
        """Red review templates contain review criteria sections."""
        tmpl_dir = ROOT / "templates"
        for tmpl_path in sorted(tmpl_dir.glob("*_red.md")):
            content = tmpl_path.read_text(encoding="utf-8")
            has_criteria = "审查" in content or "Review" in content or "review" in content
            assert has_criteria, (
                f"Red template {tmpl_path.name} has no review criteria"
            )


class TestTerminalGateNoLatch:
    """Regression: the run must terminate at a loop-EXTERNAL `done` gate, not at
    `node_reached 5_review`.

    5_review is inside the goal loop. With `node_reached 5_review` as the end
    condition, once 5_review completed ONCE (even passed:false → loops to "3"),
    its completed row latched node_reached and the run terminated on the next
    loop iteration before re-verifying the fixed code — a live dpe_game run
    shipped a game with playtest passed:false as `completed`. The fix routes
    5_review passed:true → git_push_post → `done` (a gate: no completed row, fires at most once).
    """

    def _graph(self):
        path = ROOT / "configs" / "dpe_default.yaml"
        return PipelineGraph.from_yaml(path)

    def test_terminal_is_loop_external_done_gate(self):
        g = self._graph()
        done = next((n for n in g.steps if n.id == "done"), None)
        assert done is not None, "no loop-external `done` terminal node"
        assert done.step_type == "gate", "`done` must be a gate (no completed row to latch)"
        assert [t.to for t in done.transitions] == [None], "`done` must be terminal (to: null)"

    def test_end_condition_fires_on_done_not_review(self):
        g = self._graph()
        nodes = {c.node for c in g.end_conditions.conditions if c.type == "node_reached"}
        assert "done" in nodes, "end condition must fire on `done`"
        assert "5_review" not in nodes, "end must NOT fire on in-loop `5_review` (premature-latch trap)"

    def test_review_pass_pushes_then_reaches_done_fail_loops(self):
        g = self._graph()
        review = next(n for n in g.steps if n.id == "5_review")
        pass_edge = next(t for t in review.transitions
                         if t.match and t.match.get("value") is True)
        fail_edge = next(t for t in review.transitions
                         if t.match and t.match.get("value") is False)
        assert pass_edge.to == "git_push_post"
        push = next(n for n in g.steps if n.id == pass_edge.to)
        assert push.tool_name == "git_push_post"
        assert [(t.to, t.match) for t in push.transitions] == [("done", None)]
        assert fail_edge.to == "3", "passed:false must loop back (not terminate)"
