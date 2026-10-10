"""L0 diagnostics use the same verdict classes as driven passes.

The fixture is the complete stderr of both retained coop controls in normal
run gq-20261010T070944Z-4e706e90, passes 329/330; SHA256
5515d920023ebbe9253fd63621c5fbf7a7972131eb8061c86e63472ed70adc4a.
These CPU tests replace only the engine subprocess: actual probe parsing,
observation validation, control comparison and report aggregation all run.
Synthetic snapshots do not establish native gameplay or timing acceptance.
"""
import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"
_spec = importlib.util.spec_from_file_location("control_parity_harness", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)
_STDERR = (Path(__file__).parent / "fixtures/godot_native_stderr/coop_no_input_exit.txt").read_text()


def _report(monkeypatch, tmp_path, *, frames=180, scene="res://scenes/coop/coop_scenario_fixture_en.tscn",
            control="complete", same_state=False, scenarios=1):
    calls = []

    def engine_output(args, timeout, extra_env, render):
        spec = json.loads(Path(extra_env["AITELIER_PROBE_SPEC"]).read_text())
        driven = bool(spec["timeline"])
        calls.append({"driven": driven, "frames": spec["frames"], "scene": args[-1],
                      "timeout": timeout, "render": render})
        snapshot = {"frames": frames, "complete": True,
                    "nodes": {"CoopPlayerView": {"last_command": "practice" if driven and not same_state else ""}},
                    "asserts": [{"name": "CoopPlayerView.last_command", "frame": 120,
                                 "passed": True, "measurement": "ok"}] if driven else [],
                    "timing": {"frames_stepped": frames, "game_usec": int(frames * 1_000_000 / 60)}}
        stderr = _STDERR
        if not driven:
            if control == "runtime":
                stderr += "SCRIPT ERROR: Cannot call method 'refresh' on a null value.\n          at: _process (res://fixture.gd:10)\n"
            elif control == "parse":
                stderr += 'SCRIPT ERROR: Parse Error: Identifier "foo" not declared.\n          at: GDScript::reload (res://fixture.gd:10)\n'
            elif control == "native":
                stderr += "ERROR: Error calling deferred method: 'Node::refresh': Cannot convert argument 1 from Array to Array.\n   at: _call_function (core/object/message_queue.cpp:222)\n"
            elif control == "timeout":
                raise subprocess.TimeoutExpired("godot", timeout, output=b"", stderr=stderr.encode())
            elif control == "missing":
                return subprocess.CompletedProcess("godot", 0, "", stderr)
            elif control == "malformed":
                Path(extra_env["AITELIER_PROBE_OUT"]).write_text("{bad json")
                return subprocess.CompletedProcess("godot", 0, "", stderr)
            elif control == "short":
                snapshot["frames"] -= 1
            elif control == "false-complete":
                snapshot["complete"] = False
            elif control == "bad-nodes":
                snapshot["nodes"] = []
            elif control == "spec-error":
                snapshot["spec_errors"] = ["assertion expression is invalid"]
        Path(extra_env["AITELIER_PROBE_OUT"]).write_text(json.dumps(snapshot))
        return subprocess.CompletedProcess("godot", 0, "AITELIER_PROBE_WROTE\n", stderr)

    monkeypatch.setattr(gh, "_run", engine_output)
    timeline = [{"at": 105, "clicks": ["CoopPracticeButton"]},
                {"at": 120, "assert": {"CoopPlayerView.last_command": 'last_command == "practice"'}}]
    spec = {"frames": frames, "scenarios": [
        {"name": "coop%d" % i, "scene": scene, "timeline": copy.deepcopy(timeline)}
        for i in range(scenarios)]}
    ledger = {}
    result = gh._playtest_spec(tmp_path / "proj", spec, frames, 30, ledger=ledger)
    assert len(calls) == scenarios + 1
    assert all(c["frames"] == frames and c["scene"] == scene and 0 < c["timeout"] <= 30 for c in calls)
    assert calls[-1]["driven"] is False and calls[-1]["render"] is False
    assert len(ledger["controls"]) == 1
    return result


@pytest.mark.parametrize("frames,scene", [
    (180, "res://scenes/coop/coop_scenario_fixture_en.tscn"),
    (235, "res://scenes/coop/coop_scenario_fixture.tscn"),
])
def test_complete_exit_debt_control_is_comparable_and_keeps_identity(monkeypatch, tmp_path, frames, scene):
    result = _report(monkeypatch, tmp_path, frames=frames, scene=scene)
    assert result["passed"] is True
    assert result["spec_errors"] == [] and result["errors"] == []
    assert result["behavior"]["scenarios"][0]["input_dead"] is False
    debt = result["native_debt"]
    assert len(debt) == 4
    assert all(d["native_class"] == "exit_leak" and d["blocking"] is False for d in debt)
    assert [d["control"] for d in debt if "control" in d] == ["control:%s@%d" % (scene, frames)] * 2
    assert [d["scenario"] for d in debt if "scenario" in d] == ["coop0"] * 2


@pytest.mark.parametrize("control", ["runtime", "parse", "native", "timeout", "missing", "malformed",
                                      "short", "false-complete", "bad-nodes", "spec-error"])
def test_unusable_control_still_fails_and_keeps_its_debt(monkeypatch, tmp_path, control):
    result = _report(monkeypatch, tmp_path, control=control)
    assert result["passed"] is False
    assert any("no-input control is not a complete comparable observation" in e for e in result["spec_errors"])
    assert result["behavior"]["scenarios"][0]["input_dead"] is False
    assert len([d for d in result["native_debt"] if "control" in d]) == 2
    if control in ("runtime", "parse", "native"):
        assert len(result["errors"]) == 1
        assert result["errors"][0]["control"].endswith("@180")


def test_equal_control_still_rejects_dead_input_with_exit_debt(monkeypatch, tmp_path):
    result = _report(monkeypatch, tmp_path, same_state=True)
    assert result["passed"] is False
    assert result["spec_errors"] == []
    assert result["behavior"]["scenarios"][0]["input_dead"] is True
    assert len(result["native_debt"]) == 4


def test_shared_control_debt_is_reported_once(monkeypatch, tmp_path):
    result = _report(monkeypatch, tmp_path, scenarios=2)
    assert result["passed"] is True
    assert len(result["native_debt"]) == 6
    assert len([d for d in result["native_debt"] if "control" in d]) == 2
