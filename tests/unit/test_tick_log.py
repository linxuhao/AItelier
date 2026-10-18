"""The tick has nine ways to return; most of them used to be silent.

"The run is not moving" gave you nothing to look at — a stuck project looked
exactly like an idle one. A dpe_default run sat at `running:1` for 47 minutes
while `claim_next_step` raised the same actionable sentence on every single tick,
and the only way to see it was to call claim_next_step by hand.

The tick log is its own rolling file because the 5-second cadence would drown the
container log, and idle ticks are coalesced because ~17k lines a day of "nothing
to do" would push the informative lines out of the rotation window.
"""
import logging
import time

import pytest

from core import scheduler


@pytest.fixture
def caplines(monkeypatch):
    """Capture what tick_log would write, without touching the real file."""
    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    lg = logging.getLogger("aitelier.scheduler.tick.test")
    lg.handlers = [_Sink()]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    monkeypatch.setattr(scheduler, "_get_tick_logger", lambda: lg)
    monkeypatch.setattr(scheduler, "_tick_last_idle", 0.0)
    monkeypatch.setattr(scheduler, "_tick_last_paused", {})
    return lines


class TestItRecordsWhatTheTickDecided:
    def test_the_outcome_and_project_are_greppable(self, caplines):
        scheduler.tick_log("p1", "claim_failed", run="9d9d1c5f",
                           error="RequiredContextMissing: ... finalize")
        assert len(caplines) == 1
        line = caplines[0]
        assert "project=p1" in line
        assert "outcome=claim_failed" in line
        assert "run=9d9d1c5f" in line
        assert "finalize" in line

    def test_empty_detail_values_are_omitted(self, caplines):
        scheduler.tick_log("p1", "no_claim", run="abc", node=None, step="")
        assert "node=" not in caplines[0] and "step=" not in caplines[0]

    def test_a_projectless_tick_still_reads_cleanly(self, caplines):
        scheduler.tick_log("", "idle")
        assert "project=- outcome=idle" in caplines[0]


class TestIdleIsCoalesced:
    def test_consecutive_idle_ticks_collapse(self, caplines):
        for _ in range(50):
            scheduler.tick_log("", "idle")
        assert len(caplines) == 1, "17k idle lines a day would evict the real ones"

    def test_it_heartbeats_again_after_the_interval(self, caplines, monkeypatch):
        scheduler.tick_log("", "idle")
        monkeypatch.setattr(
            scheduler, "_tick_last_idle",
            time.time() - scheduler._TICK_IDLE_HEARTBEAT_S - 1)
        scheduler.tick_log("", "idle")
        assert len(caplines) == 2

    def test_a_real_outcome_is_never_coalesced(self, caplines):
        for i in range(5):
            scheduler.tick_log("p1", "executed", step=f"s{i}")
        assert len(caplines) == 5


class TestItCannotBreakTheTick:
    def test_a_broken_logger_is_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("disk full")
        monkeypatch.setattr(scheduler, "_get_tick_logger", _boom)
        scheduler.tick_log("p1", "executed")      # must not raise

    def test_an_unformattable_detail_is_swallowed(self, caplines):
        class _Bad:
            def __repr__(self):
                raise ValueError("nope")
        scheduler.tick_log("p1", "executed", weird=_Bad())   # must not raise


class TestATerminalTickSaysWhy:
    """A routing dead end is reported by skillflow as a FAILED RUN, not an
    exception: advance_run() writes the reason onto the run row and returns
    None, so the terminal branch is the tick's only chance to say it. Logging
    just `status=failed` is what sent the operator of the 104-task benchmark
    sweep into sqlite to find out why nl2repo-asteval had stopped.
    """

    @staticmethod
    def _stub(monkeypatch, run):
        from unittest.mock import MagicMock
        sf = MagicMock()
        sf.trace_query.return_value = [[0]]
        sf.get_run.return_value = run
        monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
        monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: "run1")
        monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
        monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *a: None)
        monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
        return sf

    async def test_a_failed_run_logs_its_error_reason(self, caplines, monkeypatch):
        self._stub(monkeypatch, {
            "status": "failed",
            "error_reason": "No matching transition from 't_impl_review' with flags {}",
        })

        await scheduler._run_skillflow_tick("nl2repo-asteval", None)

        line = next(l for l in caplines if "outcome=terminal" in l)
        assert "status=failed" in line
        assert "No matching transition from 't_impl_review'" in line

    async def test_a_completed_run_carries_no_reason(self, caplines, monkeypatch):
        self._stub(monkeypatch, {"status": "completed", "error_reason": None})

        await scheduler._run_skillflow_tick("p1", None)

        line = next(l for l in caplines if "outcome=terminal" in l)
        assert "status=completed" in line
        assert "reason=" not in line


