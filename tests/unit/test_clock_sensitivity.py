"""The detector for "this scenario is green because its frames were expensive".

Four scenarios on the wuxia tree were green under `Engine.max_fps = 60` only
because photographing four frames per run was worth 1.2 s of extra game time in
a 230-frame budget. Nothing declared that dependency and nothing could see it:
the 2x2 matrix that found it was run by hand, once, and wired into nothing.

`docker/godot/clock_sensitivity.py` runs each scenario under both clocks, with
and without an injected per-frame real cost, and reports every assertion the
real-time-capped pair disagrees about (the dependency) and every assertion the
fixed-delta pair disagrees about (the immunity, which must be empty).

These tests pin the comparison and the way the two questions are kept apart.
The engine half is a measurement, not a unit test: it runs the real scenarios in
both polarities and lands its output.
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MOD = _ROOT / "docker" / "godot" / "clock_sensitivity.py"
_ms = importlib.util.spec_from_file_location("clock_sensitivity", _MOD)
cs = importlib.util.module_from_spec(_ms)
_ms.loader.exec_module(cs)


def _report(*scenarios):
    return {"behavior": {"scenarios": list(scenarios)}}


def _scenario(name, asserts):
    return {"name": name, "asserts": [
        {"name": n, "node": n.split(".")[0], "expr": e, "frame": f,
         "passed": p, "actual": a}
        for (n, e, f, p, a) in asserts]}


PHASE = ("CombatManager.phase", 'phase == "PLAYER_TURN"', 200)
MARKER = ("CombatManager.acting_marker_visible", "acting_marker_visible == true", 100)


def _cells(load0, loaded, fixed0=None, fixedL=None):
    """Four reports keyed the way run_cells returns them. By default the fixed
    pair is a copy of the cheap real-time pass, i.e. immune."""
    base = fixed0 if fixed0 is not None else load0
    return {"fixed_delta_load0": base,
            "fixed_delta_loaded": fixedL if fixedL is not None else base,
            "real_time_cap_load0": load0,
            "real_time_cap_loaded": loaded}


# ── the detector, both polarities ──────────────────────────────────────────

def test_a_scenario_whose_verdict_does_not_move_with_frame_cost_is_not_flagged():
    r = _report(_scenario("quiet", [PHASE + (True, True), MARKER + (True, True)]))
    out = cs.analyse(_cells(r, r))
    assert out["findings"] == []
    assert out["scenarios_flagged"] == []
    assert out["fixed_delta_immunity_broken"] == []


def test_a_scenario_that_is_green_only_because_the_frames_were_expensive_is_flagged():
    # The shape of end_turn_button_click_hands_over_turn: same assertion
    # identity, red when the frames are cheap, green when they are not.
    cheap = _report(_scenario("end_turn", [PHASE + (False, False),
                                           MARKER + (True, True)]))
    dear = _report(_scenario("end_turn", [PHASE + (True, True),
                                          MARKER + (True, True)]))
    out = cs.analyse(_cells(cheap, dear))
    assert out["scenarios_flagged"] == ["end_turn"]
    f = out["findings"]
    assert len(f) == 1 and f[0]["kind"] == "verdict_differs"
    assert f[0]["assertion"] == "CombatManager.phase" and f[0]["frame"] == 200
    assert f[0]["real_time_cap_load0"] is False
    assert f[0]["real_time_cap_loaded"] is True


def test_an_assertion_reached_under_only_one_cost_is_flagged_too():
    # The strongest form of the dependency: one run never got far enough to
    # judge it. Silence here would read as agreement.
    cheap = _report(_scenario("half", [PHASE + (True, True)]))
    dear = _report(_scenario("half", [PHASE + (True, True), MARKER + (True, True)]))
    out = cs.analyse(_cells(cheap, dear))
    assert [x["kind"] for x in out["findings"]] == ["missing"]
    assert out["findings"][0]["real_time_cap_load0"] is None


def test_the_identity_is_the_key_and_the_actual_value_is_not():
    # Two runs may legally disagree about a number while agreeing about the
    # verdict — debug_enemy_round_msec is literally a CPU-time reading. Keying
    # on `actual` would flag every scenario and the detector would be noise.
    cheap = _report(_scenario("floaty", [
        ("CombatManager.debug_enemy_round_msec",
         "debug_enemy_round_msec <= 10000", 200, True, 287)]))
    dear = _report(_scenario("floaty", [
        ("CombatManager.debug_enemy_round_msec",
         "debug_enemy_round_msec <= 10000", 200, True, 1581)]))
    assert cs.analyse(_cells(cheap, dear))["findings"] == []


def test_a_moved_assertion_is_a_different_assertion():
    # `frame` is part of the identity, so raising an `at:` shows up as a removal
    # plus an addition rather than as a silent re-aiming.
    a = _report(_scenario("moved", [PHASE + (True, True)]))
    b = _report(_scenario("moved", [
        ("CombatManager.phase", 'phase == "PLAYER_TURN"', 270, True, True)]))
    found = cs.analyse(_cells(a, b))["findings"]
    assert {f["frame"] for f in found} == {200, 270}
    assert all(f["kind"] == "missing" for f in found)


def test_an_empty_pass_does_not_read_as_agreement():
    # A crashed pass returns no scenarios. Comparing it against a real one must
    # say so rather than return "no differences".
    real = _report(_scenario("s", [PHASE + (True, True)]))
    out = cs.analyse(_cells({}, real))
    assert len(out["findings"]) == 1
    assert out["findings"][0]["kind"] == "missing"


# ── the immunity is a SEPARATE question and is reported separately ─────────

def test_the_fixed_delta_losing_its_immunity_is_reported_on_its_own_line():
    # If the fixed-delta pair ever disagrees, the property this whole round was
    # about is gone. That is worse news than a budget-critical scenario and must
    # not be mixed into the same list.
    quiet = _report(_scenario("s", [PHASE + (True, True)]))
    broken = _report(_scenario("s", [PHASE + (False, False)]))
    out = cs.analyse(_cells(quiet, quiet, fixed0=quiet, fixedL=broken))
    assert out["findings"] == []
    assert out["fixed_delta_immunity_broken"] == ["s"]
    assert out["immunity_findings"][0]["fixed_delta_load0"] is True
    assert out["immunity_findings"][0]["fixed_delta_loaded"] is False


def test_the_two_questions_do_not_borrow_each_others_evidence():
    # A scenario may be both: flagged by the real-time pair AND immune under the
    # fixed delta. That is the normal, healthy reading for the four known ones.
    cheap = _report(_scenario("end_turn", [PHASE + (False, False)]))
    dear = _report(_scenario("end_turn", [PHASE + (True, True)]))
    out = cs.analyse(_cells(cheap, dear, fixed0=cheap, fixedL=cheap))
    assert out["scenarios_flagged"] == ["end_turn"]
    assert out["fixed_delta_immunity_broken"] == []


# ── and the knob it leans on has to exist in the probe ─────────────────────

def test_the_probe_can_be_told_to_make_a_frame_cost_more():
    src = (_ROOT / "docker" / "godot" / "godot_harness.py").read_text()
    assert 'OS.get_environment("AITELIER_PROBE_FRAME_LOAD_USEC")' in src
    assert "OS.delay_usec(_frame_load_usec)" in src
