# core/event_loop_lag.py
# Bounded, direct event-loop lag observation.
#
# WHY THIS EXISTS
# The scheduler's control plane runs on the asyncio event loop. A synchronous
# tool step on the loop, a burst of in-process work, or a stalled transport all
# look identical from the outside: ticks stop advancing. The existing watchdog
# (`core.scheduler._check_hung_claims`) answers "is a CLAIM stale", and the
# reaper answers "is the OWNER process gone" — neither can see a loop that is
# simply not being turned, because both run ON that loop. This monitor measures
# the gap directly and attributes it to the one control-plane operation actually
# known to be in flight, or reports `unknown` when attribution is not known.
#
# BOUNDS
#   * one sampling task per scheduler lifecycle (see core.scheduler);
#   * the task is cancelled on scheduler shutdown, so it cannot outlive its
#     scheduler lifecycle; and it also ends ITSELF, on its own loop, when the
#     scheduler object is collected without a shutdown (a discarded lifecycle
#     loses its registry entry, so nothing can be asked to stop the task);
#   * observations live in a fixed-length deque (`max_observations`);
#   * logging is rate-limited (`log_cooldown_s`);
#   * interval and threshold are clamped, so no configuration turns loop jitter
#     into a storm of records.
#
# It records current-source facts only: each observation is the lag measured on
# THIS process's loop now. It makes no claim about production performance.

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import weakref
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("aitelier.event_loop_lag")

# Hard bounds. A monitor that could be configured into a hot loop or an
# unbounded buffer would not be a lightweight monitor.
_MIN_INTERVAL_S = 0.02
_MAX_INTERVAL_S = 60.0
_MIN_THRESHOLD_S = 0.02
_MAX_THRESHOLD_S = 300.0
_MAX_OBSERVATIONS = 1000
_MAX_LOG_COOLDOWN_S = 3600.0
# A label wider than this is truncated, so a pathological callback cannot grow
# the log line or the retained record without bound.
_MAX_ATTRIBUTION_BYTES = 200

_monotonic = time.monotonic


def _env_float(name: str, default: float) -> float:
    """Read a finite float from the environment, or `default`.

    A nan/inf value would otherwise poison every clamp it feeds (`min(nan, x)`
    is nan), so it is treated as absent and the default is used instead.
    """
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value, low: float, high: float, default: float) -> float:
    """A finite float clamped to [low, high]; non-finite input uses `default`."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(number, high))


def _bounded_int(value, low: int, high: int, default: int) -> int:
    """An int clamped to [low, high]; non-numeric input uses `default`."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(number, high))


DEFAULT_INTERVAL_S = _bounded_float(
    _env_float("AITELIER_LOOP_LAG_INTERVAL_SECONDS", 1.0),
    _MIN_INTERVAL_S, _MAX_INTERVAL_S, 1.0)
DEFAULT_THRESHOLD_S = _bounded_float(
    _env_float("AITELIER_LOOP_LAG_THRESHOLD_SECONDS", 0.5),
    _MIN_THRESHOLD_S, _MAX_THRESHOLD_S, 0.5)
DEFAULT_MAX_OBSERVATIONS = _bounded_int(
    _env_int("AITELIER_LOOP_LAG_MAX_OBSERVATIONS", 100),
    1, _MAX_OBSERVATIONS, 100)
DEFAULT_LOG_COOLDOWN_S = _bounded_float(
    _env_float("AITELIER_LOOP_LAG_LOG_COOLDOWN_SECONDS", 30.0),
    0.0, _MAX_LOG_COOLDOWN_S, 30.0)


