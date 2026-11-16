# core/loop_stack_watchdog.py
# An in-process witness for an event loop parked in a syscall.
#
# WHY THIS EXISTS
# Measured on the backend on 2026-11-16: the event loop stopped serving for
# 21.52 s and 17.99 s, and the retained log was silent across EVERY thread for
# the duration of the biggest stalls. The existing lag monitor
# (core/event_loop_lag.py) can report a block only AFTER the loop turns again —
# it runs ON that loop — and its `attribution` label stayed `unknown` for those
# samples. A stack sampled from outside would name the blocking frame, but
# py-spy is unavailable: the container's seccomp profile refuses ptrace. So the
# witness has to be in the process, and it has to cover ALL threads: a dump of
# the loop thread alone could miss what the loop was actually waiting for.
#
# WHAT IT IS
# `faulthandler` is stdlib and needs no ptrace and no seccomp allowance. The
# watchdog is a plain daemon THREAD that expects the event loop to publish a
# heartbeat at least once per `budget_s`. When the heartbeat goes silent for
# longer than the budget, the loop is not being turned, and the thread writes a
# file containing a header naming the measured silence plus
# `faulthandler.dump_traceback(all_threads=True)` — every thread's stack,
# including the parked thread's own frame.
#
# THE BOUNDARY, STATED SO IT IS NOT OVERCLAIMED
# The watchdog is Python code and needs the GIL to run. A block held INSIDE a C
# extension that does not release the GIL (a CPU-bound C loop, a long re/memcpy)
# produces NOTHING: the watchdog thread cannot run either, so there is no dump.
# What it covers is a loop parked in a syscall that RELEASES the GIL: fsync /
# fdatasync on the loop thread, a subprocess pipe read, a SQLite busy-wait that
# releases the GIL, a blocking socket read, `time.sleep`. It also cannot observe
# a block that begins before the first heartbeat is ever published.
#
# BOUNDS
#   * nothing is opened while healthy: the dump file is created only when a
#     dump is actually taken, so enabling the watchdog cannot grow data volume
#     when nothing is wrong;
#   * one file per incident, at most `max_dumps_per_incident` dumps, spaced
#     geometrically (budget, 2x, 4x ...) so a long stall is sampled across its
#     length instead of only at its start;
#   * further dumps stop once the file reaches `max_incident_bytes`;
#   * at most `max_files` incident files are kept (oldest pruned), so the
#     directory cannot grow without bound;
#   * every configured number is clamped, so no environment setting turns the
#     watchdog into a busy loop or an unbounded writer.
#
# It records current-source facts only: which thread's stack was where, while
# THIS process's loop was not beating. It makes no claim about production
# performance, and it does not attribute a cause it did not observe.

from __future__ import annotations

import asyncio
import faulthandler
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import datadir

logger = logging.getLogger("aitelier.loop_stack_watchdog")

# The environment switch. DEFAULT OFF: a deployment that sets nothing behaves
# exactly as it did before this module existed. It is opted into by the
# operator who needs the witness, because it writes files to the data volume.
SWITCH_ENV = "AITELIER_LOOP_STACK_DUMP"
BUDGET_ENV = "AITELIER_LOOP_STACK_DUMP_BUDGET_SECONDS"
CHECK_ENV = "AITELIER_LOOP_STACK_DUMP_CHECK_SECONDS"
MAX_DUMPS_ENV = "AITELIER_LOOP_STACK_DUMP_MAX_DUMPS"
MAX_BYTES_ENV = "AITELIER_LOOP_STACK_DUMP_MAX_BYTES"
MAX_FILES_ENV = "AITELIER_LOOP_STACK_DUMP_MAX_FILES"

DEFAULT_BUDGET_S = 0.5          # matches the lag monitor's 0.5 s threshold
DEFAULT_CHECK_S = 0.05
DEFAULT_MAX_DUMPS_PER_INCIDENT = 3
DEFAULT_MAX_INCIDENT_BYTES = 256 * 1024
DEFAULT_MAX_FILES = 20

