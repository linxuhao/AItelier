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
