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
