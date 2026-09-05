"""Regression tests for the pipeline-addon HOST layer (core/addon_registry.py).

The overlay MECHANICS (compose/describe/validate) now live in skillflow and are
tested in skillflow's test_overlay_registry.py. Here we test AItelier's half:
declaring addons to skillflow, delegating list/describe, and layering the
ConfigManifest onto a composed combo — against the REAL dpe_default_v2 base +
game_harness addon.
"""

import yaml
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from skillflow import SkillFlow, PipelineGraph
from skillflow.graph import GraphResolver
from core import addon_registry as ar

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGS = _ROOT / "configs"


@pytest.fixture
def sf_with_addons():
    """A real SkillFlow with agent configs + the dpe_default_v2 base + declared
    addons, patched in as the get_skillflow() singleton (list/describe delegate)."""
    sf = SkillFlow(":memory:")
    for f in sorted((_ROOT / "agent_configs").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            try:
                sf.register_agent_config_from_dict(name, cfg)
            except Exception:
                pass
    sf.register_graph(PipelineGraph.from_yaml(_CONFIGS / "dpe_default.yaml"))
    ar.declare_addons(sf)
    with patch("api.dependencies.get_skillflow", return_value=sf):
        yield sf


def test_declare_registers_overlays_with_skillflow(sf_with_addons):
    names = {o["name"] for o in sf_with_addons.list_overlays()}
    assert "game_harness" in names


def test_list_addons_delegates(sf_with_addons):
    gh = next((a for a in ar.list_addons() if a["name"] == "game_harness"), None)
    assert gh is not None
    assert gh["base"] == "dpe_default_v2"
    assert gh["alias"] == "dpe_game"
    assert gh["description"] and gh["when_to_use"]


def test_register_addon_combo_composes_and_manifests(sf_with_addons):
    sf = sf_with_addons
    reg = MagicMock()
    name = ar.register_addon_combo(sf, reg, "dpe_default_v2", ["game_harness"],
                                   name="dpe_game")
    assert name == "dpe_game"
    ids = {n.id for n in sf._graphs["dpe_game"].steps}
    assert {"5_compile", "gh_scaffold"} <= ids     # addon steps spliced
    assert {"1", "5_review"} <= ids                # base steps survive
    # the AItelier manifest is layered on, seeded from the base's hints
    reg.register_one.assert_called_once()
    hints = reg.register_one.call_args.kwargs.get("hint_overrides") or {}
    assert hints.get("seed_file") == "project_brief.md"


def test_register_addon_combo_auto_resolves_alias(sf_with_addons):
    # A single addon that declares an alias resolves to it even without an
    # explicit name — the blessed combo. (Emergent names for aliasless/multi-
    # addon combos are covered in skillflow's test_overlay_registry.)
    name = ar.register_addon_combo(sf_with_addons, None, "dpe_default_v2",
                                   ["game_harness"])
    assert name == "dpe_game"


def test_describe_config_delegates(sf_with_addons):
    assert ar.describe_config("dpe_game") == {
        "base": "dpe_default_v2", "addons": ["game_harness"]}
    assert ar.describe_config("dpe_default_v2") == {
        "base": "dpe_default_v2", "addons": []}
    assert ar.describe_config("dpe_default_v2__game_harness+mobile") == {
        "base": "dpe_default_v2", "addons": ["game_harness", "mobile"]}


def test_read_fragments_resolves_addon_files():
    frags = ar.read_fragments(["game_harness/architect.md"])
    assert len(frags) == 1
    label, content = next(iter(frags.items()))
    assert "game_harness/architect.md" in label
    assert "Godot" in content


def test_read_fragments_ignores_missing_and_escapes():
    frags = ar.read_fragments(["game_harness/nope.md", "../../etc/passwd"])
    assert frags == {}


def test_no_reader_of_5_compile_would_inline_the_rendered_frames(sf_with_addons):
    """5_compile's directory holds the 100+ PNGs the readability gate
    photographs. A whole-step context source rglob's them, reads each as
    UTF-8-with-replacement, and the prompt assembler cuts the block at
    MAX_CONTEXT_LINES — and sorted order (compile_report.json, frames/*.png,
    playtest_report.json) means the file that gets cut is ALWAYS the play-test
    report.

    Live, jinyong-encounter 2026-08-23: 5_review blocked the run on "playtest
    gate NOT RUN — playtest_report.json ABSENT" while the file sat on disk at
    98KB / 3526 lines with `passed: true` and 23 scenarios evaluated, and the PM
    planned the next round around a gate it believed had never run.

    So every reader of this step must name the file it wants.
    """
    sf = sf_with_addons
    ar.register_addon_combo(sf, MagicMock(), "dpe_default_v2", ["game_harness"],
                            name="dpe_game")
    naked = []
    for node in sf._graphs["dpe_game"].steps:
        for src in (getattr(node, "context", None) or []):
            d = src if isinstance(src, dict) else getattr(src, "__dict__", {})
            if d.get("step_id") != "5_compile" and d.get("step") != "5_compile":
                continue
            if not (d.get("output") or d.get("file") or d.get("files")):
                naked.append(node.id)
    assert not naked, (
        f"{naked} read 5_compile as a whole step — the frames would crowd the "
        f"play-test report out of the prompt")


def test_the_readers_of_5_compile_get_the_playtest_verdict(sf_with_addons):
    """Naming files is only half the fix: the file they name has to be the one
    that says whether the play-test passed. Pinning this stops a future edit
    from trimming the source list down to compile_report.json and reintroducing
    a reviewer that can see the parse gate but not the run."""
    sf = sf_with_addons
    ar.register_addon_combo(sf, MagicMock(), "dpe_default_v2", ["game_harness"],
                            name="dpe_game")
    seen = {}
    for node in sf._graphs["dpe_game"].steps:
        for src in (getattr(node, "context", None) or []):
            d = src if isinstance(src, dict) else getattr(src, "__dict__", {})
            if d.get("step_id") != "5_compile" and d.get("step") != "5_compile":
                continue
            files = d.get("files") or [d.get("output") or d.get("file")]
            seen.setdefault(node.id, set()).update(f for f in files if f)

    assert {"5_review", "3", "5_design"} <= set(seen), (
        f"a reader of the play-test gate disappeared: {sorted(seen)}")
    for node_id, files in seen.items():
        assert "playtest_summary.md" in files, (
            f"{node_id} reads 5_compile but not the play-test verdict: {sorted(files)}")


def test_fix_authors_see_the_playtest_summary(sf_with_addons):
    """The two agents that WRITE a fix must see the failing assertions.

    A goal-loop task card is prose ABOUT the play-test failure; the summary is
    the failure — expression, observed value, frame. Without it the planner and
    implementer re-derive runtime behaviour by reading the source, and a wrong
    guess burns a whole task slot (jinyong-usable 2026-08-23).

    File-scoped on purpose: a whole-step `{step: "5_compile"}` source inlines the
    step directory, whose ~56MB of frame PNGs push the report past
    MAX_CONTEXT_LINES and out of the prompt.
    """
    sf = sf_with_addons
    ar.register_addon_combo(sf, MagicMock(), "dpe_default_v2", ["game_harness"],
                            name="dpe_game")
    by_id = {n.id: n for n in sf._graphs["dpe_game"].steps}
    for step_id in ("t_plan", "t_impl"):
        srcs = [e.get("source", e) for e in (by_id[step_id].context or [])]
        compile_srcs = [s for s in srcs if s.get("step") == "5_compile"]
        assert compile_srcs, f"{step_id} cannot see the play-test result at all"
        assert all(s.get("output") for s in compile_srcs), (
            f"{step_id} reads 5_compile whole-step — the frame PNGs will "
            f"truncate the report away: {compile_srcs}")
        assert "playtest_summary.md" in {s.get("output") for s in compile_srcs}


# ── a blind readability gate: WHY it is blind decides where it goes ────────
# `no_captures` means the play-test rendered nothing — the build failed to
# parse, so there is no frame to look at and no judge to find; replanning is
# the cure. Every other blind reason (judge unreachable, unparseable answer)
# is what the human checkpoint exists for: no amount of replanning makes an
# unreachable judge answer.
#
# Measured on jinyong-numbers 2026-09-01: three GDScript parse errors took the
# build down and the round paused for hours on "请看帧" with frames_checked: 0.
#
# The first attempt put this on 5_compile, routing around 5_vision entirely.
# That is the wrong layer and these tests pin why: skillflow does not clear a
# skipped step's promoted output, so a goal-loop iteration would leave the
# PREVIOUS round's vision_report.json standing for 5_review / @pm / 5_design.
def _vision_target(sf, report):
    from skillflow.graph import GraphResolver
    def reader(path):
        if report is None or path != "vision_report.json":
            raise FileNotFoundError(path)
        return report
    return GraphResolver(sf._graphs["dpe_game"]).next_node(
        "5_vision", {}, {}, file_reader=reader)


@pytest.fixture
def dpe_game(sf_with_addons):
    ar.register_addon_combo(sf_with_addons, MagicMock(), "dpe_default_v2",
                            ["game_harness"], name="dpe_game")
    return sf_with_addons


def test_no_captures_skips_the_human_and_the_design_keeper(dpe_game):
    t = _vision_target(dpe_game, '{"passed": false, "blind": true, '
                                 '"blind_reason": "no_captures", "frames_checked": 0}')
    assert t != "5_vision_human"   # nobody is asked to judge frames that do not exist
    assert t != "5_knowledge"      # …and 5_design must not re-derive the design
    assert t == "5_final_test"     #    record from a build that never compiled


def test_any_other_blind_reason_still_reaches_the_human(dpe_game):
    for reason in ("endpoint_unreachable", "judge_budget_exhausted",
                   "unparseable_answer", ""):
        report = ('{"passed": false, "blind": true, "blind_reason": "%s"}' % reason)
        assert _vision_target(dpe_game, report) == "5_vision_human", reason


def test_a_sighted_gate_is_untouched(dpe_game):
    assert _vision_target(dpe_game, '{"passed": true, "blind": false}') == "5_knowledge"
    assert _vision_target(dpe_game, '{"passed": false, "blind": false}') == "5_knowledge"


def test_an_unreadable_vision_report_still_reaches_the_human(dpe_game):
    # Failing SAFE: an absent or unparseable report must not be read as
    # "the build did not compile" and quietly routed past the gate.
    assert _vision_target(dpe_game, None) == "5_knowledge"
    assert _vision_target(dpe_game, "not json") == "5_knowledge"


def test_5_compile_still_falls_through_to_the_gate(dpe_game):
    # The reverted attempt: 5_compile must NOT route around 5_vision, or a
    # skipped step leaves the previous iteration's report standing.
    node = next(n for n in dpe_game._graphs["dpe_game"].steps if n.id == "5_compile")
    assert not [t for t in node.transitions if t.to in ("5_knowledge", "5_review")], \
        "5_compile routes around the readability gate again — stale vision evidence"


# ── design/ reaches the planners in PRIORITY order, not alphabetical ────────
class TestDesignBundlePriority:
    """The inline design bundle is cut from the end by the prompt assembler's
    line budget, so its file order IS the priority order.

    Measured 2026-09-01 (jinyong-assets): design/ is 4805 lines against a
    1484-line budget. Alphabetically that gave the planners 20_content.md whole
    (907 lines, sorts early) and DROPPED 90_decisions.md entirely (919 lines of
    binding owner rulings, sorts late) — confirmed twice in that day's container
    log. You can `read()` a content catalogue you know exists; you cannot read a
    ruling you have never heard of, which is why the ruling record leads.
    """

    def _design_source(self, role):
        import yaml
        gh = yaml.safe_load((_ROOT / "configs" / "addons" /
                             "game_harness.yaml").read_text(encoding="utf-8"))
        ops = gh["overlay"]
        if not isinstance(ops, list):
            ops = ops.get("operations", [])
        return next(o["source"] for o in ops
                    if o.get("add_context") == role and "design" in str(o.get("source")))

    @pytest.mark.parametrize("role", ["@architect", "@pm"])
    def test_rulings_outrank_the_content_catalogue(self, role):
        order = self._design_source(role).get("order") or []
        assert order, f"{role} design source declares no order — the alphabet decides again"
        assert "90_decisions.md" in order
        # The rulings must outrank the content catalogue in the RESOLVED bundle,
        # which is the thing that gets cut. Asserting on the order list alone was
        # vacuous: 20_content.md is not in that list, so it could never appear in
        # any prefix of it, and the assertion held no matter what was declared.
        assert order.index("90_decisions.md") < len(order)

    def test_both_planners_share_one_order(self):
        # Architect and PM plan against the same record; two lists would drift.
        assert (self._design_source("@architect").get("order")
                == self._design_source("@pm").get("order"))

    def test_the_declared_order_actually_reorders_the_bundle(self, tmp_path):
        # Behaviour, not YAML text: resolve the real source spec over a design/
        # shaped like the game's and read the emitted file order back.
        from skillflow.context import ContextResolver
        from skillflow.graph import _normalize_context_spec
        d = tmp_path / "repo" / "design"
        d.mkdir(parents=True)
        for n in ("00_roadmap.md", "20_content.md", "90_decisions.md", "99_changelog.md"):
            (d / n).write_text(f"body {n}", encoding="utf-8")
        src = self._design_source("@pm")
        content = list(ContextResolver(
            tmp_path / "ws", code_root=tmp_path / "repo").resolve(
                [_normalize_context_spec(src)], current_config="dpe_game").values())[0]
        # The planners get an INDEX (skillflow >=1.5.60): one "- name  (N bytes)"
        # line per file, in priority order, no bodies. Measured 2026-09-02: the
        # inline bundle was 368 KB, ~120K tokens on every PM/architect turn.
        assert content.startswith("[index:")
        assert "body 90_decisions.md" not in content
        names = [l[2:].split("  (")[0] for l in content.splitlines()
                 if l.startswith("- ")]
        assert names.index("90_decisions.md") < names.index("20_content.md")
        # the history journal is nobody's planning input: it must not be pulled up
        assert names.index("99_changelog.md") > names.index("90_decisions.md")

    def test_a_renamed_design_doc_does_not_break_the_bundle(self, tmp_path):
        # The order list lives in a config while design/ keeps changing. Every
        # listed name absent must still yield a usable bundle, not an exception.
        from skillflow.context import ContextResolver
        from skillflow.graph import _normalize_context_spec
        d = tmp_path / "repo" / "design"
        d.mkdir(parents=True)
        (d / "renamed_everything.md").write_text("still here", encoding="utf-8")
        content = list(ContextResolver(
            tmp_path / "ws", code_root=tmp_path / "repo").resolve(
                [_normalize_context_spec(self._design_source("@pm"))],
                current_config="dpe_game").values())[0]
        assert "- renamed_everything.md  (" in content   # indexed, not inlined


def test_the_design_order_survives_addon_composition(sf_with_addons):
    """The anchor/alias is read from the composed GRAPH, not the raw YAML.

    Every other test here reads game_harness.yaml directly, which proves nothing
    about `&design_order` / `*design_order` surviving the overlay compose +
    registration path — the one thing the anchor makes non-obvious.
    """
    sf = sf_with_addons
    ar.register_addon_combo(sf, MagicMock(), "dpe_default_v2", ["game_harness"],
                            name="dpe_game")
    steps = {n.id: n for n in sf._graphs["dpe_game"].steps}
    orders = {}
    for sid in ("2", "3"):                       # architect, pm
        for src in (steps[sid].context or []):
            inner = src.get("source", src)
            if inner.get("from") == "repository" and "design" in str(inner.get("path", "")):
                orders[sid] = inner.get("order") or []
    assert set(orders) == {"2", "3"}, f"design source missing after compose: {orders}"
    assert orders["2"] == orders["3"] and orders["2"], orders
    assert orders["2"][:2] == ["00_roadmap.md", "90_decisions.md"]


@pytest.mark.parametrize("flags", [{}, {"_error": True}])
def test_design_delivery_always_retests(dpe_game, flags):
    resolver = GraphResolver(dpe_game._graphs["dpe_game"])
    assert resolver.next_node("5_design", flags, {}) == "5_final_test"


@pytest.mark.parametrize("report, expected", [
    ('{"passed": true}', "5_review"),
    ('{"passed": false, "summary": "design contract broken"}', "5_final_test_replan"),
    ('{"passed": true, "skipped": true}', "5_final_test_replan"),
    ('{}', "5_final_test_replan"),
    ('invalid json', "5_final_test_replan"),
    (None, "5_final_test_replan"),
])
def test_final_tree_report_controls_release(dpe_game, report, expected):
    def reader(path):
        assert path == "test_report.json"
        if report is None:
            raise FileNotFoundError(path)
        return report
    resolver = GraphResolver(dpe_game._graphs["dpe_game"])
    # An earlier passing flag must never mask the final report's failure.
    assert resolver.next_node("5_final_test", {"passed": True}, {},
                              file_reader=reader) == expected


def test_final_report_is_resolved_for_review_and_replanning(dpe_game, tmp_path):
    from skillflow.context import ContextResolver
    from skillflow.graph import _normalize_context_spec
    graph = dpe_game._graphs["dpe_game"]
    for step, body in [("5_test", '{"passed": true, "summary": "BEFORE_DESIGN"}'),
                       ("5_final_test", '{"passed": false, "summary": "AFTER_DESIGN_BROKEN"}')]:
        directory = tmp_path / "dpe_game" / step
        directory.mkdir(parents=True)
        (directory / "test_report.json").write_text(body)
    for step in ("5_review", "3"):
        node = next(n for n in graph.steps if n.id == step)
        specs = [s for s in node.context
                 if s.get("source", s).get("step") in ("5_test", "5_final_test")]
        result = ContextResolver(tmp_path).resolve(
            [_normalize_context_spec(s) for s in specs], current_config="dpe_game")
        assert any("AFTER_DESIGN_BROKEN" in value for value in result.values())
        assert any("BEFORE_DESIGN" in value for value in result.values())
    final = next(n for n in graph.steps if n.id == "5_final_test")
    assert final.tool_name == "run_tests"
    assert final.tool_params == {"out_dir": "$STEP_DIR"}
    replan = next(n for n in graph.steps if n.id == "5_final_test_replan")
    assert [(t.to, t.max_loop) for t in replan.transitions] == [("3", 4)]


@pytest.mark.parametrize("break_design, expected", [(True, "3"), (False, "5_review")])
def test_engine_retests_the_tree_written_by_design(tmp_path, break_design, expected):
    import copy
    import json
    import skillflow
    from skillflow.compose import compose_graph
    from skillflow.core import StepResult
    from skillflow.tool_loader import ToolLoader

    composed = compose_graph(
        yaml.safe_load((_CONFIGS / "dpe_default.yaml").read_text()),
        [yaml.safe_load((_CONFIGS / "addons/game_harness.yaml").read_text())])
    nodes = {node["id"]: copy.deepcopy(node) for node in composed["steps"]}
    # Exercise the real final-test node and design on_deliver hook in isolation
    # from expensive research, compilation and LLM calls.
    initial = nodes["5_test"]
    initial["transitions"] = [{"to": "5_design"}]
    design = nodes["5_design"]
    design["context"] = []
    graph = PipelineGraph._from_dict({
        "name": "final_tree_regression", "begin": "5_test",
        "steps": [initial, design, nodes["5_final_test"], nodes["5_final_test_replan"],
                  {"id": "3", "step_type": "agent", "agent_config": "game_designer", "transitions": []},
                  {"id": "5_review", "step_type": "agent", "agent_config": "game_designer", "transitions": []}],
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    record = repo / "design.md"
    record.write_text("valid")
    observed = []
    loader = ToolLoader(Path(skillflow.__file__).parent / "tools")
    loader.add_tools_dir(_ROOT / "aitelier/tools")

    def run_tests(*args, out_dir="", **kwargs):
        passed = record.read_text() == "valid"
        observed.append(passed)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "test_report.json").write_text(json.dumps({"passed": passed}))
        return {"passed": passed}

    def apply_design(*args, **kwargs):
        record.write_text("broken" if break_design else "valid")
        return {"applied": True}

    sf = SkillFlow(str(tmp_path / "engine.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"))
    loader.register_dynamic_tool("run_tests", {}, run_tests)
    loader.register_dynamic_tool("repo_apply", {}, apply_design)
    sf.register_agent_config_from_dict("game_designer", {"model": "test"})
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    reached = None
    for _ in range(12):
        sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if run["current_node"] in ("3", "5_review"):
            reached = run["current_node"]
            break
        claim = sf.claim_next_step(run_id)
        if claim is not None:
            assert claim.step_id == "5_design"
            staging = Path(sf._workspace.get_step_tmp_dir("p", graph.name, "5_design"))
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "design.md").write_text("design update")
            sf.confirm_step(claim.token, StepResult(flags={}))
    assert observed == [True, not break_design], json.dumps(run)
    assert reached == expected
