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

    # CP#2 moved off finalize onto final_gate (review-before-checkpoint,
    # third instance of the pattern) — see the dedicated journal-audit test.
    assert resolver.get_node("finalize").checkpoint is False
    assert resolver.get_node("final_gate").checkpoint is True


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


def test_humanize_fidelity_review_closes_the_chain_of_custody():
    # Red approves the DRAFT but the reader gets chapter_final — humanize_review
    # is the only thing verifying they're the same story. Cheap mechanical gate
    # runs first (fail-fast), then the A/B fidelity diff; either failure loops
    # back to humanize to re-polish from the intact draft.
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)

    hr = resolver.get_node("humanize_review")
    assert hr.step_type == "agent" and hr.checkpoint is False
    assert hr.agent_config == "novel_humanize_reviewer"
    # it must see BOTH sides to diff them
    srcs = {(s.get("source") or s).get("step") for s in hr.context}
    assert {"draft", "humanize"} <= srcs

    routes = {t.match.get("value"): t.to for t in hr.transitions
              if t.match and t.match.get("from_file")}
    assert routes.get(True) == "finalize"
    assert routes.get(False) == "humanize"   # not draft: polish drift → re-polish

    # humanize gets the fidelity evidence on the re-polish loop
    h_srcs = {(s.get("source") or s).get("step")
              for s in resolver.get_node("humanize").context}
    assert "humanize_review" in h_srcs


def test_continuity_gate_loops_back_to_humanize():
    # continuity is now AFTER humanize: pass → the fidelity review, fail →
    # humanize (re-polish; AI-ism is humanize's job, not a re-write).
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)
    assert resolver.next_node("continuity", {"passed": True}, {}) == "humanize_review"
    assert resolver.next_node("continuity", {"passed": False}, {}) == "humanize"
    node = resolver.get_node("continuity")
    fail_edge = [t for t in node.transitions if t.to == "humanize"][0]
    assert fail_edge.max_loop == 2 and fail_edge.feedback is True

def test_reviewers_guard_user_feedback_rounds():
    # Every reviewer that gates a human-checkpoint reject loop must SEE the
    # accumulated user feedback ({feedback_of: step}) — otherwise a revision
    # that silently reverts an earlier round's fix passes review unchallenged
    # (live incident: outline v3 pasted the round-1 quote back verbatim and
    # Red approved it, blind).
    cases = [
        ("novel_chapter.yaml", "outline_review", "outline"),
        ("novel_chapter.yaml", "draft_review", "draft"),
        ("novel_init.yaml", "design_review", "design"),
    ]
    for cfg, reviewer, target in cases:
        graph = PipelineGraph.from_yaml(ROOT / "configs" / cfg)
        node = GraphResolver(graph).get_node(reviewer)
        fb = [s for s in node.context if s.get("feedback_of") == target]
        assert fb, f"{cfg}: {reviewer} must declare {{feedback_of: {target}}}"
        assert fb[0].get("source_type") == "feedback", \
            f"{cfg}: {reviewer} feedback_of spec not normalized to 'feedback'"

def test_journal_audit_gates_the_booking():
    # The journal (chapter_events.json) permanently mutates bible balances at
    # apply_state, and finalize was the ONLY unreviewed agent output in the
    # chain. Order: finalize → finalize_review(Red audit, prose is the source
    # of truth) → final_gate(human CP#2, restage prose+journal+verdict) →
    # apply_state. Audit fail → re-book (finalize), NOT re-write (draft);
    # human reject → draft (正文返工重走全链).
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")
    resolver = GraphResolver(graph)

    assert resolver.next_node("finalize", {}, {}) == "finalize_review"

    fr = resolver.get_node("finalize_review")
    assert fr.agent_config == "novel_finalize_reviewer"
    srcs = {(s.get("source") or s).get("step") for s in fr.context}
    assert {"probe", "outline", "humanize", "finalize"} <= srcs  # 分录+真相源都要在场

    # finalize must SEE this chapter's user feedback: world-rule verdicts in it
    # get booked into the bible (world_setting / golden_finger entries) — the
    # next chapter is a fresh project+run whose probe only feeds the bible, so
    # an unbooked rule is a rule the next writer never learns. (project=章,
    # so feedback_of is chapter-scoped by construction.)
    fin = resolver.get_node("finalize")
    fb_targets = {c.get("feedback_of") for c in fin.context if c.get("feedback_of")}
    assert {"outline", "draft"} <= fb_targets

    routes = {t.match.get("value"): t.to for t in fr.transitions
              if t.match and t.match.get("from_file")}
    assert routes.get(True) == "final_gate"
    assert routes.get(False) == "finalize"
    fail_edge = [t for t in fr.transitions if t.to == "finalize"][0]
    assert fail_edge.max_loop == 2

    gate = resolver.get_node("final_gate")
    assert gate.checkpoint is True and gate.tool_name == "restage"
    assert gate.checkpoint_reject_to == "draft"
    assert set(gate.tool_params["from_steps"]) == {"humanize", "finalize", "finalize_review"}
    approve = [t for t in gate.transitions
               if t.match and t.match.get("from") == "checkpoint"][0]
    assert approve.to == "apply_state"
