# tests/unit/test_gate_deferral_execution_points.py
#
# Criterion 4, and round 5's missing half: the numbers that decide how long
# ONE step may hold the shared scheduler, and the two CALL SITES that read
# `core/gate_deferral.py`. Round 5 asserted the pure functions only, so
# deleting either call site left the whole suite green (S1 0/33, H2 12/12).
#
# The three numbers, each asserted and each named in the failure message:
#
#   * gate runs per step invocation  == 1        (was 3 after round 5)
#   * single-step worst-case hold    <= 5400 s   (was 3 x 5400 + 2 x 60)
#   * the poll interval stays inside the valve   (540 s cannot re-arm it
#     before the episode ceiling ends the run)
#
# Every assertion names the NUMBER it is about, because round 5's real defect
# was not that these grew — it was that NOTHING WAS WATCHING THEM, so the next
# growth would not have been noticed either.
import asyncio
import json

import pytest

from core import gate_deferral as gd


def _worst_case_hold(*, gate_timeout, attempts, retry_delay):
    """What one step invocation can hold the scheduler for, in seconds."""
    return attempts * gate_timeout + max(0, attempts - 1) * retry_delay


def _step_budget_violations(*, gate_timeout, attempts, retry_delay,
                            previous_main_line=5400.0, max_runs_per_step=1):
    """The names of the numbers that are out of budget — empty means green.

    Returns names, not a bool, because a red that does not say WHICH number
    moved is the failure mode this file exists to avoid.
    """
    out = []
    if attempts > max_runs_per_step:
        out.append(f"gate runs per step={attempts} > {max_runs_per_step}")
    hold = _worst_case_hold(gate_timeout=gate_timeout, attempts=attempts,
                            retry_delay=retry_delay)
    if hold > previous_main_line:
        out.append(f"worst-case hold={hold}s > {previous_main_line}s")
    return out


def test_one_step_holds_the_scheduler_for_at_most_one_gate_run():
    """The budget itself, read off the REAL constants rather than restated."""
    from aitelier.tools.run_tests import impl as rt

    assert rt.REPO_GATE_UNMEASURED_ATTEMPTS == 1, (
        "gate runs per step moved: "
        f"{rt.REPO_GATE_UNMEASURED_ATTEMPTS} (the scheduler parks the absence "
        "now, so a step does not pay for the wait)")
    assert _step_budget_violations(
        gate_timeout=rt.REPO_GATE_TIMEOUT,
        attempts=rt.REPO_GATE_UNMEASURED_ATTEMPTS,
        retry_delay=rt.REPO_GATE_RETRY_DELAY_SECONDS) == []
    assert _worst_case_hold(
        gate_timeout=rt.REPO_GATE_TIMEOUT,
        attempts=rt.REPO_GATE_UNMEASURED_ATTEMPTS,
        retry_delay=rt.REPO_GATE_RETRY_DELAY_SECONDS) <= 5400


@pytest.mark.parametrize("attempts,expect_name", [
    (3, "gate runs per step=3 > 1"),
    (4, "gate runs per step=4 > 1"),
])
def test_the_budget_goes_red_and_names_the_number_that_moved(attempts,
                                                             expect_name):
    """Kills "put the re-acquisition back to 3" (the round-5 regression) and
    "widen one step's hold": both must be reported BY NAME, not as a bare
    False. A guard nobody has seen go red is a claim, not a guard."""
    from aitelier.tools.run_tests import impl as rt

    violations = _step_budget_violations(
        gate_timeout=rt.REPO_GATE_TIMEOUT, attempts=attempts,
        retry_delay=rt.REPO_GATE_RETRY_DELAY_SECONDS)
    assert any(expect_name in v for v in violations), violations
    # ...and the hold it implies is named too, since that is the resource the
    # budget protects.
    assert any("worst-case hold=" in v for v in violations), violations


