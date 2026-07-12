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


# ── generate_addon tool schema + handler ─────────────────────────────

def test_generate_addon_in_tool_definitions():
    from core.meta_agent import TOOL_DEFINITIONS, _TOOL_HANDLERS, MetaAgent
    names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
    assert "generate_addon" in names
    td = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "generate_addon")
    props = td["function"]["parameters"]["properties"]
    assert "description" in props and "base" in props
    assert _TOOL_HANDLERS["generate_addon"] is MetaAgent._tool_generate_addon


def test_system_prompt_routes_generate_addon():
    from core.meta_agent import SYSTEM_PROMPT
    assert "generate_addon" in SYSTEM_PROMPT
    # routing distinguishes the three paths
    assert "generate_pipeline" in SYSTEM_PROMPT and "start_new_project" in SYSTEM_PROMPT


async def test_generate_addon_unknown_base(tmp_path):
    """Handler rejects a base graph that isn't registered."""
    from core.meta_agent import MetaAgent
    db, ws = MagicMock(), MagicMock()
    agent = MetaAgent(db, ws, owner_email="t@local")
    sf = SkillFlow(":memory:")
    with patch("api.dependencies.get_skillflow", return_value=sf):
        result = await agent._tool_generate_addon(
            {"description": "add a gate", "base": "nope_base"})
    assert "error" in result and "Unknown base" in result["error"]


async def test_generate_addon_base_without_anchors(tmp_path):
    """A base with no anchors has nothing for an overlay to target."""
    from core.meta_agent import MetaAgent
    db, ws = MagicMock(), MagicMock()
    agent = MetaAgent(db, ws, owner_email="t@local")
    sf = SkillFlow(":memory:")
    sf.register_agent_config_from_dict("r", {"model": "host", "tools": []})
    no_anchor = yaml.safe_load(_BASE_YAML)
    no_anchor.pop("anchors")
    no_anchor["name"] = "no_anchor_base"
    sf.register_graph(PipelineGraph._from_dict(no_anchor))
    with patch("api.dependencies.get_skillflow", return_value=sf):
        result = await agent._tool_generate_addon(
            {"description": "add a gate", "base": "no_anchor_base"})
    assert "error" in result and "anchors" in result["error"]


async def test_generate_addon_seeds_and_launches(tmp_path):
    """Happy path: handler seeds base_spec + base_graph and launches addon_converter."""
    from core.meta_agent import MetaAgent
    db, ws = MagicMock(), MagicMock()
    db.link_run_to_session = MagicMock()
    agent = MetaAgent(db, ws, owner_email="t@local")
    sf = SkillFlow(":memory:")
    sf.register_agent_config_from_dict("r", {"model": "host", "tools": []})
    sf.register_graph(PipelineGraph._from_dict(yaml.safe_load(_BASE_YAML)))

    captured = {}

    def fake_launch(db_, ws_, config_name, pid, **kwargs):
        captured["config_name"] = config_name
        captured["seed_inputs"] = kwargs.get("seed_inputs")
        captured["seed_text"] = kwargs.get("seed_text")
        return {"status": "started", "run_id": "run-42"}

    async def fake_wait(run_id):
        return {"status": "checkpoint", "run_id": run_id, "label": "Overlay Review"}

    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("core.run_launcher.start_config_run", side_effect=fake_launch), \
         patch.object(agent, "_run_pipeline_until_checkpoint", side_effect=fake_wait), \
         patch("core.scheduler._sync_project_status_to_db"):
        result = await agent._tool_generate_addon(
            {"description": "add a compile gate after tests", "base": "mini_base"})

    assert result["status"] == "checkpoint"
    assert result["pipeline"] == "addon_converter"
    assert captured["config_name"] == "addon_converter"
    assert captured["seed_text"].startswith("add a compile gate")
    # base_spec seed carries the real anchors + steps
    spec = yaml.safe_load(captured["seed_inputs"]["base_spec.json"])
    assert spec["anchors"] == {"mid": "b"}
    assert spec["steps"] == ["a", "b", "c"]
    assert spec["anchor_targets"]["mid"] == ["c"]
    # base_graph seed is the full graph dict (anchors intact) for compose_validate
    bg = yaml.safe_load(captured["seed_inputs"]["base_graph.yaml"])
    assert bg["name"] == "mini_base" and bg["anchors"] == {"mid": "b"}
