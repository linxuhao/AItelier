# tests/unit/test_planner_prompt_guardrails.py
# The task planner writes the only brief the implementer ever sees: t_impl has
# no network and no shell, so a plan that defers part of the work to a URL, or
# makes "byte-for-byte identical to upstream" the acceptance bar, gives the
# implementer a goal it cannot verify or even look at. One such plan burned a
# whole 65k-token output budget on reasoning and emitted zero tool calls.
# These tests pin the guardrail text and the premise it rests on.

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


def _t_plan_output_fixed() -> dict:
    graph = yaml.safe_load(
        (ROOT / "configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    step = next(s for s in graph["steps"] if s["id"] == "t_plan")
    return step["output"]["fixed"]


def test_planner_template_states_the_implementer_has_no_network_or_shell():
    text = (TEMPLATES / "task_plan.md").read_text(encoding="utf-8")
    assert "没有网络" in text and "没有 shell" in text
    # Named concretely, not abstractly: a planner that knows the actual tool
    # list will not write a plan that assumes a missing one.
    for tool in ("read", "list_tree", "create", "edit", "test_write", "delete_file"):
        assert tool in text, f"planner template never names implementer tool '{tool}'"


def test_planner_template_forbids_deferring_work_to_a_url():
    text = (TEMPLATES / "task_plan.md").read_text(encoding="utf-8")
    assert "URL" in text
    assert "甩给" in text, "no explicit prohibition on deferring work to a link"


def test_planner_template_forbids_byte_exact_acceptance_but_keeps_signatures():
    text = (TEMPLATES / "task_plan.md").read_text(encoding="utf-8")
    assert "byte-for-byte" in text, "byte-exact acceptance is never ruled out"
    # The distinction that must survive: signatures verbatim (the contract the
    # implementer cannot look up) / bodies written from the behavioural contract.
    assert "签名照抄，实现体不抄" in text


def test_research_notes_slot_keeps_verbatim_signatures():
    fmt = _t_plan_output_fixed()["research_notes"]["format"]
    assert "verbatim signatures" in fmt
    assert "NOT prose summaries" in fmt


def test_research_notes_slot_must_be_self_contained_and_not_byte_exact():
    fmt = _t_plan_output_fixed()["research_notes"]["format"]
    assert "no web access" in fmt
    assert "SELF-CONTAINED" in fmt
    assert "never defer to a link" in fmt
    assert "byte-for-byte" in fmt


def test_plan_reviewer_checks_the_implementer_capability_boundary():
    text = (TEMPLATES / "task_plan_red.md").read_text(encoding="utf-8")
    assert "byte-for-byte" in text
    assert "没有网络" in text


def test_implementer_role_really_has_no_network_or_shell_tools():
    """The premise the planner guardrail asserts. If t_impl ever gains a web or
    shell tool, this fails so the planner prompt gets corrected with it."""
    roles = yaml.safe_load(
        (ROOT / "agent_configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    tools = set(roles["task_implementer"]["tools"])
    assert not (tools & {"web_search", "web_fetch", "bash", "shell", "run_tests"}), \
        f"task_implementer gained a network/shell tool: {sorted(tools)}"
