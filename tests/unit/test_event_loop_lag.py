"""Bounded event-loop lag observation: causal, owned, CPU-only.

The scheduler's control plane runs on one asyncio event loop. `_check_hung_claims`
answers "is a CLAIM stale", but it runs *on* that loop, so a loop that is not
turning cannot be reported by it. `core.event_loop_lag` measures the gap
directly: a sleep scheduled for `interval_s` wakes `lag_s` late, and that excess
IS the time the loop spent blocked. These tests drive the real monitor and a
real scheduler lifecycle:

  * an actually-delayed loop emits an observation (the delay is real `time.sleep`
    on the loop, not a mocked clock);
  * a healthy loop over many intervals does not emit;
  * logging is rate-limited and records carry a bounded attribution;
  * retention and configuration are clamped, including nan/inf;
  * only a weakref-able owner is accepted, and an unsupported owner is rejected
    before any sampling task is created;
  * stop/restart of a scheduler lifecycle leaves no sampling task behind — also
    when shutdown is issued from another thread — and does not stack monitors or
    shutdown listeners;
  * an owner that is simply collected, with no shutdown event and no object left
    to ask, loses its sampler too: the task settles itself on its own loop
    (checked against the real AsyncIOScheduler owner type, not only a stand-in).

They are CPU-only and make no claim about production performance: they record
what the current source does on a synthetic delayed loop.
"""

import asyncio
import gc
import logging
import math
import threading
import time
import weakref

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core import event_loop_lag as ell
from core import scheduler as sc


class _Owner:
    """A weakref-able stand-in for a scheduler lifecycle owner."""


class _NotWeakrefable:
    """A type that refuses weak references (`object()` is one)."""

    __slots__ = ()


def _lag_tasks():
    return [t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
            and t.get_name() == "event-loop-lag-monitor"]


@pytest.fixture
def clean_monitors(monkeypatch):
    monkeypatch.setattr(ell, "_monitors", weakref.WeakKeyDictionary())
    monkeypatch.setattr(sc, "_lag_monitor_listener_on", weakref.WeakSet())
    monkeypatch.setattr(sc, "_shutdown_listener_on", weakref.WeakSet())



# ── criterion: a genuinely delayed loop emits an observation ────────────────


@pytest.mark.asyncio
async def test_a_delayed_loop_emits_a_lag_observation(clean_monitors):
    """Block the loop and prove the monitor sees the real delay.

    The blocking is a real `time.sleep` executed as a coroutine on the loop, so
    the monitor's own sleep is genuinely late — no synthetic clock.
    """
    key = _Owner()
    monitor = ell.start_monitor(key, interval_s=0.05, threshold_s=0.1,
                                max_observations=10)
    await asyncio.sleep(0.08)       # let the monitor complete one clean sample


    async def block_the_loop():
        time.sleep(0.35)            # synchronous: the loop cannot turn

    await block_the_loop()
    await asyncio.sleep(0.15)       # let the delayed wakeup be recorded

    observations = monitor.snapshot()
    print(f"\nobservations after a 0.35s loop block: {observations}")
    assert observations, "a 0.35s loop block produced no lag observation"
    assert observations[-1]["lag_s"] >= monitor.threshold_s
    assert observations[-1]["attribution"] == "unknown"

    await monitor.aclose()


# ── criterion: steady state does not spam ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_healthy_loop_emits_no_observation(clean_monitors):
    """Many idle intervals, no block: the threshold is not tripped."""
    key = _Owner()
    monitor = ell.start_monitor(key, interval_s=0.01, threshold_s=1.0,
                                max_observations=100)
    await asyncio.sleep(0.3)
    print(f"\nobservations over ~30 healthy intervals: {monitor.snapshot()}")
    assert monitor.snapshot() == []
    await monitor.aclose()


# ── criterion: bounded retention and clamped configuration ──────────────────


def test_retention_is_bounded():
    clock = [0.0]
    monitor = ell.EventLoopLagMonitor(interval_s=1.0, threshold_s=1.0,
                                      max_observations=3,
                                      clock=lambda: clock[0])
    for i in range(10):
        clock[0] += 100.0
        monitor._record(float(i))
    assert len(monitor.snapshot()) == 3, "retention grew past max_observations"
    assert [o["lag_s"] for o in monitor.snapshot()] == [7.0, 8.0, 9.0]


def test_configuration_is_clamped():
    monitor = ell.EventLoopLagMonitor(interval_s=-5.0, threshold_s=-1.0,
                                      max_observations=10_000_000,
                                      log_cooldown_s=-3.0)
    assert monitor.interval_s >= ell._MIN_INTERVAL_S
    assert monitor.threshold_s >= ell._MIN_THRESHOLD_S
    assert monitor.max_observations <= ell._MAX_OBSERVATIONS
    assert monitor.log_cooldown_s >= 0.0