class EventLoopLagMonitor:
    """One bounded lag sampler for one scheduler lifecycle.

    A single asyncio task sleeps for `interval_s` on the loop and compares the
    wall elapsed time with the requested interval. The excess IS the lag: a
    synchronous block inside any callback delays this sleep's wakeup by exactly
    the time the loop spent not turning. No profiler, no hook, no monkeypatch.
    """

    def __init__(self, *, interval_s: float = DEFAULT_INTERVAL_S,
                 threshold_s: float = DEFAULT_THRESHOLD_S,
                 max_observations: int = DEFAULT_MAX_OBSERVATIONS,
                 log_cooldown_s: float = DEFAULT_LOG_COOLDOWN_S,
                 attribution: Optional[Callable[[], Optional[str]]] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.interval_s = _bounded_float(
            interval_s, _MIN_INTERVAL_S, _MAX_INTERVAL_S, DEFAULT_INTERVAL_S)
        self.threshold_s = _bounded_float(
            threshold_s, _MIN_THRESHOLD_S, _MAX_THRESHOLD_S,
            DEFAULT_THRESHOLD_S)
        self.max_observations = _bounded_int(
            max_observations, 1, _MAX_OBSERVATIONS, DEFAULT_MAX_OBSERVATIONS)
        self.log_cooldown_s = _bounded_float(
            log_cooldown_s, 0.0, _MAX_LOG_COOLDOWN_S, DEFAULT_LOG_COOLDOWN_S)
        self._attribution = attribution
        self._clock = clock or _monotonic
        self._observations: deque = deque(maxlen=self.max_observations)
        self._task: "asyncio.Task | None" = None
        self._last_log_at: Optional[float] = None
        # A WEAK reference to the lifecycle that owns this monitor. Strong
        # would pin a discarded scheduler; a reaper thread or a registry of
        # dead owners would be a second lifecycle to get wrong. See
        # `bind_owner` and the owner check in `_run`.
        self._owner_ref: "weakref.ref | None" = None

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        """True only while the sampling task is actually live.

        Deliberately not a sticky flag: a task that exited can no longer be
        reported as a running monitor, so the registry can replace it.
        """
        task = self._task
        return task is not None and not task.done()

    @property
    def task(self) -> "asyncio.Task | None":
        return self._task

    def start(self) -> "EventLoopLagMonitor":
        """Create the sampling task on the running loop (idempotent while live).

        Idempotent only while the task is live: if the previous task has
        finished, a new one is created, so an unexpected exit cannot leave the
        monitor silently dead.
        """
        if self._task is not None and not self._task.done():
            return self
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync CLI startup with no loop turned yet: leave the monitor
            # unattached so a later call can start it. Never raise into startup.
            logger.debug("event-loop lag monitor not started: no running loop")
            return self
        task = loop.create_task(self._run(), name="event-loop-lag-monitor")
        self._task = task
        task.add_done_callback(self._on_task_done)
        return self

    def _on_task_done(self, task: "asyncio.Task") -> None:
        """Drop the task reference as soon as it finishes.

        Keeps `started` honest after an unexpected exit and lets the registry
        replace a dead monitor instead of handing back a stopped one.
        """
        if self._task is task:
            self._task = None

    def bind_owner(self, owner) -> "EventLoopLagMonitor":
        """Hold a WEAK reference to the lifecycle that owns this monitor.

        Teardown is tied to the actual owner, in both directions. A shutdown
        event still cancels and awaits the task explicitly (see
        `core.scheduler._stop_lag_monitor`); owner LOSS has no event and no
        object left to ask — the registry entry disappears with the key, so
        the sampler itself must notice, on its own next wake, that its owner
        is gone and settle on the loop that owns it. Bounded: at most one
        interval past the owner's death. No reaper thread, no dead-owner
        registry, and no strong reference that would pin a discarded owner.
        """
        if owner is None:
            return self
        try:
            self._owner_ref = weakref.ref(owner)
        except TypeError:
            # start_monitor rejects a non-weakref-able owner at the registry
            # before this is ever reached; leave the monitor unbound anyway.
            self._owner_ref = None
        return self

    async def _run(self) -> None:
        interval = self.interval_s
        clock = self._clock
        owner_ref = self._owner_ref
        while True:
            before = clock()
            await asyncio.sleep(interval)
            if owner_ref is not None and owner_ref() is None:
                # The scheduler lifecycle that owned this monitor is gone, and
                # with it the registry entry that let anyone ask for a stop.
                # Settle here, on the loop that owns the task, rather than
                # sampling forever for a scheduler nobody can reach.
                return
            after = clock()
            lag = (after - before) - interval
            if lag >= self.threshold_s:
                self._record(lag)

    def cancel(self) -> int:
        """Cancel the sampling task from anywhere. Returns 1 if one was live."""
        task = self._task
        if task is None or task.done():
            return 0
        loop = task.get_loop()
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            task.cancel()
        else:
            loop.call_soon_threadsafe(task.cancel)
        return 1

    async def aclose(self) -> None:
        """Cancel and AWAIT the task so the lifecycle leaves nothing behind."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:                                          # noqa: BLE001
            pass

    # ── observation ─────────────────────────────────────────────────────

    def snapshot(self) -> list:
        """A copy of the retained observations, oldest first."""
        return list(self._observations)

    def _record(self, lag: float) -> None:
        attribution = self._attribute()
        self._observations.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "lag_s": round(lag, 3),
            "threshold_s": self.threshold_s,
            "attribution": attribution,
        })
        self._maybe_log(lag, attribution)

    def _attribute(self) -> str:
        """The active-context label, or `unknown`.

        Only a non-empty string is a usable label; anything else (a list, a
        task, a private object) is reported as `unknown` rather than rendered.
        The label is truncated to a byte budget so a pathological callback
        cannot grow the log line or the retained record without bound. This is
        the context active when the lag was sampled, not a proven cause.
        """
        if self._attribution is None:
            return "unknown"
        try:
            value = self._attribution()
        except Exception:                                          # noqa: BLE001
            return "unknown"
        if not isinstance(value, str):
            return "unknown"
        value = value.strip()
        if not value:
            return "unknown"
        return value.encode("utf-8")[:_MAX_ATTRIBUTION_BYTES].decode(
            "utf-8", "ignore")

    def _maybe_log(self, lag: float, attribution: str) -> None:
        now = self._clock()
        if self._last_log_at is not None and \
                now - self._last_log_at < self.log_cooldown_s:
            return
        self._last_log_at = now
        logger.warning(
            "event-loop lag %.2fs >= threshold %.2fs (attribution=%s); "
            "control-plane work was not being turned on this loop",
            lag, self.threshold_s, attribution)


# One monitor per scheduler lifecycle. Weak keys, so a discarded scheduler and
# its monitor are collected together and the registry cannot grow without bound.
_monitors: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def monitor_for(scheduler) -> Optional[EventLoopLagMonitor]:
    """The lifecycle's monitor, or None when none was ever attached."""
    try:
        return _monitors.get(scheduler)
    except TypeError:
        return None


def start_monitor(scheduler, *,
                  attribution: Optional[Callable[[], Optional[str]]] = None,
                  **kwargs) -> EventLoopLagMonitor:
    """Attach (once) and start the monitor bound to this scheduler lifecycle.

    The registry tracks the *live* task, not a sticky started flag: if the
    recorded monitor's task has already finished (an unexpected return, or a
    cancellation nobody awaited), a fresh monitor replaces it so the next
    lifecycle still observes lag. An owner that cannot be weakly referenced is
    rejected *before* any task is created, so no untracked sampling task can be
    started.
    """
    existing = monitor_for(scheduler)
    if existing is not None and existing.started:
        return existing
    monitor = EventLoopLagMonitor(attribution=attribution, **kwargs)
    # A non-weakref-able owner raises TypeError here, before `start()` below:
    # the rejection happens before a task exists, so nothing is left untracked.
    _monitors[scheduler] = monitor
    # Tie the sampler's lifetime to the ACTUAL owner. A scheduler that is
    # collected without a shutdown loses its registry entry, so nothing can be
    # asked to stop its task; the task must notice and settle itself. Only a
    # weak reference is kept — no pinning, no reaper, no dead-owner registry.
    monitor.bind_owner(scheduler)
    monitor.start()
    return monitor


def stop_monitor(scheduler) -> int:
    """Cancel the lifecycle's monitor. Returns how many tasks it cancelled."""
    monitor = monitor_for(scheduler)
    if monitor is None:
        return 0
    return monitor.cancel()
