"""The lifecycle row stamps: off the event loop, witnessed one call site at a time.

`DshDrivers.update(...)` is synchronous sqlite on purpose, and it is PATIENT - up
to `STAMP_LOCK_ATTEMPTS` fresh connections, each spending a full busy timeout
(~15.4 s in total, see `core.dsh_driver`). Awaiting it directly from `start`,
`_observe` and `stop` therefore parked the whole event loop on a held lock: the
driver runtime's own monitor tasks, every other project's lifecycle and every
request in the process stopped for as long as the writer ahead of it took. The
activity route already made this call form `await asyncio.to_thread(...)`.

What these arms measure is the same one thing for each lifecycle stamp: does the
event loop keep running while the stamp waits out a real write lock? Each arm
holds a REAL `BEGIN IMMEDIATE` write lock on the same database file for 0.8 s
from a second thread - exactly the shape `update` is patient about - and counts
how often a 5 ms counter coroutine got the loop during the call.

TWO numbers per arm, and the second one is the interesting one:

  * `ticks >= MIN_TICKS` - the loop kept getting the thread overall (~150-370
    ticks on this change, 1-2 on the base revision).
  * `ticks in the lock's last 0.4 s >= MIN_CONTENDED_TICKS` - the loop kept
    getting the thread while the stamp was CERTAINLY waiting. This is what makes
    each arm sensitive to a SINGLE reverted call site: one synchronous stamp
    parks the loop until the foreign lock is released, so it produces no tick in
    that tail at all, while the rest of the call can still look healthy.

Why the tail and not a maximum-freeze threshold: a shared host schedules this
process away for hundreds of milliseconds at a time. Measured here on the
delivered tree, in otherwise green runs: single gaps of 0.40 s and 1.75 s with
the loop provably alive on both sides (the second one inside the `begin` of a
sibling thread, not inside any stamp). Any freeze ceiling low enough to catch a
parked loop therefore also catches the host. The tail count is immune to that,
because it is anchored to the LOCK's own release instead of to the host's
schedule: whatever the loop did before, a stamp that parks it is still parked
when the holder commits.

One lock, one stamp. A foreign holder can only be contended with by the FIRST
write that arrives while it holds, so each arm arranges for exactly one stamp to
be the waiter:

  * `test_start_...`          -> the `phase="starting"` stamp in `start`.
  * `test_start_failure_...`  -> the `phase="failed"` stamp in `start`'s
                                 `except (OSError, ValueError)` handler, reached
                                 by making `_environment` fail (it runs BEFORE the
                                 `starting` stamp, so that stamp is never issued).
  * `test_observe_...`        -> the `phase="failed"` stamp in `_observe`.
  * `test_stop_...`           -> the `phase="stopping"` stamp in `stop`.
  * `test_stop_final_...`     -> the final `phase="stopped"` stamp in `stop`; the
                                 lock is taken by the reaping stand-in, because by
                                 then the holder for the `stopping` stamp is gone.

HOST MODE - the three external-host stamps. `DshDrivers.external_host` is the
branch selector: when the host controller is not this process, the driver is
run by the host iff `<root>/runtime.json` says `{"mode": "host"}`. The three arms
below write that file, assert the selector, and then run the REAL branch - no
monkeypatched `update`, no monkeypatched `external_host`, and no host process:
the host's process marker (`host-process.json`) is simply never created, except
where an arm creates it on purpose.

  * `test_start_external_...`       -> the `enabled=1, phase="starting"` stamp in
                                       `start`'s external-host branch.
  * `test_stop_external_...`        -> the `phase="stopping"` stamp in `stop`'s
                                       external-host branch.
  * `test_stop_external_final_...`  -> the final `phase="stopped"` stamp in the same
                                       branch. Its mechanism is spelled out in its
                                       own docstring: with the marker absent, `stop`
                                       issues BOTH stamps back-to-back, so a lock
                                       held across the call only contends the first.

The RED runs live outside this file (they are runs of this file against a scratch
copy of `core/dsh_driver.py`, with exactly one call site put back to the
synchronous `self.update(...)`). On the base revision every arm here goes red with
a nearly frozen counter, and a mutant that puts back exactly one call site reddens
exactly the arm named above while the others stay green.
"""
import asyncio
import json
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager

import pytest

from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_database import StateDatabase
from core.state_service import StateService