def test_non_finite_configuration_falls_back_to_defaults():
    """A nan/inf value must not survive a clamp as nan; it uses the default.

    `min(nan, x)` is nan, so a nan passed straight through the old clamps would
    have produced a monitor whose threshold never trips and whose log cooldown
    never elapses. The constructor must reject the non-finite value instead.
    """
    monitor = ell.EventLoopLagMonitor(interval_s=float("nan"),
                                      threshold_s=float("inf"),
                                      log_cooldown_s=float("nan"))
    assert math.isfinite(monitor.interval_s)
    assert math.isfinite(monitor.threshold_s)
    assert math.isfinite(monitor.log_cooldown_s)
    assert ell._MIN_INTERVAL_S <= monitor.interval_s <= ell._MAX_INTERVAL_S
    assert ell._MIN_THRESHOLD_S <= monitor.threshold_s <= ell._MAX_THRESHOLD_S
    assert 0.0 <= monitor.log_cooldown_s <= ell._MAX_LOG_COOLDOWN_S


# ── criterion: rate-limited logging; attribution is named or unknown ────────


def test_logging_is_rate_limited(caplog):
    clock = [0.0]
    monitor = ell.EventLoopLagMonitor(interval_s=1.0, threshold_s=1.0,
                                      max_observations=100,
                                      log_cooldown_s=30.0,
                                      clock=lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="aitelier.event_loop_lag"):
        for _ in range(4):
            clock[0] += 1.0        # all within the 30 s cooldown
            monitor._record(2.0)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    print(f"\nwarnings for 4 records within one cooldown: {len(warns)}")
    assert len(monitor.snapshot()) == 4
    assert len(warns) == 1, "log cooldown did not suppress repeated warnings"



@pytest.mark.asyncio
async def test_attribution_names_live_context_and_filters_completed(monkeypatch):
    """Live in-flight ticks are named; completed ticks are not; else unknown."""
    monkeypatch.setattr(sc, "_detached_ticks", {})
    assert sc._current_operation_attribution() == "unknown"

    async def _never():
        await asyncio.sleep(600)

    async def _noop():
        return None

    live = asyncio.ensure_future(_never())
    finished = asyncio.ensure_future(_noop())
    await asyncio.sleep(0)                 # let `finished` complete
    try:
        monkeypatch.setattr(sc, "_detached_ticks",
                            {"proj-a": live, "proj-b": finished})
        # The finished tick is dropped; only the genuinely live one is named.
        assert sc._current_operation_attribution() == "tick:proj-a"
    finally:
        live.cancel()
        try:
            await live
        except asyncio.CancelledError:
            pass


def test_attribution_reports_unknown_for_odd_values():
    """A non-string, empty, raising or oversized attribution is bounded safely."""
    def _raises():
        raise RuntimeError("boom")
    assert ell.EventLoopLagMonitor(attribution=_raises)._attribute() == "unknown"
    assert ell.EventLoopLagMonitor(attribution=lambda: None)._attribute() == "unknown"
    assert ell.EventLoopLagMonitor(attribution=lambda: "   ")._attribute() == "unknown"
    # Not a string: never rendered, so no private object can leak into a record.
    assert ell.EventLoopLagMonitor(attribution=lambda: object())._attribute() \
        == "unknown"
    huge = "x" * (ell._MAX_ATTRIBUTION_BYTES * 4)
    bounded = ell.EventLoopLagMonitor(attribution=lambda: huge)._attribute()
    assert len(bounded.encode("utf-8")) <= ell._MAX_ATTRIBUTION_BYTES


# ── criterion: lifecycle stop/restart leaves no monitor task behind ─────────


@pytest.mark.asyncio
async def test_shutdown_cancels_the_monitor_and_rescheduling_does_not_stack(
        clean_monitors):
    settings = {"scheduler_type": "interval", "scheduler_interval": 60}
    sched = AsyncIOScheduler()
    sc._add_scheduler_job(sched, settings)
    sched.start()
    try:
        # Rescheduling re-runs _add_scheduler_job on the SAME scheduler.
        for _ in range(3):
            sc.reschedule_scheduler(sched, settings)
        monitor = ell.monitor_for(sched)
        assert monitor is not None and monitor.started
        assert monitor._task is not None
        # The watchdog job and its cadence are untouched by this integration.
        hung = [j for j in sched.get_jobs() if j.name == "_check_hung_claims"]
        assert len(hung) == 1
    finally:
        # stop_scheduler also tears the monitor down; here we shut this
        # standalone scheduler by hand (no module instance was registered).
        sc._stop_lag_monitor(sched)
        sched.shutdown(wait=False)
        deadline = time.monotonic() + 5
        while (sched.running or _lag_tasks()) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
    assert not sched.running, "scheduler never finished shutting down"
    assert monitor.task is None, "monitor task survived shutdown"
    assert not _lag_tasks(), "a lag sampling task leaked past lifecycle shutdown"



