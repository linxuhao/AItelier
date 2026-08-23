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
