# tests/unit/test_gate_deferral_is_accounted.py
#
# "A gate that never ran is not a failing gate" — round 5. The wall-clock
# bound protects a SHARED resource (the scheduler must not be held forever);
# the accounting rule protects the LEDGER (an absence may never be charged to
# the implementer). Both hold at once: the episode lets the run end, and when
# it ends it NAMES THE ABSENCE.
#
# Every assertion here was checked by mutating the implementation and watching
# it fail; the mutation each test kills is named in its docstring.
import json

import pytest

from core import gate_deferral as gd


@pytest.fixture
def ledger(monkeypatch):
    fresh = gd.DeferralLedger()
    monkeypatch.setattr(gd, "LEDGER", fresh)
    return fresh


def _absence_report(tmp_path, gate="run_tests.sh", flag="repo_gate_absent"):
    path = tmp_path / "test_report.json"
    path.write_text(json.dumps({
        "passed": False, "summary": "gate not measured", "failures": [],
        "repo_gate": {"script": gate},
        flag: True,
    }), encoding="utf-8")
    return path


# ── the three states of one observed absence ───────────────────────────────

def test_a_report_that_graded_the_code_states_no_absence(tmp_path):
    """Kills "any red is treated as an absence": a report that says the code
    failed is a MEASUREMENT, and the implementer does own that one."""
    path = tmp_path / "test_report.json"
    path.write_text(json.dumps({
        "passed": False, "summary": "FAILED tests/test_ops.py::test_sub",
        "failures": ["FAILED tests/test_ops.py::test_sub"], "repo_gate": {
            "script": "run_tests.sh", "measured": "measured_fail"}}),
        encoding="utf-8")
    assert gd.read_absence(path) is None
    assert gd.observe_run(None, "r", report_path=path)["state"] == "none"


def test_no_report_at_all_is_not_an_absence(tmp_path, ledger):
    """A run before its gate step has no report; that is not silence."""
    assert gd.observe_run(None, "r", report_path=tmp_path / "missing.json"
                          )["state"] == "none"


def test_a_declared_absence_is_silent_inside_its_wait(tmp_path, ledger):
    """Kills "expire immediately": inside the wait the tick spends nothing."""
    path = _absence_report(tmp_path)
    out = gd.observe_run(None, "r", now=1000.0, report_path=path)
    assert out["state"] == "silent"
    assert out["remaining"] == pytest.approx(gd.wait_seconds())
    assert out["gate"] == "run_tests.sh"
    # ...and it stays silent for the whole wait, not one tick.
    still = gd.observe_run(None, "r", now=1000.0 + gd.wait_seconds() / 2,
                           report_path=path)
    assert still["state"] == "silent"
    assert still["remaining"] > 0


def test_the_episode_does_not_restart_on_every_tick(tmp_path, ledger):
    """Kills "measure the ceiling from the last tick": a 20-tick episode
    would then never expire, which is the wall-clock bound being removed."""
    path = _absence_report(tmp_path)
    for i in range(20):
        gd.observe_run(None, "r", now=1000.0 + i * 10, report_path=path)
    assert ledger.episode_count("r") == 20
    out = gd.observe_run(None, "r",
                         now=1000.0 + gd.episode_max_seconds() + 1,
                         report_path=path)
    assert out["state"] == "expired"


# ── an expired absence ends the run, NAMING THE ABSENCE ────────────────────

def test_an_expired_absence_names_the_absence_and_never_the_code(tmp_path,
                                                                 ledger):
    """Kills "fail with `Cycle limit exceeded`". The run may end past the
    ceiling; the sentence it ends with must name the gate that never spoke."""
    path = _absence_report(tmp_path)
    gd.observe_run(None, "r", now=1000.0, report_path=path)
    out = gd.observe_run(
        None, "r", now=1000.0 + gd.episode_max_seconds() + 1,
        report_path=path)

    assert out["state"] == "expired"
    assert gd.ABSENCE_TERMINAL in out["reason"]
    assert gd.absent_terminal_names_no_failure(out["reason"])
    lowered = out["reason"].lower()
    for word in ("cycle limit exceeded", "failed", "assert", "regression"):
        assert word not in lowered, word


def test_a_zero_ceiling_cannot_expire_a_run_instantly(monkeypatch, tmp_path):
    """Kills "ceiling 0 -> expire the first absence": a zero ceiling is a
    mis-set knob, and a mis-set knob may not charge an absence to the code."""
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_SECONDS", 0)
    assert gd.episode_max_seconds() > 0
    path = _absence_report(tmp_path)
    out = gd.observe_run(None, "r", now=1000.0, report_path=path)
    assert out["state"] == "silent"


# ── both knobs are bounded, and both directions have a guard ───────────────

def test_the_wait_cannot_be_widened_until_the_ceiling_disappears(monkeypatch):
    """Kills "wait = 24h / 1e9" — the fifth round of buying the same ticket.
    Widening the poll interval is not a repair; it is the wall-clock bound
    being spent instead of resolved."""
    monkeypatch.setattr(gd, "GATE_DEFERRAL_WAIT_SECONDS", 24 * 3600)
    assert gd.wait_seconds() <= gd.GATE_DEFERRAL_WAIT_MAX
    monkeypatch.setattr(gd, "GATE_DEFERRAL_WAIT_SECONDS", 1e9)
    assert gd.wait_seconds() <= gd.GATE_DEFERRAL_WAIT_MAX
    monkeypatch.setattr(gd, "GATE_DEFERRAL_WAIT_SECONDS", 0)
    assert gd.wait_seconds() > 0


