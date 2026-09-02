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
    for tool in ("read", "list_tree", "create", "edit", "test_write", "repo_remove_file"):
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


# ── Templates may only name tools their own role can actually call ──────
# This class of defect showed up three times: task_plan.md told the planner to
# explore with `read_file` (a REGISTRY tool it was never granted — one planner
# obeyed and got a tool-not-allowed error), and step3_pm.md said the same to
# the PM. The gated trio is named `read`/`search`/`list` and is DERIVED from a
# step's context specs, so the confusion is easy to repeat.
#
# Registry tools are gated by the role's `tools:` list; the derived trio and the
# create/edit/write slots are not, so only registry names are checked here.

# (template, tool) pairs where the name is deliberately mentioned for a role
# OTHER than the reader — a denial ("you do NOT have `write`"), another role's
# toolset, or engine machinery the step never invokes itself.
_NAMED_BUT_NOT_CALLED = {
    ("task_plan.md", "test_write"),          # describing t_impl's toolset
    ("task_plan.md", "read_test_written"),
    ("task_plan.md", "repo_remove_file"),
    ("task_plan_red.md", "test_write"),      # same list, from the reviewer side
    ("task_plan_red.md", "read_test_written"),
    ("task_plan_red.md", "repo_remove_file"),
    ("task_implementer.md", "write"),        # "你没有整文件覆写的 `write` 工具"
    ("step5_verifier.md", "repo_apply"),     # on_deliver hook, not an agent call
}


def _registry_tool_names() -> set[str]:
    # Located through the installed package, not a checkout path: skillflow is
    # an editable install on the host and a PyPI wheel in the container.
    import skillflow
    names = {p.name for p in (ROOT / "aitelier" / "tools").iterdir() if p.is_dir()}
    names |= {p.name for p in (Path(skillflow.__file__).parent / "tools").iterdir()
              if p.is_dir() and not p.name.startswith("__")}
    return names


def test_dpe_templates_only_name_registry_tools_their_role_has():
    import re
    registry = _registry_tool_names()
    roles = yaml.safe_load(
        (ROOT / "agent_configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    offenders = []
    for role, cfg in roles.items():
        if not isinstance(cfg, dict) or not cfg.get("template"):
            continue
        tpl = cfg["template"]
        path = TEMPLATES / tpl
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        named = set(re.findall(r"`([a-z_]+)`", text))
        for tool in sorted((named & registry) - set(cfg.get("tools") or [])):
            if (tpl, tool) not in _NAMED_BUT_NOT_CALLED:
                offenders.append(f"{role}/{tpl} names `{tool}`")
    assert not offenders, (
        "template names a registry tool the role cannot call: " + "; ".join(offenders))


def test_planner_template_does_not_send_the_planner_to_read_file():
    """`read_file` is registry-gated and task_planner has web/list_tree only."""
    text = (TEMPLATES / "task_plan.md").read_text(encoding="utf-8")
    assert "read_file" not in text


class TestReadDisciplineSurvivesInBothPlaces:
    """Narrow reads are stated twice on purpose. Do not de-duplicate.

    NUMBERS CORRECTED 2026-08-30, same day. The first version of this docstring
    claimed 0 scoped calls out of 11 as the baseline. That was measured by
    replaying four real `t_impl` steps and reading only TURN 1, where a model
    orients broadly before narrowing — it is not how the step behaves. Across
    all turns of real production traffic the picture is completely different:

        qwen/qwen3.8-flash ....  730 / 1,010 scoped  72.3%   105 steps
        localqwen/qwen3 .......  437 /   676         64.6%    73 steps
        ark/deepseek-v4-flash . 1,284 / 2,509        51.2%   314 steps
        opencodego/…-v4-flash .  331 /   671         49.3%    94 steps

    So localqwen already scopes ~65% of its reads unaided, and the effect of
    this guidance is UNPROVEN: the one production step after it shipped scored
    5/5, inside a pre-existing 20-94% per-step range. It is kept because it is
    a few hundred characters and cannot hurt, not because it is measured to
    help — and the honest metric is the spill rate (`context-failover` events
    per t_impl step, 23% at baseline), not counting tool calls.

    Two things the correction does NOT overturn. Position still matters: the
    guidance existed in the role template all along, 78k chars into the system
    message as the 6th bullet of a section about WRITING, and restating it at
    the end of the user message is the same recency effect `dpe_pipeline`
    already documents for `[Language]`. And the interesting difference is
    Qwen-vs-DeepSeek (72/65 vs 51/49), not Flash-Next-vs-27B — which points at
    routing rather than at either the prompt or a bigger GPU.

    Deleting either copy still reads as tidying, so both stay pinned.
    """

    def test_the_role_template_names_the_mechanism(self):
        """'read the part you need' was already there and was ignored 11/11.
        Naming the actual parameters is what changed anything."""
        t = (ROOT / "templates" / "task_implementer.md").read_text(encoding="utf-8")
        assert "start_line" in t and "end_line" in t, (
            "the template must name the ranged-read parameters, not merely "
            "advise against reading whole files")
        assert "context_lines" in t and "glob" in t, (
            "the template must name search's narrowing parameters")

    def test_the_turn_budget_block_restates_it(self):
        """The end of the user message is the position that actually landed."""
        src = (ROOT / "core" / "dpe_pipeline.py").read_text(encoding="utf-8")
        # There are TWO turn-budget blocks: the JSON-mode one ("{remaining}
        # remaining") and the native one. `t_impl` is native, and native is
        # where the 0/11 -> 5/9 was measured, so pin that one specifically —
        # matching the first occurrence tested the wrong code path.
        i = src.find("turns total, then forced output")
        assert i > 0, "the NATIVE turn-budget block moved or was renamed"
        block = src[i:i + 3000]
        assert "start_line" in block and "context_lines" in block, (
            "read-discipline was dropped from the turn-budget block — measured "
            "worth 7 of 10 scoped tool calls; see this class's docstring")
