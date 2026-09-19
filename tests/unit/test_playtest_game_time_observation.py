"""THE PROPERTY THIS ROUND BOUGHT, AND THE THING THAT WATCHES IT.

`--fixed-fps N` is supposed to make a frame budget mean a determined slice of
game time: N frames, N/60 seconds, every run, whatever the machine is doing.
That was measured once by hand and then went unwatched — the tests that shipped
with it mock `_probe_once` out, so not one frame has ever run under them, and
they would all stay green if the engine started handing out any delta it liked.

These tests are about the OBSERVATION POINT instead: the probe reads the game
time it was handed, the ledger carries it next to what it should have been, and
a run where the two disagree HARD-fails rather than quietly measuring a
different game than the author wrote.

The engine-in-the-loop half of this lives in `docker/godot/game_time_probe.py`,
which runs real frames at all three clock settings and writes its numbers to a
file. It is not a unit test because it takes the global engine lock.
"""

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = _ROOT / "docker" / "godot" / "godot_harness.py"
_ms = importlib.util.spec_from_file_location("godot_harness_gt", _HARNESS)
gh = importlib.util.module_from_spec(_ms)
_ms.loader.exec_module(gh)


def _row(name, frames, game_time):
    return {"name": name, "frames_stepped": frames, "game_time_sec": game_time}


# ── the checker, both polarities ───────────────────────────────────────────

def test_a_run_whose_frames_bought_exactly_their_game_time_reports_nothing():
    rows = [_row("a", 230, 230 / 60), _row("b", 1030, 1030 / 60),
            _row("c", 180, 3.0)]
    assert gh.determined_game_time_findings(rows, 60) == []


def test_a_scenario_whose_frames_bought_the_wrong_game_time_is_reported():
    # 230 frames at 60 fps must be 3.8333 s. This one got what the OLD
    # real-time cap gave it (measured 2026-09-17: 5.05 s), which is the exact
    # shape of the regression — the flag silently stopping and the cap coming
    # back would look like this and nothing else would notice.
    rows = [_row("ok", 230, 230 / 60), _row("drifted", 230, 5.05)]
    found = gh.determined_game_time_findings(rows, 60)
    assert len(found) == 1, found
    assert "drifted" in found[0]
    assert "3.833333" in found[0] and "5.050000" in found[0], found[0]


def test_the_tolerance_is_about_rounding_and_not_about_machine_load():
    # Float accumulation over a few thousand frames, yes. A whole frame, no:
    # one missing frame of game time is a real change in what the scenario saw.
    rows = [_row("rounding", 600, 600 / 60 + 0.0005)]
    assert gh.determined_game_time_findings(rows, 60) == []
    rows = [_row("one_frame_short", 600, 600 / 60 - 1 / 60)]
    assert len(gh.determined_game_time_findings(rows, 60)) == 1


def test_with_the_flag_off_there_is_no_expectation_to_check():
    # fixed_fps <= 0 is the deliberate fallback to the real-time cap, under
    # which frames/N is NOT what the budget buys. Saying nothing is right;
    # reporting every scenario would be a checker that fires on its own
    # configuration.
    rows = [_row("legacy", 230, 5.05)]
    assert gh.determined_game_time_findings(rows, 0) == []


def test_a_scenario_that_never_stepped_a_frame_is_not_accused():
    assert gh.determined_game_time_findings([_row("crashed", 0, 0.0)], 60) == []


# ── the reading itself ─────────────────────────────────────────────────────

def test_the_probe_sums_the_delta_the_engine_hands_it_and_reports_it():
    src = _HARNESS.read_text()
    # Summing the _process ARGUMENT is the only reading that cannot disagree
    # with the game. A second clock (Time.get_ticks_usec) would measure real
    # time, which under a fixed delta is a different quantity entirely.
    assert "_game_usec += _d * 1000000.0" in src
    assert '"game_usec": int(_game_usec)' in src


def test_the_ledger_line_carries_the_measurement_beside_its_expectation(monkeypatch):
    monkeypatch.setattr(gh, "PLAYTEST_FIXED_FPS", 60)
    row = gh._scenario_ledger("s", "res://x.tscn", 4.0, {
        "boot_usec": 1_000_000, "step_usec": 3_000_000, "engine_usec": 4_000_000,
        "game_usec": 3_833_333, "frames_stepped": 230, "proc_sec": 4.0})
    assert row["game_time_sec"] == pytest.approx(3.833333, abs=1e-6)
    assert row["expected_game_time_sec"] == pytest.approx(230 / 60, abs=1e-6)


def test_the_ledger_states_no_expectation_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(gh, "PLAYTEST_FIXED_FPS", 0)
    row = gh._scenario_ledger("s", "", 4.0, {"game_usec": 5_050_000,
                                             "frames_stepped": 230})
    assert row["game_time_sec"] == pytest.approx(5.05, abs=1e-6)
    assert row["expected_game_time_sec"] is None


# ── and it is HARD, not advisory ───────────────────────────────────────────

def _spec_run(monkeypatch, tmp_path, game_usec):
    """Drive _playtest_spec with the engine replaced by a recorder that reports
    `game_usec` for a 50-frame scenario."""
    def fake_run_probe(dst, state_path, frames, timeout, extra, scene="",
                       capture_at=None, timing=None, render=True):
        if timing is not None:
            timing["frames_stepped"] = frames
            timing["game_usec"] = game_usec
            timing["engine_usec"] = 1_000_000
            timing["proc_sec"] = 1.0
        probe_timing = {"frames_stepped": frames, "game_usec": game_usec}
        # A distinct node tree per pass, so the L0 no-input control can never
        # read equal and turn this into an input_dead failure instead.
        nodes = {"N": {"v": "driven" if extra.get("AITELIER_PROBE_SPEC") and
                       "assert" in Path(extra["AITELIER_PROBE_SPEC"]).read_text()
                       else "idle"}}
        return ({"asserts": [{"name": "a", "node": "N", "expr": "1 == 1",
                              "passed": True, "actual": True, "error": "",
                              "frame": 20}],
                 "nodes": nodes, "captures": [],
                 "timing": probe_timing}, [], False)

    monkeypatch.setattr(gh, "_run_probe", fake_run_probe)
    monkeypatch.setattr(gh, "PLAYTEST_FIXED_FPS", 60)
    spec = {"scene": "res://main.tscn", "scenarios": [
        {"name": "s", "timeline": [
            {"at": 20, "press": "ui_accept"},
            {"at": 20, "assert": {"N.v": "v == \"driven\""}}]}]}
    return gh._playtest_spec(tmp_path / "proj", spec, 50, 10, ledger={})


def test_the_gate_goes_red_when_the_frames_stopped_buying_their_game_time(
        monkeypatch, tmp_path):
    (tmp_path / "proj").mkdir()
    # 50 frames at 60 fps is 0.8333 s; this run reports 1.2 s.
    res = _spec_run(monkeypatch, tmp_path, 1_200_000)
    assert res["passed"] is False
    assert any("determined slice of game time" in e for e in res["spec_errors"]), \
        res["spec_errors"]
    # And the summary must not call it a malformed timeline entry — that would
    # send the next reader to the wrong file.
    assert "spec violation" in res["summary"], res["summary"]


def test_the_gate_stays_green_when_they_did(monkeypatch, tmp_path):
    (tmp_path / "proj").mkdir()
    res = _spec_run(monkeypatch, tmp_path, 50 * 1_000_000 // 60)
    assert res["spec_errors"] == []
    assert res["passed"] is True
