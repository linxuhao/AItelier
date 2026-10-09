"""CPU admission/driver proofs; embedded Godot execution is deliberately UNRUN."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"
spec = importlib.util.spec_from_file_location("before_harness", HARNESS)
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)


def timeline():
    return [
        {"at": 10, "capture_before": [{"id": "damage", "node": "Actor",
            "attr": "health", "action_frame": 20}]},
        {"at": 20, "actions": ["hit"]},
        {"at": 30, "assert": [{"node": "Actor", "attr": "health",
            "mode": "changed", "before": "damage", "name": "health fell"}]},
    ]


def test_protocol_keeps_capture_operand_input_and_delta_mode():
    raw = timeline()
    normalized, errors = gh._normalize_timeline(raw)
    assert errors == []
    assert normalized[0] == raw[0]
    assert normalized[1] == {"at": 20, "press": "hit"}
    assert normalized[2]["assert"] == raw[2]["assert"]
    # Input-free control can be attached to an existing assertion point.
    raw[0]["assert"] = {"Actor.health": "health == 30"}
    assert gh._normalize_timeline(raw)[1] == []


@pytest.mark.parametrize("change", [
    lambda t: t.pop(0),
    lambda t: t.insert(1, copy.deepcopy(t[0])),
    lambda t: t[0]["capture_before"][0].update(action_frame=10),
    lambda t: t[0]["capture_before"][0].update(action_frame=30),
    lambda t: t[0]["capture_before"][0].update(action_frame=21),
    lambda t: t[0]["capture_before"][0].update(action_frame=True),
    lambda t: t[0]["capture_before"][0].update(value=30),
    lambda t: t[0]["capture_before"][0].update(scenario="other"),
    lambda t: t[0]["capture_before"][0].update(node="Other"),
    lambda t: t[0]["capture_before"][0].update(attr="armor"),
    lambda t: t[0]["capture_before"][0].update(id=""),
    lambda t: t[0].update(actions=["hit"]),
    lambda t: t[0].update(capture_before={"id": "damage"}),
    lambda t: t[2]["assert"][0].update(before="unknown"),
    lambda t: t[2]["assert"][0].update(before=1),
    lambda t: t[2]["assert"][0].update(mode="presence"),
    lambda t: t[2]["assert"][0].update(expr="health > 0"),
    lambda t: t[1].update(actions=[]),
])
def test_admission_refuses_ambiguous_missing_late_wrong_scope_or_constant_controls(change):
    raw = timeline()
    change(raw)
    assert gh._normalize_timeline(raw)[1]


def test_each_scenario_has_its_own_id_namespace():
    assert gh._normalize_timeline(timeline())[1] == []
    other = timeline()[1:]
    assert any("unknown before capture" in e for e in gh._normalize_timeline(other)[1])


def test_recreated_logical_actor_is_not_an_instance_id_in_protocol():
    raw = timeline()
    raw.insert(2, {"at": 25, "actions": ["replace_actor"]})
    assert gh._normalize_timeline(raw)[1] == []
    assert set(raw[0]["capture_before"][0]) == {"id", "node", "attr", "action_frame"}


def test_capture_uses_existing_safe_read_and_owns_structured_value():
    src = gh._PROBE_GD
    capture = src.split("func _capture_before", 1)[1].split("func _capture_baselines", 1)[0]
    assert '_read_before_attr(target, record["attr"])' in capture
    assert 'if read["ok"]:' in capture
    assert 'value.duplicate(true)' in capture
    assert capture.index('if _before_captures.has(id)') < capture.index('_read_before_attr(')
    assert '_spec_errors.append' in capture
    assert '_jsonable' not in capture and 'str(value)' not in capture
    delta = src.split("func _eval_delta", 1)[1].split("func _truthy", 1)[0]
    for bound in ('captured.get("scope"', 'captured.get("node"', 'captured.get("attr"',
                  'captured.get("action_frame"', '_before_actions.has'):
        assert bound in delta
    assert 'captured["value"] if a.has("before") else _baselines.get(key, null)' in delta
    assert '_observe_equal(cur, base)' in delta
    assert '(not same) if mode == "changed" else same' in delta
    assert 'elif not _baselines.has(key):' in delta


@pytest.mark.parametrize("attr", [
    "health", "input_stats.hp", 'profile.cultivation["month"]',
    "items[0]", "items[-1].health", "items[2]['hp']",
    'stats["health * 0"]',
])
def test_before_property_access_paths_are_admitted(attr):
    raw = timeline()
    raw[0]["capture_before"][0]["attr"] = attr
    raw[2]["assert"][0]["attr"] = attr
    assert gh._normalize_timeline(raw)[1] == []


@pytest.mark.parametrize("attr", [
    "42", "-42", "0.0", "true", "false", "null", "self", "PI", "TAU", "INF", "NAN",
    "health * 0", "health - health", "health == health", "health + 1",
    "not health", "health and health", "health if health else health", "health ? 1 : 0",
    "min(health, health)", "get('health')", "health.size()", "stats.get('hp')",
    "[health]", "{'hp': health}", "(health)", "self.health", "get_node('Actor').health",
    "items[index]", "items[health - health]", "items[0 + 0]", "stats[true]",
    "items[1.0]", "items[00]", "health\n", "health;health", "health # comment",
    "items[0] + health", "items[-1].health == health", '"health"',
    "health / health", "health % 1", "health ** 0", "health | 0",
    "health & health", "health ^ health", "~health", "health << 0",
    "health or true", "health is int", "health in [health]",
    "health if true else 0", "items[abs(health)]", "health.to_string()",
])
def test_before_rejects_computations_literals_calls_and_dynamic_index_masking(attr):
    raw = timeline()
    raw[0]["capture_before"][0]["attr"] = attr
    raw[2]["assert"][0]["attr"] = attr
    assert gh._normalize_timeline(raw)[1]


def test_explicit_reads_require_same_shared_grammar_and_real_node_property():
    src = gh._PROBE_GD
    read = src.split("func _read_before_attr", 1)[1].split("func _capture_baselines", 1)[0]
    assert '__BEFORE_PROPERTY_PATH_RE__' not in src
    assert 'path.compile(' in read and 'path.search(attr) == null' in read
    assert 'target.get_property_list()' in read
    assert read.index('target.get_property_list()') < read.index('return _read_attr(target, attr)')
    assert 'str(property.get("name", "")) == root' in read
    delta = src.split("func _eval_delta", 1)[1].split("func _truthy", 1)[0]
    assert '_read_before_attr(target, attr) if a.has("before") else _read_attr(target, attr)' in delta


def test_legacy_expression_operands_keep_their_existing_admission():
    raw = timeline()[1:]
    raw[1]["assert"][0].pop("before")
    raw[1]["assert"][0]["attr"] = "health * 0"
    assert gh._normalize_timeline(raw)[1] == []


def test_admitted_controls_bind_existing_live_input_fixture_operands():
    fixture = HARNESS.parents[2] / "tests/unit/fixtures/godot_assertion_value_controls"
    controls = json.loads((fixture / "before-controls.json").read_text())
    live = (fixture / "main.gd").read_text()
    for scenario in controls["scenarios"]:
        assert gh._normalize_timeline(scenario["timeline"])[1] == []
        operands = [c["attr"] for c in scenario["timeline"][0]["capture_before"]]
        assert operands == ["input_health", "input_stats"]
    # These values are changed through the real input handler, not a constructor
    # or test-only echo of a claimed baseline. Native effects remain UNRUN here.
    handler = live.split("func _input", 1)[1].split('var label', 1)[0]
    assert 'event.is_action_pressed("before_damage")' in handler
    assert 'input_health -= 2' in handler and 'input_stats["hp"] -= 2' in handler
    assert 'damage_inputs += 1' in handler


def test_host_driver_preserves_noop_changed_as_false(monkeypatch, tmp_path):
    value = {"id": "damage", "scope": "s", "node": "Actor", "attr": "health",
             "frame": 10, "action_frame": 20, "ok": True, "value": 30}
    report_row = {"name": "health fell", "node": "Actor", "frame": 30,
        "passed": False, "measurement": "ok", "actual": {"baseline": 30, "current": 30},
        "before": value, "expr": "health changed since before damage"}
    def fake_probe(*args, **kwargs):
        return {"frames": 180, "complete": True, "asserts": [report_row],
                "before_captures": {"damage": value}}, [], False
    monkeypatch.setattr(gh, "_run_probe", fake_probe)
    report = gh._playtest_spec(tmp_path, {"scenarios": [{"name": "s", "timeline": timeline()}]}, 180, 10)
    result = report["behavior"]["scenarios"][0]
    assert result["complete"] is True
    assert result["passed"] is False
    assert result["asserts"][0]["actual"] == {"baseline": 30, "current": 30}


def test_real_host_driver_retains_captures_and_hard_fails_failed_reads(monkeypatch, tmp_path):
    # Exercise the actual host report/completeness path. The embedded GDScript
    # cannot execute in CPU-only admission tests; this is a report transport test.
    receipt = {"damage": {"scope": "s", "frame": 10, "action_frame": 20,
        "node": "Actor", "attr": "health", "ok": False, "error": "unsupported Object"}}
    result = {"frames": 180, "complete": True, "before_captures": receipt,
        "spec_errors": ["before capture damage failed: unsupported Object"],
        "asserts": [{"name": "health fell", "node": "Actor", "expr": "health changed",
            "frame": 30, "passed": False, "measurement": "incomplete",
            "actual": {"baseline": None, "current": 2, "baseline_missing": True}}]}
    def fake_probe(*args, **kwargs):
        return result, [], False
    monkeypatch.setattr(gh, "_run_probe", fake_probe)
    # Existing driver fixture helper and call signature are used by its suite.
    report = gh._playtest_spec(tmp_path, {"scenarios": [{"name": "s", "timeline": timeline()}]}, 180, 10)
    assert report["behavior"]["scenarios"][0]["before_captures"] == receipt
    assert report["spec_errors"]
    assert report["behavior"]["scenarios"][0]["complete"] is False