# The numbers this file is about, named so every measured figure is quotable.
LOOP_TICK_SECONDS = 0.005        # the counter coroutine's period
BLOCKING_WRITE_SECONDS = 0.8     # how long each foreign write lock is held
MIN_TICKS = 25                   # whole-call floor; a parked loop measures 0-few
CONTENDED_TAIL_SECONDS = 0.4     # the stretch of the hold whose ticks are counted
MIN_CONTENDED_TICKS = 5          # tail floor; a stamp on the loop measures exactly 0


@pytest.fixture
def runtime(tmp_path):
    """The production shape: real State storage, real identity registry, sleeping child.

    Nothing here is a stand-in for the thing under test: `update`, `_provision`,
    `start`, `_observe`, `stop`, the sqlite file and its WAL journal are the real
    ones. The only substitution is the child process (a `sleep`), which the
    lifecycle paths do not care about - they stamp the row either way.
    """
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.store.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "dsh-lifecycle-stamp-pepper-" * 3)
    registry.seed("fixture-owner-token")
    return DshDrivers(db, registry, tmp_path / "drivers",
                      command=[sys.executable, "-c", "import time; time.sleep(90)"])


@asynccontextmanager
async def running_counter():
    """A 5 ms counter on the loop, with both edges of the window pinned.

    Yields the list of tick timestamps taken while the block runs. TWO pins:

      * the prime guarantees a tick immediately BEFORE whatever the block does,
        so the whole-call count starts from the window's first millisecond;
      * the closing boundary appended on exit guarantees a timestamp AFTER the
        block, so the printed longest-freeze figure still sees a park at the END
        of the window (where a lifecycle call's last stamp sits). It is not
        asserted - see the module docstring on why a freeze ceiling cannot be
        told apart from the host's own scheduling - but it is the number that
        makes a failure easy to read.
    """
    times = []
    stop = asyncio.Event()
    primed = asyncio.Event()

    async def counter():
        while not stop.is_set():
            times.append(time.monotonic())
            primed.set()
            await asyncio.sleep(LOOP_TICK_SECONDS)

    task = asyncio.create_task(counter())
    await primed.wait()
    try:
        yield times
    finally:
        times.append(time.monotonic())
        stop.set()
        await task


def longest_freeze(times):
    """The longest stretch, in seconds, during which the counter could not run."""
    return max((later - earlier for earlier, later in zip(times, times[1:])), default=0.0)


def ticks_while_certainly_waiting(times, lock):
    """How often the loop ran during the last stretch of the foreign hold.

    `lock.released_at` is stamped by the holder thread after its commit, so the
    interval ends the instant the write lock became available - and a stamp that
    runs ON the loop is still parked at that instant, because it has been waiting
    for exactly this. Ticks in this interval therefore mean the stamp was
    somewhere else (a thread), not that the host was generous.
    """
    floor = lock.released_at - CONTENDED_TAIL_SECONDS
    return sum(1 for taken_at in times if floor <= taken_at <= lock.released_at)


def assert_loop_kept_running(times, lock, where):
    """The witness: the loop kept getting the thread, overall and while waiting."""
    ticks = len(times) - 1  # the closing boundary is not a tick
    tail = ticks_while_certainly_waiting(times, lock)
    print(f"[{where}] ticks during the call: {ticks} (floor {MIN_TICKS}); ticks while the "
          f"lock was certainly held out: {tail} in its last {CONTENDED_TAIL_SECONDS}s "
          f"(floor {MIN_CONTENDED_TICKS}); longest freeze: {longest_freeze(times):.3f}s")
    assert ticks >= MIN_TICKS, (
        f"{where}: the event loop was parked for the whole call - the {LOOP_TICK_SECONDS}s "
        f"counter advanced only {ticks} times across a {BLOCKING_WRITE_SECONDS}s held write "
        f"lock (floor {MIN_TICKS})")
    assert tail >= MIN_CONTENDED_TICKS, (
        f"{where}: the loop ran only {tail} times in the last {CONTENDED_TAIL_SECONDS}s "
        f"before the foreign write lock was released, i.e. while the row stamp was "
        f"certainly waiting for that lock (floor {MIN_CONTENDED_TICKS}): that stamp is "
        f"synchronous on the event loop")