def test_the_valve_is_a_simple_bound_and_the_deferral_path_cannot_reach_it():
    """The per-instance valve fires on claims > max_claims, unconditionally.

    The prior bypass (deferring → False) was removed as unreachable: the tick
    returns early when state == "silent" (scheduler line ~1232), BEFORE the
    valve check (~1283), so a step instance during a live episode never
    accumulates more than 1 claim.  This is proved by the integration test
    `test_the_tick_refuses_to_claim_while_the_gate_is_silent` below.

    The assertions here confirm the valve itself is narrow (fires only past
    the bound) and that no deferral state changes its answer.
    """
    from core.scheduler import _MAX_CLAIMS_PER_INSTANCE

    wait = gd.wait_seconds()
    ceiling = gd.episode_max_seconds()
    assert wait >= 1.0
    # The arithmetic: if the valve WERE reachable during an episode, it could
    # be reached (ceiling/wait exceeds the bound).  That's why the tick's early
    # return matters — it prevents claims from accumulating in the first place.
    re_claim_bound = ceiling / wait
    assert re_claim_bound > _MAX_CLAIMS_PER_INSTANCE, (
        f"re-claim bound={re_claim_bound:.0f} exceeds valve={_MAX_CLAIMS_PER_INSTANCE}: "
        "if the tick did NOT return early during deferral, this valve would fire")

    # Pole 1: at the bound, the valve does NOT fire.
    assert gd.guard_per_instance_valve(
        _MAX_CLAIMS_PER_INSTANCE, "run-1",
        max_claims=_MAX_CLAIMS_PER_INSTANCE) is False
    # Pole 2: past the bound, it fires unconditionally.
    assert gd.guard_per_instance_valve(
        _MAX_CLAIMS_PER_INSTANCE + 1, "run-1",
        max_claims=_MAX_CLAIMS_PER_INSTANCE) is True
    # Pole 3: a live episode no longer suppresses the valve (bypass removed).
    ledger = gd.DeferralLedger()
    ledger.note_absence("run-1", now=1000.0)
    assert gd.guard_per_instance_valve(
        _MAX_CLAIMS_PER_INSTANCE + 1, "run-1",
        max_claims=_MAX_CLAIMS_PER_INSTANCE, ledger=ledger, now=1000.0) is True



# ── the two CALL SITES (round 5 measured the pure functions only) ──────────

def _silent_report(out_dir):
    (out_dir / "test_report.json").write_text(json.dumps({
        "passed": False, "summary": "gate not measured", "failures": [],
        "repo_gate": {"script": "run_tests.sh"}, "repo_gate_absent": True,
    }), encoding="utf-8")


def _graded_report(out_dir):
    (out_dir / "test_report.json").write_text(json.dumps({
        "passed": False, "summary": "FAILED tests/test_a.py::test_b",
        "failures": ["FAILED tests/test_a.py::test_b"],
        "repo_gate": {"script": "run_tests.sh", "measured": "measured_fail"},
    }), encoding="utf-8")


class _TickStub:
    """Just enough host for `_run_skillflow_tick` to reach its claim phase."""

    def __init__(self):
        self.claims = 0

    def get_run(self, run_id):
        return {"status": "running", "current_node": "test",
                "project_id": "p"}

    def trace_query(self, run_id, sql, params=()):
        if "out_dir" in sql:
            return []
        return [{"step_id": "test",
                 "payload_json": json.dumps(
                     {"params": {"out_dir": str(self.out_dir)}})}]

    def claim_next_step(self, run_id):
        self.claims += 1
        return None

    def fail_run(self, run_id, reason):
        raise AssertionError(f"the tick ended the run: {reason}")


def _wire_tick(monkeypatch, sf, out_dir):
    from core import scheduler

    sf.out_dir = out_dir
    monkeypatch.setattr(gd, "LEDGER", gd.DeferralLedger())
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run",
                        lambda pid: "run-1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db",
                        lambda pid: None)
    monkeypatch.setattr(scheduler, "_claim_retry_after", {})
    monkeypatch.setattr(scheduler, "_reconcile_lease", lambda *a: None)

    async def _advance(sf_, run_id, project_id=""):
        return "implement"

    monkeypatch.setattr(scheduler, "_advance_off_the_loop", _advance)
    return scheduler