# Clamps. A budget below this would fire on ordinary scheduler jitter; a check
# interval below it would be a busy loop.
_MIN_BUDGET_S = 0.05
_MAX_BUDGET_S = 300.0
_MIN_CHECK_S = 0.01
_MAX_CHECK_S = 5.0
_MAX_DUMPS = 100
_MIN_INCIDENT_BYTES = 1024
_MAX_INCIDENT_BYTES = 16 * 1024 * 1024
_MAX_FILES_CAP = 1000

WATCHDOG_THREAD_NAME = "aitelier-loop-stack-watchdog"
HEARTBEAT_TASK_NAME = "loop-stack-watchdog-heartbeat"
DUMP_DIR_NAME = "loop-stack-dumps"
_DUMP_GLOB = "loop-stack-*.log"
_HEADER_PREFIX = "# aitelier loop-stack-watchdog: watched loop silent for"
_FOOTER_PREFIX = "# end of aitelier loop-stack-watchdog dump"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_float(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(number, high))


def _bounded_int(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(number, high))


def enabled_from_env() -> bool:
    """True only for an explicit affirmative value; unset means OFF."""
    return os.getenv(SWITCH_ENV, "").strip().lower() in _TRUTHY


def dump_dir() -> Path:
    """Where incident files land: under the mounted data volume's logs/.

    core.datadir is the single authority for that root, so an AITELIER_HOME
    override (the test suite) redirects the dumps exactly like every other
    persistent path, and production resolves to <data volume>/logs.
    """
    return datadir.logs_dir() / DUMP_DIR_NAME


def config_from_env() -> dict:
    """The clamped configuration, read at call time (not import time)."""
    budget = _bounded_float(
        _env_float(BUDGET_ENV, DEFAULT_BUDGET_S),
        _MIN_BUDGET_S, _MAX_BUDGET_S, DEFAULT_BUDGET_S)
    check = _bounded_float(
        _env_float(CHECK_ENV, min(DEFAULT_CHECK_S, budget / 5.0)),
        _MIN_CHECK_S, _MAX_CHECK_S, DEFAULT_CHECK_S)
    # A check interval longer than the budget only delays the first dump past
    # the budget; it cannot make the witness wrong, but it does make it late.
    check = min(check, budget)
    return {
        "budget_s": budget,
        "check_interval_s": check,
        "max_dumps_per_incident": _bounded_int(
            _env_int(MAX_DUMPS_ENV, DEFAULT_MAX_DUMPS_PER_INCIDENT),
            1, _MAX_DUMPS, DEFAULT_MAX_DUMPS_PER_INCIDENT),
        "max_incident_bytes": _bounded_int(
            _env_int(MAX_BYTES_ENV, DEFAULT_MAX_INCIDENT_BYTES),
            _MIN_INCIDENT_BYTES, _MAX_INCIDENT_BYTES,
            DEFAULT_MAX_INCIDENT_BYTES),
        "max_files": _bounded_int(
            _env_int(MAX_FILES_ENV, DEFAULT_MAX_FILES),
            1, _MAX_FILES_CAP, DEFAULT_MAX_FILES),
    }


class _Incident:
    """Book-keeping for ONE stretch of silence (from first late check to beat)."""

    __slots__ = ("path", "dumps", "bytes", "next_due", "full")

    def __init__(self, budget_s: float):
        self.path: Optional[Path] = None
        self.dumps = 0
        self.bytes = 0
        self.next_due = budget_s
        self.full = False