@pytest.mark.asyncio
async def test_start_monitor_is_once_per_lifecycle(clean_monitors):
    key = _Owner()
    first = ell.start_monitor(key, interval_s=1.0, threshold_s=1.0)
    second = ell.start_monitor(key, interval_s=1.0, threshold_s=1.0)
    assert first is second
    await first.aclose()


@pytest.mark.asyncio
async def test_a_discarded_owner_settles_its_own_sampler(clean_monitors):
    """Owner LOSS, not just shutdown, must end the sampler.

    The registry is weak, so once the scheduler object is collected
    `monitor_for` answers None and nothing can be ASKED to stop the task. The
    task therefore has to notice, on its own next wake, that its owner is gone
    and settle on the loop that owns it — at most one interval later. A strong
    owner reference (a monitor pinning its scheduler) or a global reaper would
    both be wrong; this is the control that says neither is needed.
    """
    owner = _Owner()
    monitor = ell.start_monitor(owner, interval_s=0.02, threshold_s=1.0)
    task = monitor.task
    assert task is not None and not task.done(), "the sampler never started"
    del owner
    deadline = time.monotonic() + 5
    while len(ell._monitors) and time.monotonic() < deadline:
        gc.collect()
        await asyncio.sleep(0.01)
    assert len(ell._monitors) == 0, "a discarded owner still holds a registry entry"
    while not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert task.done(), "the sampler kept running for an owner nobody holds"
    # The sampler settles on its own loop. The done callback that clears the
    # task pointer is queued for the next turn, so wait (bounded) for that
    # pointer to clear instead of asserting on the very turn the captured task
    # reports done.
    while monitor.task is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert monitor.task is None, "a settled task still looked live"

    assert not monitor.started
    assert not _lag_tasks(), "a lag sampling task leaked past owner collection"


@pytest.mark.asyncio
async def test_a_discarded_real_scheduler_settles_its_sampler(clean_monitors):
    """The same owner-loss case against the REAL owner type.

    An actual AsyncIOScheduler lifecycle, dropped without `shutdown()` and
    without `stop_scheduler`: the weak registry empties, and the live sampling
    task must not survive it.
    """
    sched = AsyncIOScheduler()
    monitor = ell.start_monitor(sched, interval_s=0.02, threshold_s=1.0)
    task = monitor.task
    assert monitor.started and task is not None
    del sched
    deadline = time.monotonic() + 5
    while len(ell._monitors) and time.monotonic() < deadline:
        gc.collect()
        await asyncio.sleep(0.01)
    assert len(ell._monitors) == 0, "a collected scheduler still holds a monitor"
    while not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert task.done(), "the live sampler survived its collected owner"
    while monitor.task is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert monitor.task is None and not monitor.started

    assert not _lag_tasks(), "a lag sampling task leaked past owner collection"


def test_unsupported_owner_is_rejected_before_any_task(clean_monitors):
    """A non-weakref-able owner is rejected; no orphan sampling task is started.

    Negative control paired with the weakref-able `_Owner` used above. `object()`
    is the canonical non-weakref-able value; `_NotWeakrefable` documents the same
    shape for a class.
    """
    for owner in (object(), _NotWeakrefable()):
        with pytest.raises(TypeError):
            ell.start_monitor(owner, interval_s=1.0, threshold_s=1.0)
    # The rejection happened before any task existed, so nothing is registered
    # and nothing untracked is left running.
    assert len(ell._monitors) == 0


@pytest.mark.asyncio
async def test_shutdown_from_another_thread_still_awaits_the_monitor(clean_monitors):
    """Off-thread shutdown tears the monitor down on its own loop."""
    settings = {"scheduler_type": "interval", "scheduler_interval": 60}
    sched = AsyncIOScheduler()
    sc._add_scheduler_job(sched, settings)
    sched.start()
    monitor = ell.monitor_for(sched)
    assert monitor is not None and monitor.started
    try:
        # Issued from a plain thread, exactly as a cross-thread stop would be.
        result = {}

        def _off_thread_stop():
            result["cancelled"] = sc._stop_lag_monitor(sched)

        worker = threading.Thread(target=_off_thread_stop)
        worker.start()
        worker.join(timeout=5)
        deadline = time.monotonic() + 5
        while (monitor.task is not None or _lag_tasks()) \
                and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert result.get("cancelled") == 1
        assert monitor.task is None, "monitor task survived an off-thread stop"
        assert not _lag_tasks(), "a lag sampling task leaked past off-thread stop"
    finally:
        sched.shutdown(wait=False)
        deadline = time.monotonic() + 5
        while sched.running and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