class _ForeignWriteLock:
    """Another connection holds a REAL write lock on the database, for `hold` s.

    `BEGIN IMMEDIATE` is what takes the write lock in WAL mode, and the UPDATE
    under it touches the same row the stamps write, so a stamp issued meanwhile
    is contending for exactly the lock a concurrent writer would hold.

    `__enter__` blocks the calling thread until the lock is really held (a few
    ms), `__exit__` releases and joins the holder thread - so both belong OUTSIDE
    a measured window, which is where every arm puts them. `release()` is a plain
    Event set and may be called from the loop; it exists so the second window in
    `test_stop_final_...` can be given back while the stamp is already waiting.
    """

    def __init__(self, db_path, project_id, hold):
        self.db_path, self.project_id, self.hold = db_path, project_id, hold
        self.held = threading.Event()
        self.thread = None
        self.released_at = None   # when the commit made it available again
        self._released = threading.Event()

    def _hold_the_lock(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE dsh_drivers SET phase='lockholder' WHERE project_id=?",
                         (self.project_id,))
            self.held.set()
            self._released.wait(self.hold)
            conn.commit()
            self.released_at = time.monotonic()
        finally:
            conn.close()

    def __enter__(self):
        self.thread = threading.Thread(target=self._hold_the_lock, daemon=True)
        self.thread.start()
        assert self.held.wait(10), "the foreign connection never acquired the write lock"
        return self

    def release(self):
        self._released.set()

    def __exit__(self, *exc_info):
        self.release()
        self.thread.join(self.hold + 15)
        return False


class _ReapingUnderLock:
    """The child process stand-in whose reaping takes its own foreign write lock.

    `stop`'s FINAL stamp cannot be reached by a lock held from outside the call:
    the holder that parks the `phase="stopping"` stamp is released long before the
    process is reaped. So the second window is taken here, in `wait()`, and given
    back `hold` seconds AFTER `wait()` returns - which is precisely while `stop`
    is issuing the final `phase="stopped"` stamp. Releasing is a plain Event set
    from a loop callback, so the callback itself never blocks the loop it runs on.
    """

    def __init__(self, proc, db_path, project_id, hold):
        self._proc = proc
        self.window = _ForeignWriteLock(db_path, project_id, hold)
        self._hold = hold
        self.pid = proc.pid
        self.returncode = None  # `stop` only signals a proc it believes is alive

    async def wait(self):
        # ACQUIRING the window runs on a thread, not on the loop. `__enter__`
        # waits for a real OS thread to be scheduled and to take the write lock;
        # on a loaded host that is seconds, and measured on the loop it would be
        # indistinguishable from a stamp that parks it. The window this arm is
        # about is the one HELD across `stop`'s final stamp - nothing else here
        # may cost the loop time.
        await asyncio.to_thread(self.window.__enter__)
        loop = asyncio.get_running_loop()
        try:
            return await self._proc.wait()
        finally:
            loop.call_later(self._hold, self.window.release)

    async def join(self):
        """Let the window close and the holder thread finish, without blocking the loop."""
        self.window.release()
        while self.window.thread.is_alive():
            await asyncio.sleep(LOOP_TICK_SECONDS)


def provisioned(runtime, project_id="alpha"):
    """Create the driver row BEFORE a window opens.

    `_provision` INSERTs the row the first time and returns the existing one
    afterwards. Doing it here means the measured window contains only the STAMPS
    under test: an INSERT waiting for the foreign lock would be a different
    (equally real, but not-this-issue) blocking write, and it would blur which
    call site the arm is about.
    """
    return runtime._provision(project_id, "aitelier", "driver", "fixture")


def host_mode(runtime):
    """Select the external-host branch the way production does: `runtime.json`.

    `DshDrivers.external_host` is true when no in-process host controller is set
    and `<root>/runtime.json` reads `{"mode": "host"}`. Writing that file is the
    only switch: the branch is then entered for real, with no host process behind
    it. The assertion keeps a future change to the selector from quietly turning
    these arms back into the process-mode ones.
    """
    runtime.root.mkdir(parents=True, exist_ok=True)
    (runtime.root / "runtime.json").write_text(json.dumps({"mode": "host"}))
    assert runtime.external_host is True, \
        "runtime.json mode=host did not select the external-host branch"


def host_marker(runtime, project_id="alpha"):
    """The host's process marker for this project's driver (`host-process.json`)."""
    return runtime.root / runtime.get(project_id)["driver_id"] / "host-process.json"