class LoopStackWatchdog:
    """One daemon thread that dumps every stack while the loop is parked.

    The loop publishes `beat()` (through `start_from_env`, a small asyncio
    task). The thread wakes every `check_interval_s`; if the last beat is older
    than `budget_s`, the loop is not being turned and a dump is due.
    """

    def __init__(self, *,
                 dump_dir: Path,
                 budget_s: float = DEFAULT_BUDGET_S,
                 check_interval_s: float = DEFAULT_CHECK_S,
                 max_dumps_per_incident: int = DEFAULT_MAX_DUMPS_PER_INCIDENT,
                 max_incident_bytes: int = DEFAULT_MAX_INCIDENT_BYTES,
                 max_files: int = DEFAULT_MAX_FILES,
                 clock=time.monotonic):
        self.dump_dir = Path(dump_dir)
        self.budget_s = _bounded_float(
            budget_s, _MIN_BUDGET_S, _MAX_BUDGET_S, DEFAULT_BUDGET_S)
        self.check_interval_s = _bounded_float(
            check_interval_s, _MIN_CHECK_S, _MAX_CHECK_S, DEFAULT_CHECK_S)
        self.check_interval_s = min(self.check_interval_s, self.budget_s)
        self.max_dumps_per_incident = _bounded_int(
            max_dumps_per_incident, 1, _MAX_DUMPS,
            DEFAULT_MAX_DUMPS_PER_INCIDENT)
        self.max_incident_bytes = _bounded_int(
            max_incident_bytes, _MIN_INCIDENT_BYTES, _MAX_INCIDENT_BYTES,
            DEFAULT_MAX_INCIDENT_BYTES)
        self.max_files = _bounded_int(
            max_files, 1, _MAX_FILES_CAP, DEFAULT_MAX_FILES)
        self._clock = clock
        self._lock = threading.Lock()
        self._beat_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task: Optional["asyncio.Task"] = None
        self._incident: Optional[_Incident] = None
        self._incidents = 0
        self._write_disabled = False

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    def start(self, loop: Optional["asyncio.AbstractEventLoop"] = None
              ) -> "LoopStackWatchdog":
        """Start the watchdog thread, and the loop heartbeat if a loop is given.

        Idempotent while the thread is alive: a second start returns the same
        running watchdog instead of stacking threads.
        """
        if self.started:
            return self
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name=WATCHDOG_THREAD_NAME, daemon=True)
        self._thread = thread
        thread.start()
        if loop is not None:
            self._install_heartbeat(loop)
        return self

    def _install_heartbeat(self, loop: "asyncio.AbstractEventLoop") -> None:
        """Publish a beat from the loop; this is what goes silent on a block."""
        if self._task is not None and not self._task.done():
            return

        async def beat_forever() -> None:
            interval = self.check_interval_s
            while True:
                self.beat()
                await asyncio.sleep(interval)

        self._task = loop.create_task(beat_forever(), name=HEARTBEAT_TASK_NAME)

    def beat(self) -> None:
        """Record that the loop is being turned. Called ON the loop thread."""
        now = self._clock()
        with self._lock:
            self._beat_at = now

    def silent_seconds(self) -> Optional[float]:
        """How long the loop has been silent, or None before the first beat."""
        with self._lock:
            beat_at = self._beat_at
        if beat_at is None:
            return None
        return self._clock() - beat_at

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the heartbeat and the thread. Safe to call more than once."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The loop is already closed; the cancelled task can no
                # longer run, and the thread below is what must be joined.
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    # ── observation ─────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval_s):
            now = self._clock()
            with self._lock:
                beat_at = self._beat_at
            if beat_at is None:
                # No baseline yet: a loop that never beat has not been seen
                # running, so nothing here can call it blocked.
                continue
            blocked = now - beat_at
            if blocked < self.budget_s:
                self._incident = None
                continue
            incident = self._incident
            if incident is None:
                incident = _Incident(self.budget_s)
                self._incident = incident
            if incident.full or self._write_disabled:
                continue
            if blocked < incident.next_due:
                continue
            if incident.dumps >= self.max_dumps_per_incident:
                incident.full = True
                continue
            if incident.bytes >= self.max_incident_bytes:
                incident.full = True
                continue
            self._write_dump(incident, blocked)

    def _write_dump(self, incident: _Incident, blocked: float) -> None:
        """Write one header + all-thread stack dump, then update the bounds."""
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            if incident.path is None:
                self._incidents += 1
                stamp = datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ")
                incident.path = self.dump_dir / (
                    f"loop-stack-{stamp}-p{os.getpid()}-{self._incidents}.log")
                self._prune()
            header = (
                f"{_HEADER_PREFIX} {blocked:.3f}s (budget "
                f"{self.budget_s:.3f}s); dump {incident.dumps + 1}/"
                f"{self.max_dumps_per_incident}; pid {os.getpid()}; "
                f"{datetime.now(timezone.utc).isoformat()}\n").encode("utf-8")
            with open(incident.path, "ab") as handle:
                handle.write(header)
                # faulthandler writes straight to the fd, so the buffered
                # header must reach the fd before the traceback does.
                handle.flush()
                faulthandler.dump_traceback(file=handle, all_threads=True)
                handle.write(
                    f"{_FOOTER_PREFIX} {incident.dumps + 1}/"
                    f"{self.max_dumps_per_incident}\n".encode("utf-8"))
                handle.flush()
            # Prune AFTER the new file exists, so the retention cap counts it.
            self._prune()
            incident.dumps += 1
            incident.bytes = incident.path.stat().st_size
            incident.next_due = blocked * 2.0
            logger.warning(
                "event loop silent for %.3fs; wrote all-thread stack dump %s "
                "(dump %d/%d, %d bytes)",
                blocked, incident.path, incident.dumps,
                self.max_dumps_per_incident, incident.bytes)
        except (OSError, ValueError, RuntimeError):
            # A witness that raises into the loop or spins on a bad path would
            # be worse than no witness: declare it once and stop trying.
            logger.exception("loop-stack watchdog could not write a dump")
            self._write_disabled = True

    def _prune(self) -> None:
        """Keep at most `max_files` incident files, oldest removed first."""
        try:
            files = sorted(self.dump_dir.glob(_DUMP_GLOB))
        except OSError:
            return
        for old in files[:max(0, len(files) - self.max_files)]:
            try:
                old.unlink()
            except OSError:
                pass


