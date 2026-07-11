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
        """The `test` step must route on the run_tests `passed` flag — pass →
        `done`, fail → loop back to `implement` with a max_loop guard — NOT
        transition to null unconditionally (the bug: a failing suite didn't
        block completion)."""
        path = ROOT / "configs" / "coding_impl.yaml"
        graph = PipelineGraph.from_yaml(path)
        test = next(s for s in graph.steps if s.id == "test")

        pass_edges = [t for t in test.transitions
                      if t.match and t.match.get("passed") is True]
        fail_edges = [t for t in test.transitions
                      if t.match and t.match.get("passed") is False]
        assert len(pass_edges) == 1 and pass_edges[0].to == "done"
        assert len(fail_edges) == 1 and fail_edges[0].to == "implement"
        # Bounded loop — no infinite churn when the suite never passes.
        assert fail_edges[0].max_loop is not None
        # No unconditional escape hatch that would let a failing test through.
        assert not any(t.to is None and not t.match for t in test.transitions)

    def test_coding_impl_end_condition_is_outside_the_loop(self):
        """The terminating `node_reached` must fire on `done` (a loop-external
        terminal gate), never on `test`. `test` sits inside the implement↔test
        loop, so on a loop-back the graph re-enters it while a stale `completed`
        `test` row exists — a `node_reached: test` end-condition would then
        complete the run as SUCCESS before the retried test runs."""
        path = ROOT / "configs" / "coding_impl.yaml"
        graph = PipelineGraph.from_yaml(path)
        node_reached = [c for c in graph.end_conditions.conditions
                        if c.type == "node_reached"]
        assert node_reached, "coding_impl lost its node_reached end-condition"
        for c in node_reached:
            assert c.node == "done", f"end-condition on '{c.node}', must be 'done'"
        done = next(s for s in graph.steps if s.id == "done")
        assert done.step_type == "gate"
        assert [t.to for t in done.transitions] == [None]  # terminal

    def test_coding_impl_loop_is_bounded(self):
        """The implement↔test loop must be bounded so a never-passing suite
        fails instead of looping forever. The bound is `max_loop` on the
        test → implement edge (enforced on tool-step edges since skillflow-py
        1.5.2)."""
        path = ROOT / "configs" / "coding_impl.yaml"
        graph = PipelineGraph.from_yaml(path)
        test = next(s for s in graph.steps if s.id == "test")
        fail_edge = next(t for t in test.transitions
                         if t.match and t.match.get("passed") is False)
        assert fail_edge.max_loop and fail_edge.max_loop > 0, \
            "coding_impl loop is unbounded (test → implement has no max_loop)"

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
    5_review passed:true → `done` (a gate: no completed row, fires at most once).
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

    def test_review_pass_routes_to_done_fail_loops(self):
        g = self._graph()
        review = next(n for n in g.steps if n.id == "5_review")
        pass_edge = next(t for t in review.transitions
                         if t.match and t.match.get("value") is True)
        fail_edge = next(t for t in review.transitions
                         if t.match and t.match.get("value") is False)
        assert pass_edge.to == "done", "passed:true must route to the done gate"
        assert fail_edge.to == "3", "passed:false must loop back (not terminate)"
