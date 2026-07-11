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