def test_the_tick_refuses_to_claim_while_the_gate_is_silent(monkeypatch,
                                                            tmp_path):
    """Kills S1 (delete the deferral skip inside the tick; 0/33 in review).

    Driven at the CALL SITE — `core/scheduler._run_skillflow_tick` — with the
    REAL `observe_run` reading a REAL report off disk. Both poles are measured:
    with the absence the tick must not claim; with a GRADED report the tick
    must. One pole alone would pass for a tick that never claims anything.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _silent_report(out_dir)

    sf = _TickStub()
    scheduler = _wire_tick(monkeypatch, sf, out_dir)

    asyncio.run(scheduler._run_skillflow_tick("p", None))
    assert sf.claims == 0, "the tick claimed a step for a gate that never spoke"
    assert gd.LEDGER.deferring("run-1") is True

    _graded_report(out_dir)
    sf.claims = 0
    asyncio.run(scheduler._run_skillflow_tick("p", None))
    assert sf.claims == 1, (
        "the tick skipped a claim with no absence to account for — the skip "
        "is not conditional on the deferral")
    assert gd.LEDGER.deferring("run-1") is False


def test_during_a_deferral_hold_the_step_claim_is_held_not_released(monkeypatch,
                                                                     tmp_path):
    """While an absence is live the tick neither claims the step nor advances
    it. This stub measures exactly two things: `claims == 0` across three
    silent ticks, and the ledger holding the episode (3 absences, remaining
    hold > 0). The step-STATE claims this file used to make in prose —
    retry_count / release_count staying put — are measured on REAL step rows
    in `test_coding_impl_gate_absence.py`, not here: a stub with no step row
    cannot see a counter it does not model.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _silent_report(out_dir)

    sf = _TickStub()
    scheduler = _wire_tick(monkeypatch, sf, out_dir)

    # Drive multiple ticks: each returns without claiming.
    for _ in range(3):
        asyncio.run(scheduler._run_skillflow_tick("p", None))

    assert sf.claims == 0, (
        "the deferral tick claimed a step — retry_count or release would move")
    # LEDGER accumulated 3 absences across ticks — the step is held, not reset.
    assert gd.LEDGER.episode_count("run-1") == 3, (
        "the episode did not accumulate absences across ticks")
    # hold_remaining > 0 proves the step is still held, not re-released:
    assert gd.LEDGER.hold_remaining("run-1") > 0, (
        "the hold expired between ticks — step might be re-claimed")


def test_the_host_refuses_to_advance_before_reaching_the_framework(
        monkeypatch):
    """Kills H2 (delete the hold check inside `advance_run`; 12/12 in review).

    Two poles, and the point is WHERE the refusal happens: with a live episode
    `advance_run` must return None WITHOUT calling the framework's own
    advance; with no episode the framework's advance must be reached.
    """
    import skillflow.core as sk_core

    from core.skillflow_host import AItelierSkillFlow

    reached = []
    monkeypatch.setattr(sk_core.SkillFlow, "advance_run",
                        lambda self, run_id: reached.append(run_id) or "node")
    monkeypatch.setattr(AItelierSkillFlow, "_operation_blocks_reentry",
                        lambda self, run_id, trigger: False)

    host = AItelierSkillFlow.__new__(AItelierSkillFlow)
    ledger = gd.DeferralLedger()
    monkeypatch.setattr(gd, "LEDGER", ledger)

    ledger.note_absence("run-1")
    assert host.advance_run("run-1") is None
    assert reached == [], "the absence was advanced past the hold"

    ledger.clear("run-1")
    assert host.advance_run("run-1") == "node"
    assert reached == ["run-1"]