# One watchdog per process. A second start while one is live returns it, so
# two app objects in one process cannot stack threads or interleave dumps.
_active: Optional[LoopStackWatchdog] = None
_active_lock = threading.Lock()


def current_watchdog() -> Optional[LoopStackWatchdog]:
    """The process's watchdog, or None when it was never armed / is stopped."""
    with _active_lock:
        return _active


def start_from_env(*, loop: Optional["asyncio.AbstractEventLoop"] = None
                   ) -> Optional[LoopStackWatchdog]:
    """Arm the watchdog when the env switch is on; otherwise do nothing.

    The production entrypoints call this from their lifespan, where the running
    loop is available; that loop is the one whose heartbeat is watched. Returns
    None when the switch is OFF (the default), so a deployment that sets
    nothing gets no thread, no file and no import-time side effect.
    """
    if not enabled_from_env():
        return None
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    global _active
    with _active_lock:
        existing = _active
        if existing is not None and existing.started:
            return existing
        config = config_from_env()
        watchdog = LoopStackWatchdog(dump_dir=dump_dir(), **config)
        _active = watchdog
    watchdog.start(loop=loop)
    logger.warning(
        "loop-stack watchdog armed: budget=%.3fs check=%.3fs, dumps under %s",
        watchdog.budget_s, watchdog.check_interval_s, watchdog.dump_dir)
    return watchdog


def shutdown(watchdog: Optional[LoopStackWatchdog]) -> None:
    """Disarm a watchdog returned by `start_from_env` (None is a no-op)."""
    if watchdog is None:
        return
    global _active
    watchdog.stop()
    with _active_lock:
        if _active is watchdog:
            _active = None
