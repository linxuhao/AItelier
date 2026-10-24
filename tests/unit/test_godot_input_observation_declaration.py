"""Split-file declarations must survive the real reader and native execution.

Reuse the existing CPU-only native fixture and retained raw reports. These are
not transport mocks; AITELIER_REQUIRE_NATIVE=1 makes a missing engine fail.
"""

import copy
import json

import pytest

from aitelier.tools.godot_playtest.impl import read_spec, select_scenarios
from tests.unit.test_godot_input_observation import (  # noqa: F401 -- native fixture
    native, _assert_dispatch, _assert_invalid, _assert_unobserved, _decode,
    _observation, _scenario,
)


@pytest.mark.parametrize("declaration", ["valid", "missing_node", "missing_property", "omitted"])
def test_real_split_file_observation_keeps_both_declaration_fields(native, tmp_path, declaration):
    project, run = native
    # Both declaration fields differ from the original fixture. A reader that
    # drops the mapping or substitutes its old Player/input_acceptance values
    # cannot produce the valid witness below.
    scene = project / "all.tscn"
    text = scene.read_text(encoding="utf-8")
    assert text.count('[node name="Player"') == 1
    scene.write_text(text.replace('[node name="Player"', '[node name="DeclaredPlayer"'), encoding="utf-8")
    script = project / "player.gd"
    text = script.read_text(encoding="utf-8")
    assert text.count("input_acceptance") == 2
    script.write_text(text.replace("input_acceptance", "split_acceptance"), encoding="utf-8")

    common = {"scene": "res://partial.tscn", "frames": 60, "actions": ["move_up"],
              "surface": {"DeclaredPlayer": ["grid_y", "position", "split_acceptance"]},
              "scenario_order": ["split_declaration"]}
    scenario = {
        "name": "split_declaration", "scene": "res://all.tscn",
        "description": "Read both nondefault declaration fields in native Godot",
        "repeatability": True,
        "execution_budget": {"max_frames": 89, "timeout_seconds": 120},
        "input_observation": {"node": "DeclaredPlayer", "property": "split_acceptance"},
        "timeline": [
            {"at": 20, "press": "move_up"},
            {"at": 30, "press": "move_up"},
            {"at": 40, "press": "move_up"},
            {"at": 59, "assert": {"DeclaredPlayer.grid_y": -3, "DeclaredPlayer.position.y": -3}},
            {"at": 59, "assert": [
                {"name": "raw received", "node": "DeclaredPlayer", "expr": "true", "attr": "raw_received"}]},
        ],
    }
    if declaration == "missing_node":
        scenario["input_observation"]["node"] = "NonexistentDeclaredPlayer"
    elif declaration == "missing_property":
        scenario["input_observation"]["property"] = "nonexistent_split_acceptance"
    elif declaration == "omitted":
        scenario.pop("input_observation")
    directory = project / "playtest"
    directory.mkdir()
    # JSON is valid YAML; these are actual files consumed by the strict loader.
    (directory / "_common.yaml").write_text(json.dumps(common), encoding="utf-8")
    (directory / "split_declaration.yaml").write_text(json.dumps(scenario), encoding="utf-8")
    loaded, info = read_spec(project)
    expected = {k: copy.deepcopy(v) for k, v in common.items() if k != "scenario_order"}
    expected["scenarios"] = [copy.deepcopy(scenario)]
    assert info["source"] == "playtest/" and info["errors"] == []
    assert loaded == expected  # Preserve the complete scenario, not an allowlist projection.
    selected, unknown = select_scenarios(loaded, ["split_declaration"])
    assert unknown == [] and selected == expected
    (tmp_path / "split-loaded-spec.json").write_text(json.dumps(selected, indent=2), encoding="utf-8")

    report, probe = run(selected)
    row, observation = _scenario(report), _observation(report)
    assert probe["frames"] == 89 and probe["complete"] is True
    assert report["timing"]["scenarios"][0]["frames_stepped"] == 89
    assert row["name"] == "split_declaration" and row["ran"] is True
    assert row["expected_asserts"] == len(row["asserts"]) == 3
    assert all(a["passed"] is True and a["measurement"] == "ok" for a in row["asserts"])
    assert row["pressed"] is True and row["input_dead"] is False
    assert row["comparison_error"] is None
    player = report["state"]["/root/DeclaredPlayer"]
    assert player["vars"]["grid_y"] == player["pos"][1] == -3
    assert "split_acceptance" not in player["vars"]
    _assert_dispatch(report, 3, 6, 0 if declaration == "omitted" else 3)
    if declaration == "omitted":
        _assert_unobserved(observation)
        assert observation["run_id"] is observation["delivery_complete"] is None
        assert observation["complete"] is False
        assert "input_observation" not in probe
        bookkeeping = probe["nodes"]["/root/_AItelierProbe"]["vars"]
        assert bookkeeping["_input_enabled"] is False
        assert bookkeeping["_input_injected"] == bookkeeping["_input_events_injected"] == 0
        # Six actual callbacks above coexist with inactive zero bookkeeping.
        assert report["passed"] is True and report["spec_errors"] == []
    elif declaration == "valid":
        assert observation["status"] == "all_accepted"
        assert (observation["accepted"], observation["rejected"], observation["unknown"]) == (3, 0, 0)
        assert observation["delivery_complete"] is observation["complete"] is observation["acceptance_pass"] is True
        assert observation["errors"] == [] and report["spec_errors"] == []
        assert report["passed"] is True
        receipts = _decode(observation["receipts"])
        assert len(receipts) == 6 and [r[1] for r in receipts] == [20, 22, 30, 32, 40, 42]
        decisions = _decode(observation["witness"])["decisions"]
        assert [d["seq"] for d in decisions] == [1, 3, 5]
        assert all(d["run_id"] == observation["run_id"] and d["decision"] == "accepted" for d in decisions)
    else:
        _assert_invalid(report)
        assert observation["witness"] is None and observation["delivery_complete"] is True
        assert len(_decode(observation["receipts"])) == 6
        assert any("witness" in error.lower() for error in observation["errors"])
        assert probe["input_observation"]["witness"] is None