async def hold_lock_across_the_marker_gap(runtime, lock, marker, project_id="alpha"):
    """Play the host's shutdown acknowledgement, with a foreign write lock in hand.

    Used only by the final-stamp arm. In the external branch `stop` issues
    `phase="stopping"`, then polls the host's marker every 0.2 s until it is gone,
    then issues `phase="stopped"`. This coroutine waits until the `stopping` stamp
    has COMMITTED, takes the foreign write lock on a thread (so the loop itself
    never waits on it), and removes the marker while the lock is held. The next
    poll of `stop` sees the marker gone and issues the final stamp into a held
    lock. Lock first, marker second: the marker is never gone while the lock is
    still free.
    """
    deadline = time.monotonic() + 10
    while runtime.get(project_id)["phase"] != "stopping":
        assert time.monotonic() < deadline, "stop never issued its stopping stamp"
        await asyncio.sleep(LOOP_TICK_SECONDS)
    await asyncio.to_thread(lock.__enter__)
    marker.unlink()


async def test_start_stamps_off_the_event_loop(runtime):
    """Arm: the `phase="starting"` stamp inside `start`."""
    provisioned(runtime)
    try:
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                row = await runtime.start("alpha", actor="fixture")
        assert_loop_kept_running(times, lock, "start")
        # The stamp is off the loop, not skipped: the row carries it.
        assert row["phase"] == "starting" and row["enabled"] == 1
        assert runtime.processes["alpha"].returncode is None
    finally:
        await runtime.close()


async def test_start_failure_stamp_off_the_event_loop(runtime, monkeypatch):
    """Arm: the `phase="failed"` stamp in `start`'s except handler.

    `_environment` runs BEFORE the `starting` stamp, so failing it makes the
    failure stamp the only write `start` issues - the only one a held lock can
    meet. The error handling itself is asserted too: the caller still gets the
    `ValueError` chained from the original, and the row still records why.
    """
    provisioned(runtime)

    def refuse(_row):
        raise ValueError("fixture: no environment today")

    monkeypatch.setattr(runtime, "_environment", refuse)
    try:
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                with pytest.raises(ValueError, match="no environment today") as raised:
                    await runtime.start("alpha", actor="fixture")
        assert_loop_kept_running(times, lock, "start-failure")
        assert isinstance(raised.value.__cause__, ValueError), "the original error is still chained"
        row = runtime.get("alpha")
        assert row["phase"] == "failed" and not row["enabled"]
        assert row["last_error"] == "fixture: no environment today"
    finally:
        await runtime.close()


async def test_observe_stamps_off_the_event_loop(runtime):
    """Arm: the `phase="failed"` stamp in `_observe`, after the child exits."""
    try:
        await runtime.start("alpha", actor="fixture")
        proc = runtime.processes["alpha"]
        monitors = list(runtime.monitors)
        assert monitors, "start did not arm its exit monitor"
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                proc.kill()
                await asyncio.gather(*monitors)
        assert_loop_kept_running(times, lock, "observe")
        row = runtime.get("alpha")
        assert row["phase"] == "failed"
        assert "exited with code" in row["last_error"]
    finally:
        await runtime.close()


async def test_stop_stamps_off_the_event_loop(runtime):
    """Arm: the `phase="stopping"` stamp, the first write `stop` issues."""
    try:
        await runtime.start("alpha", actor="fixture")
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                row = await runtime.stop("alpha")
        assert_loop_kept_running(times, lock, "stop")
        assert row["phase"] == "stopped"
        assert runtime.processes == {}
    finally:
        await runtime.close()


async def test_stop_final_stamp_off_the_event_loop(runtime):
    """Arm: the FINAL `phase="stopped"` stamp, after the child has been reaped."""
    try:
        await runtime.start("alpha", actor="fixture")
        reaper = _ReapingUnderLock(runtime.processes["alpha"], runtime.db.db_path,
                                   "alpha", BLOCKING_WRITE_SECONDS)
        runtime.processes["alpha"] = reaper
        try:
            async with running_counter() as times:
                row = await runtime.stop("alpha")
            assert_loop_kept_running(times, reaper.window, "stop-final")
            assert row["phase"] == "stopped"
        finally:
            await reaper.join()
    finally:
        await runtime.close()


async def test_start_external_stamps_off_the_event_loop(runtime):
    """Arm: the `enabled=1, phase="starting"` stamp in `start`'s external-host branch.

    Host mode, no host process: `start` provisions the row, stamps it as starting
    and returns - it never spawns a child, because the host runs the driver.
    """
    provisioned(runtime)
    host_mode(runtime)
    try:
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                row = await runtime.start("alpha", actor="fixture")
        assert_loop_kept_running(times, lock, "start-external")
        # The stamp is off the loop, not skipped: the row carries it.
        assert row["phase"] == "starting" and row["enabled"] == 1
        assert not host_marker(runtime).exists(), "start must not create the host's marker"
    finally:
        await runtime.close()