def test_the_episode_ceiling_cannot_be_removed(monkeypatch):
    """Kills "episode max = 1e9" and "the ceiling never refuses"."""
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_SECONDS", 1e9)
    assert gd.episode_max_seconds() <= gd.GATE_DEFERRAL_EPISODE_MAX_CEILING
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_SECONDS", -5)
    assert gd.episode_max_seconds() > 0


def test_hold_remaining_is_not_constant_zero(monkeypatch, ledger):
    """Kills "make `hold_remaining` always return 0": that silently turns the
    hold into a no-op, so every tick re-claims and the episode never forms."""
    monkeypatch.setattr(gd, "GATE_DEFERRAL_WAIT_SECONDS", 60)
    ledger.note_absence("r", now=1000.0)
    assert ledger.hold_remaining("r", now=1000.0) == pytest.approx(60)
    assert ledger.hold_remaining("r", now=1030.0) == pytest.approx(30)
    assert ledger.hold_remaining("nobody", now=1000.0) == 0.0


# ── the latent coupling: the deferral is what re-opens a completed row ─────

def test_the_per_instance_valve_does_not_fire_while_an_episode_is_live(ledger):
    """Kills "delete the guard at the valve" (37/6 fire counts in review).

    `core/scheduler.py:31` documents the valve's premise: a single instance is
    only re-claimed when something reset a completed row back to `pending`.
    A deferral does exactly that on purpose, so past 20 claims the valve would
    kill the run with a message that blames the step — a false attribution.
    """
    ledger.note_absence("r", now=1000.0)
    assert gd.guard_per_instance_valve(38, "r", max_claims=20,
                                      ledger=ledger, now=1000.0) is False


def test_the_valve_still_fires_when_no_absence_is_being_accounted(ledger):
    """The guard is narrow: a genuine runaway instance is still a runaway."""
    assert gd.guard_per_instance_valve(21, "r", max_claims=20,
                                      ledger=ledger, now=1000.0) is True
    assert gd.guard_per_instance_valve(20, "r", max_claims=20,
                                      ledger=ledger, now=1000.0) is False


def test_the_episode_ceiling_replaces_the_valve_that_it_disarmed(ledger):
    """The coupling is a real assertion, not a comment: the valve is off for
    the whole episode and the EPISODE is what ends the run, so the run is
    bounded either way. Below the production wait the two bounds meet — with
    `WAIT <= MAX_CLAIMS * wait`, the valve re-arms before claims could pile up
    unboundedly."""
    ledger.note_absence("r", now=1000.0)
    assert gd.guard_per_instance_valve(38, "r", max_claims=20,
                                      ledger=ledger, now=1000.0) is False
    expired_at = 1000.0 + gd.episode_max_seconds() + 1
    assert ledger.expired("r", now=expired_at) is True
    # Expired -> the run is ended by the absence, and the valve is back.
    assert gd.guard_per_instance_valve(38, "r", max_claims=20,
                                      ledger=ledger, now=expired_at) is True


# ── the other execution point: the host refuses to advance ────────────────

def test_the_host_refuses_to_advance_a_run_whose_gate_is_silent(ledger):
    """Kills "delete the hold check in `AItelierSkillFlow.advance_run`": the
    tick is not the only driver, and advancing re-enters the implement loop."""
    ledger.note_absence("r", now=1000.0)
    assert gd.hold_blocks_advance("r", ledger=ledger, now=1000.0) is True
    assert gd.hold_blocks_advance("r", ledger=ledger,
                                 now=1000.0 + gd.episode_max_seconds() + 1
                                 ) is False


def test_a_cleared_episode_stops_holding_the_run(ledger):
    """A gate that finally speaks clears the deferral, so the run resumes."""
    ledger.note_absence("r", now=1000.0)
    ledger.clear("r")
    assert gd.hold_blocks_advance("r", ledger=ledger, now=1000.0) is False
    assert ledger.hold_remaining("r", now=1000.0) == 0.0


# ── the terminal-reason CHECKER has both poles (round 5 had one) ───────────

def test_the_absent_terminal_checker_refuses_a_sentence_that_blames_the_code():
    """Round 5 left this checker with a SINGLE pole: pinning it to
    unconditional `True` kept the whole suite green, because every test only
    ever handed it a good sentence and asked for True. A checker with one pole
    is not a checker. So both directions are measured here, with the forbidden
    vocabulary the implementation actually names.
    """
    good = gd.ABSENCE_TERMINAL + " (run_tests.sh, 3 attempt(s))"
    assert gd.absent_terminal_names_no_failure(good) is True

    for bad in (
        good + " — Cycle limit exceeded",
        good + " — the suite failed",
        good + " — 3 failures",
        good + " — a regression in test_ops",
        good + " — the gate reported red",
        good + " — error: builder unreachable",
        "Cycle limit exceeded (test, 4 attempt(s))",
        "the tests failed",
        "",
    ):
        assert gd.absent_terminal_names_no_failure(bad) is False, bad

    # The word-boundary rule the implementation documents, stated as a pole of
    # its own: "measured" CONTAINS "red", and the honest absence sentence
    # contains "measured" — a naive substring check would refuse the very
    # sentence it exists to accept.
    assert "measured" in good.lower()
    assert gd.absent_terminal_names_no_failure(good) is True