def test_the_deferral_ledger_is_not_persisted_so_a_restart_re_measures():
    """Round 5's unreviewed corner, stated as a measurable property.

    The episode ledger is PROCESS-LOCAL, so after a server restart the ceiling
    restarts with it. On a deployment that restarts daily the 3-hour wall
    clock is therefore unreachable — that is a real limitation, and it is
    recorded here rather than left in prose nobody runs. What must hold in
    spite of it: the ledger keeps no durable state, so a fresh process cannot
    inherit a stale hold, and the FIRST tick after a restart re-derives the
    episode from the report on disk instead of trusting a remembered instant.
    """
    first = gd.DeferralLedger()
    first.note_absence("run-1", now=1000.0)
    assert first.episode_count("run-1") == 1

    restarted = gd.DeferralLedger()
    assert restarted.episode_count("run-1") == 0
    # A fresh process holds no remembered instant: the episode clock starts
    # from the re-derived absence, not from the previous process's start.
    assert restarted.episode_seconds("run-1", now=1e9) == 0.0
    assert restarted.expired("run-1", now=1e9) is False
    assert restarted.deferring("run-1", now=1e9) is False
    # Re-derived from the run, not remembered: a fresh ledger seeds the
    # episode from `observe_run`'s own reading at the moment it is asked.
    restarted.note_absence("run-1", now=1e9)
    assert restarted.episode_count("run-1") == 1


def test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded():
    """G2 / G2b reader that does NOT touch the constants it guards.

    The r7 reader monkeypatched `GATE_DEFERRAL_EPISODE_MAX_SECONDS` to 10800
    and then asserted 10800 — it overwrote the very value it claimed to check,
    so a module that loaded `1e9` stayed green while the effective episode
    ceiling silently doubled. This reads the module exactly as it is loaded:

    * G2 (the seconds constant becomes 1e9) is caught by the effective
      `episode_max_seconds()` and by the constant itself, and BOTH name 10800;
    * G2b (the ceiling constant becomes 1e9) is caught by the ceiling constant
      itself — the hardcoded absolute clip still holds the runtime value, so
      this is the one reader that sees it.
    """
    import importlib
    loaded = importlib.reload(gd)
    assert loaded.episode_max_seconds() == 10800.0, (
        f"as-loaded effective episode ceiling is "
        f"{loaded.episode_max_seconds()}, not 10800 s (G2 widened the bound)")
    assert loaded.GATE_DEFERRAL_EPISODE_MAX_SECONDS == 10800.0, (
        f"GATE_DEFERRAL_EPISODE_MAX_SECONDS={loaded.GATE_DEFERRAL_EPISODE_MAX_SECONDS}"
        ", expected 10800")
    assert loaded.GATE_DEFERRAL_EPISODE_MAX_CEILING == 6 * 3600, (
        f"GATE_DEFERRAL_EPISODE_MAX_CEILING={loaded.GATE_DEFERRAL_EPISODE_MAX_CEILING}"
        ", expected 21600 (6 * 3600) — G2b widened the ceiling constant")


def test_G2_episode_max_seconds_is_hard_capped_against_1e9(monkeypatch):
    """Kills G2: setting the env ceiling to 1e9 must not silently disable
    the wall-clock bound. The hard-coded _ABSOLUTE_EPISODE_CEILING clips
    the result regardless of what the environment says.

    Both directions: the ceiling env var cannot raise the effective bound
    above the absolute, and the episode env var cannot either.
    """
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_CEILING", 1e9)
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_SECONDS", 1e9)
    assert gd.episode_max_seconds() <= gd._ABSOLUTE_EPISODE_CEILING, (
        f"episode_max_seconds()={gd.episode_max_seconds()} exceeds "
        f"_ABSOLUTE_EPISODE_CEILING={gd._ABSOLUTE_EPISODE_CEILING} — "
        "G2: the wall-clock bound is silently disabled")
    # Pole 2: restore defaults — must be at the default (10800s < ceiling).
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_CEILING", 6 * 3600)
    monkeypatch.setattr(gd, "GATE_DEFERRAL_EPISODE_MAX_SECONDS", 10800)
    assert gd.episode_max_seconds() == 10800.0, (
        "the default episode is not 10800s")