class _FakeClock:
    """Deterministic clock: no sleeps and no wall-clock races in these tests."""

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name):
        # Other scheduler paths use strftime/gmtime through the same alias.
        return getattr(time, name)


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(scheduler, "_time", clock)
    monkeypatch.setattr(scheduler, "_tick_last_paused", {})
    return clock


class TestPausedTicksHeartbeatPerProject:
    """Observed live: two unchanged paused runs each wrote 20 identical
    `outcome=terminal status=paused` lines in 95s — 40 lines of rotation
    pressure that said nothing new. An unchanged pause now says it once, then
    at most once per 60s PER PROJECT; every real change (run, status, reason,
    detail) and every pause that follows another outcome stays immediate.
    """

    @staticmethod
    def _paused(project="p1", run="run1", **detail):
        scheduler.tick_log(project, "terminal", run=run, status="paused",
                           **detail)

    def test_the_first_paused_line_is_logged_at_once(self, caplines, fake_clock):
        self._paused()
        assert len(caplines) == 1
        assert "project=p1 outcome=terminal" in caplines[0]
        assert "status=paused" in caplines[0]

    def test_unchanged_repeats_inside_the_window_collapse(
            self, caplines, fake_clock):
        for _ in range(10):
            self._paused()
            fake_clock.advance(5)
        assert len(caplines) == 1, "the old code wrote one line per tick"

    def test_the_observed_95s_window_no_longer_floods(
            self, caplines, fake_clock):
        # 20 ticks at 5s: the exact window and shape of FIRST-OBSERVATION.json.
        for i in range(20):
            self._paused()
            if i < 19:
                fake_clock.advance(5)
        assert len(caplines) == 2, "one first line plus the t=60 heartbeat"

    def test_the_heartbeat_fires_at_the_boundary_and_not_before(
            self, caplines, fake_clock):
        self._paused()
        fake_clock.advance(scheduler._TICK_PAUSED_HEARTBEAT_S - 1)
        self._paused()
        assert len(caplines) == 1
        fake_clock.advance(1)
        self._paused()
        assert len(caplines) == 2

    def test_projects_do_not_suppress_each_other(self, caplines, fake_clock):
        self._paused(project="p1")
        self._paused(project="p2")
        self._paused(project="p1")
        self._paused(project="p2")
        assert [line.split()[0] for line in caplines] == [
            "project=p1", "project=p2"]

    def test_a_changed_run_is_logged_immediately(self, caplines, fake_clock):
        self._paused(run="run1")
        self._paused(run="run2")
        assert len(caplines) == 2
        assert "run=run2" in caplines[1]

    def test_a_changed_status_is_logged_immediately(
            self, caplines, fake_clock):
        self._paused(run="run1")
        scheduler.tick_log("p1", "terminal", run="run1", status="completed")
        assert len(caplines) == 2
        assert "status=completed" in caplines[1]

    def test_a_changed_reason_is_logged_immediately(
            self, caplines, fake_clock):
        self._paused(run="run1", reason="waiting for input")
        self._paused(run="run1", reason="waiting for review")
        assert len(caplines) == 2
        assert "reason=waiting for review" in caplines[1]

    def test_a_changed_detail_is_logged_immediately(
            self, caplines, fake_clock):
        self._paused(run="run1", node="5_review")
        self._paused(run="run1", node="6_final")
        assert len(caplines) == 2
        assert "node=6_final" in caplines[1]

    def test_a_pause_after_real_progress_is_never_hidden(
            self, caplines, fake_clock):
        self._paused(run="run1")
        scheduler.tick_log("p1", "advanced", run="run1", step="s1")
        self._paused(run="run1")
        assert len(caplines) == 3, "a naive timer would hide the second pause"
        assert "outcome=advanced" in caplines[1]
        assert "status=paused" in caplines[2]

    def test_completed_and_failed_are_never_coalesced(
            self, caplines, fake_clock):
        for _ in range(3):
            scheduler.tick_log("p1", "terminal", run="run1",
                               status="completed")
            scheduler.tick_log("p1", "terminal", run="run1",
                               status="failed", reason="boom")
        assert len(caplines) == 6

    async def test_the_real_tick_path_heartbeats_a_paused_run(
            self, caplines, fake_clock, monkeypatch):
        TestATerminalTickSaysWhy._stub(
            monkeypatch, {"status": "paused", "error_reason": None})
        await scheduler._run_skillflow_tick("p1", None)
        await scheduler._run_skillflow_tick("p1", None)
        assert len([l for l in caplines if "status=paused" in l]) == 1
        fake_clock.advance(scheduler._TICK_PAUSED_HEARTBEAT_S)
        await scheduler._run_skillflow_tick("p1", None)
        assert len([l for l in caplines if "status=paused" in l]) == 2
