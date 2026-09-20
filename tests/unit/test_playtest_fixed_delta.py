"""The play-test clock is a FIXED DELTA, not a real-time throttle.

`Engine.max_fps = 60` bought a real property — a frame budget maps to a
determined slice of game time — by SLEEPING for it: 82,675 frames of a gate
divided by 60 is 1,378 s, 40.9% of a 3,373 s run. `--fixed-fps` hands the same
determined slice to _process without the wait. These tests pin the two halves
of that swap (the flag is passed; the cap is gone) and the capture default the
owner ruled on, because both are one-line regressions away from coming back.
"""

import importlib.util
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_ms = importlib.util.spec_from_file_location("godot_harness_fd", _HARNESS)
gh = importlib.util.module_from_spec(_ms)
_ms.loader.exec_module(gh)


def _capture_run(monkeypatch, tmp_path):
    """Run _run_probe with the engine replaced by a recorder."""
    seen = {}

    def fake(args, env, state_path, timeout, render, timing=None):
        seen["args"] = list(args)
        seen["env"] = dict(env)
        return {}, [], False

    monkeypatch.setattr(gh, "_probe_once", fake)
    gh._run_probe(tmp_path, tmp_path / "probe_state.json", 30, 10, {})
    return seen


def test_the_engine_is_given_a_fixed_delta_and_the_probe_does_not_throttle(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "PLAYTEST_FIXED_FPS", 60)
    seen = _capture_run(monkeypatch, tmp_path)
    assert ["--fixed-fps", "60"] == seen["args"][2:4], seen["args"]
    # The cap and the flag are alternatives. Passing both would re-introduce the
    # sleep the flag exists to remove, so the cap must be absent.
    assert "AITELIER_PROBE_MAX_FPS" not in seen["env"], seen["env"]


def test_the_old_real_time_cap_is_still_reachable_for_measurement(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "PLAYTEST_FIXED_FPS", 0)
    seen = _capture_run(monkeypatch, tmp_path)
    assert "--fixed-fps" not in seen["args"], seen["args"]
    assert seen["env"]["AITELIER_PROBE_MAX_FPS"] == "60"


def test_the_probe_never_caps_the_framerate_unconditionally():
    # The regression this guards is a one-liner: any `Engine.max_fps = <int>`
    # that is not behind the env fallback puts the 1,378 s back.
    src = _HARNESS.read_text()
    live = [l.strip() for l in src.splitlines()
            if "Engine.max_fps" in l and not l.strip().startswith("#")]
    assert live == ["Engine.max_fps = int(cap)"], live
    assert 'var cap := OS.get_environment("AITELIER_PROBE_MAX_FPS")' in src


def test_captures_are_off_by_default_and_on_demand_on_request():
    # Owner ruling 2026-09-17: the 443 MB of PNGs goes, the capability stays.
    assert gh.PLAYTEST_CAPTURES == 0
    assert gh._capture_frames(300, [{"at": 10, "assert": [{"node": "N", "expr": "x"}]}]) == []
    on_demand = gh._capture_frames(
        300, [{"at": 10, "assert": [{"node": "N", "expr": "x"}]}], limit=4)
    assert len(on_demand) == 4 and 10 in on_demand


def test_a_per_request_budget_reaches_the_scenario_loop(monkeypatch, tmp_path):
    """The on-demand path is what makes a red scenario re-photographable, so it
    is proven end-to-end from the parameter, not from the constant."""
    asked = []

    def fake(dst, state_path, frames, timeout, extra, scene="",
             capture_at=None, timing=None):
        asked.append(capture_at)
        return {"frames": frames, "asserts": [{"name": "a", "passed": True}],
                "nodes": {}}, [], False

    monkeypatch.setattr(gh, "_run_probe", fake)
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]}]}
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120, cap_limit=4)
    assert asked and asked[0] and len(asked[0]) == 4, asked
    asked.clear()
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert asked and asked[0] == [], asked


def test_turning_the_photographs_off_does_not_turn_the_renderer_off(monkeypatch, tmp_path):
    """The regression that nearly shipped in this round.

    `render = bool(capture_at)` made the photograph budget decide whether the
    engine drew anything, and --headless forces the dummy driver. Measured on 8
    wuxia scenarios, dropping the captures alone turned 5 of them red — "aim:
    node not found", screens that never opened — assertions failing for a reason
    the game had nothing to do with. So: no captures, still rendering.
    """
    seen = {}

    def fake(args, env, state_path, timeout, render, timing=None):
        seen["render"] = render
        seen["env"] = dict(env)
        return {"frames": 1}, [], False

    monkeypatch.setattr(gh, "_probe_once", fake)
    gh._run_probe(tmp_path, tmp_path / "s.json", 30, 10, {}, capture_at=None)
    assert seen["render"] is True
    assert "AITELIER_PROBE_CAPTURE" not in seen["env"]


def test_the_control_pass_stays_headless(monkeypatch, tmp_path):
    """The L0 control was headless before the decoupling and must stay headless:
    it is compared against the scenario by node digest, and changing what it
    boots under would move `input_dead` verdicts with it."""
    seen = {}

    def fake(dst, state_path, frames, timeout, extra, scene="",
             capture_at=None, timing=None, render=True):
        seen.setdefault("modes", []).append(render)
        return {"frames": frames, "asserts": [{"name": "a", "passed": True}],
                "nodes": {"N": {"x": len(seen["modes"])}}}, [], False

    monkeypatch.setattr(gh, "_run_probe", fake)
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 5, "press": "ui_accept"},
        {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]}]}
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    # [scenario, control]
    assert seen["modes"] == [True, False], seen["modes"]