async def test_stop_external_stamps_off_the_event_loop(runtime):
    """Arm: the `enabled=0, phase="stopping"` stamp in `stop`'s external-host branch.

    With no host marker, `stop` has nothing to wait for: it issues `stopping`, sees
    the marker already absent and issues the final `stopped` stamp straight away.
    Both stamps therefore run back-to-back in this one call, and the lock held
    across the call is contended by the FIRST of them only. The final stamp's own
    window is the next arm.
    """
    provisioned(runtime)
    host_mode(runtime)
    try:
        await runtime.start("alpha", actor="fixture")
        lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
        with lock:
            async with running_counter() as times:
                row = await runtime.stop("alpha")
        assert_loop_kept_running(times, lock, "stop-external")
        assert row["phase"] == "stopped" and row["enabled"] == 0
        assert not host_marker(runtime).exists()
    finally:
        await runtime.close()


async def test_stop_external_final_stamp_off_the_event_loop(runtime):
    """Arm: the final `phase="stopped"` stamp in `stop`'s external-host branch.

    Mechanism. The external branch issues `stopping`, then polls the host's
    `host-process.json` every 0.2 s until it is gone, then issues `stopped`. The
    marker-absent case (the arm above) runs both stamps back-to-back, so one lock
    held across the call only ever contends `stopping`. Here the marker is PRESENT
    when `stop` starts, so `stop` must poll; a stand-in for the host
    (`hold_lock_across_the_marker_gap`) waits for the `stopping` stamp to commit,
    takes the foreign write lock, and removes the marker while the lock is held.
    The next poll sees the marker gone and issues `stopped` into the held lock,
    so the lock covers the second stamp and not the first.

    The loop is not parked during the 0.2 s poll, so the whole-call floor is met
    by those ticks; the contended tail is where a synchronous final stamp shows.
    """
    provisioned(runtime)
    host_mode(runtime)
    lock = _ForeignWriteLock(runtime.db.db_path, "alpha", BLOCKING_WRITE_SECONDS)
    try:
        await runtime.start("alpha", actor="fixture")
        marker = host_marker(runtime)
        # The host's publish, as core.dsh_host writes it: the marker exists while it runs.
        marker.write_text(json.dumps({"pid": 1, "session_id": runtime.get("alpha")["session_id"],
                                      "seen_at": time.time()}))
        async with running_counter() as times:
            host = asyncio.create_task(hold_lock_across_the_marker_gap(runtime, lock, marker))
            try:
                row = await runtime.stop("alpha")
                await host
            finally:
                host.cancel()  # a no-op once the stand-in has finished
        assert_loop_kept_running(times, lock, "stop-external-final")
        assert row["phase"] == "stopped" and row["enabled"] == 0
        assert not marker.exists()
    finally:
        if lock.thread is not None:
            # Join the holder thread off the loop; releases the lock if it is still held.
            await asyncio.to_thread(lock.__exit__, None, None, None)
        await runtime.close()


def test_the_arms_are_measuring_a_real_lock():
    """Guard: the fixture lock really is a lock, so a green arm means something.

    If `BEGIN IMMEDIATE` ever stopped taking the write lock (journal mode change,
    a different file), every arm above would pass by measuring nothing. This
    proves contention directly, on the same database shape and the same statement
    the stamps issue, and only for a second.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        db = StateDatabase(f"{directory}/lock-probe.sqlite")
        runtime = DshDrivers(db, None, f"{directory}/drivers")
        with db.get_connection() as conn:
            conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,updated_at) "
                         "VALUES(?,?,?,?,?,?)",
                         ("alpha", "dsh-lock-probe", "fixture", "driver",
                          "aitelier-dsh-lock-probe", "2020-01-01T00:00:00+00:00"))
            conn.commit()
        started_at = time.monotonic()
        with _ForeignWriteLock(db.db_path, "alpha", BLOCKING_WRITE_SECONDS):
            runtime.update("alpha", phase="probe")
        waited = time.monotonic() - started_at
        print(f"[lock probe] the stamp waited {waited:.2f}s for a "
              f"{BLOCKING_WRITE_SECONDS}s held lock")
        assert waited >= BLOCKING_WRITE_SECONDS - 0.1, (
            f"the stamp returned after {waited:.2f}s: the foreign holder did not hold a "
            f"{BLOCKING_WRITE_SECONDS}s write lock, so the arms above measure nothing")
        assert runtime.get("alpha")["phase"] == "probe"
