"""Tests for the addon_converter HOST bridge + generate_addon butler tool.

skillflow owns the addon_converter graph + compose_validate (tested in
skillflow's test_addon_converter.py). Here we test AItelier's half:
  * register_addon_from_run: persist + register the overlay a completed run made,
    and compose its blessed alias combo.
  * load_generated_addons: boot re-scan of the persisted overlays.
  * the generate_addon tool schema + handler wiring (seeds base_spec/base_graph,
    launches addon_converter) and butler routing.
"""

import yaml
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from skillflow import SkillFlow, PipelineGraph
from core import addon_registry as ar

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGS = _ROOT / "configs"

# A tiny base with an anchor + a good overlay against it (self-contained).
_BASE_YAML = """
name: mini_base
begin: a
anchors: { mid: b }
end_conditions:
  combinator: or
  conditions: [{ type: node_reached, node: c, result: completed }]
steps:
  - { id: a, step_type: agent, agent_config: r, transitions: [{ to: b }] }
  - { id: b, step_type: agent, agent_config: r, transitions: [{ to: c }] }
  - { id: c, step_type: agent, agent_config: r }
"""

_OVERLAY = {
    "name": "mini_addon",
    "base": "mini_base",
    "alias": "mini_plus",
    "description": "adds a gate after the mid anchor",
    "whenToUse": "testing",
    "overlay": [
        {"insert_after": "@mid",
         "steps": [{"id": "mid_gate", "step_type": "tool", "tool_name": "some_tool",
                    "tool_params": {"out_dir": "$STEP_DIR"}}]},
    ],
}


@pytest.fixture
def sf_base(tmp_path, monkeypatch):
    """SkillFlow with the mini base registered + an isolated generated-addons dir."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    sf = SkillFlow(":memory:")
    sf.register_agent_config_from_dict("r", {"model": "host", "tools": []})
    sf.register_graph(PipelineGraph._from_dict(yaml.safe_load(_BASE_YAML)))
    return sf


# ── register_addon_from_run ──────────────────────────────────────────

def test_register_addon_from_run_persists_and_registers(sf_base, tmp_path):
    sf = sf_base
    reg = MagicMock()
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_OVERLAY), encoding="utf-8")

    with patch("skillflow.plugins.skill_converter.get_addon_output_file",
               return_value=overlay_file):
        result = ar.register_addon_from_run(sf, reg, "run-1")

    assert result["addon_name"] == "mini_addon"
    assert result["base"] == "mini_base"
    assert result["action"] == "created"
    # overlay declared to skillflow
    assert "mini_addon" in {o["name"] for o in sf.list_overlays()}
    # blessed alias combo composed + registered
    assert result["registered_config"] == "mini_plus"
    assert "mini_plus" in sf._graphs
    # persisted to the gitignored generated-addons dir
    assert Path(result["path"]).exists()
    assert Path(result["path"]).parent == ar.generated_addons_dir()


def test_register_addon_from_run_update_in_place(sf_base, tmp_path):
    sf = sf_base
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_OVERLAY), encoding="utf-8")
    with patch("skillflow.plugins.skill_converter.get_addon_output_file",
               return_value=overlay_file):
        first = ar.register_addon_from_run(sf, MagicMock(), "run-1")
        second = ar.register_addon_from_run(sf, MagicMock(), "run-2")
    assert first["action"] == "created"
    assert second["action"] == "updated"


def test_register_addon_from_run_missing_output(sf_base):
    with patch("skillflow.plugins.skill_converter.get_addon_output_file",
               return_value=None):
        result = ar.register_addon_from_run(sf_base, MagicMock(), "run-x")
    assert "error" in result


def test_load_generated_addons_rescans(sf_base, tmp_path):
    sf = sf_base
    # Persist an overlay directly, then boot-scan.
    (ar.generated_addons_dir() / "mini_addon.yaml").write_text(
        yaml.safe_dump(_OVERLAY), encoding="utf-8")
    names = ar.load_generated_addons(sf, MagicMock())
    assert "mini_addon" in names
    assert "mini_addon" in {o["name"] for o in sf.list_overlays()}
    assert "mini_plus" in sf._graphs   # alias combo re-composed


class TestOverlayDependencyWarnings:
    """compose_validate proves the overlay composes; this host check surfaces
    runtime deps that don't exist — an invented tool or a missing fragment."""

    def _sf(self, tools):
        sf = MagicMock()
        sf._tool_loader.list_tools.return_value = list(tools)
        return sf

    def test_warns_on_unknown_tool(self):
        sf = self._sf(["run_tests", "lint"])
        spec = {"overlay": [
            {"insert_after": "@post_verify_tests",
             "steps": [{"id": "x", "step_type": "tool", "tool_name": "made_up_tool"}]}]}
        w = ar._overlay_dependency_warnings(sf, spec)
        assert any("made_up_tool" in m and "run time" in m for m in w)

    def test_no_warn_for_known_tool(self):
        sf = self._sf(["run_tests", "lint"])
        spec = {"overlay": [
            {"insert_after": "@post_verify_tests",
             "steps": [{"id": "x", "step_type": "tool", "tool_name": "lint"}]}]}
        assert ar._overlay_dependency_warnings(sf, spec) == []

    def test_warns_on_missing_fragment(self):
        sf = self._sf(["lint"])
        spec = {"overlay": [
            {"add_template": "@architect", "fragment": "nope_addon/ghost.md"}]}
        w = ar._overlay_dependency_warnings(sf, spec)
        assert any("ghost.md" in m and "skipped" in m for m in w)

    def test_no_warn_for_existing_fragment(self):
        sf = self._sf(["lint"])
        spec = {"overlay": [
            {"add_template": "@architect", "fragment": "game_harness/architect.md"}]}
        assert ar._overlay_dependency_warnings(sf, spec) == []
