"""The activity stamp: off the event loop, patient past one busy timeout, bounded.

The defect this file measures has two halves and each half is asserted against a
real clock, not against the shape of the code:

  a. `POST /api/dsh-drivers/{project}/activity` used to run its sqlite write on
     the event loop, so a busy database froze EVERY request in the process for as
     long as the write waited. The arm below blocks the write 0.5 s and counts how
     often a 5 ms counter coroutine got to run during one request.
  b. The stamp write itself made ONE attempt, i.e. it waited out a single
     connection's busy timeout (5 s on `sqlite3.connect`'s default, which is what
     the host database's connection uses). A 7.0 s write lock therefore lost the
     stamp with `OperationalError: database is locked`, and the activity tick
     never reached the row.
  c. Patience is not the same as waiting forever: a lock that outlives the whole
     budget must still raise, and must raise inside a stated ceiling.

The RED arms live outside this file (they are runs of this file against a copy of
the source tree): the base revision fails (a) with a near-frozen counter and (b)
with the lock error, and a mutant with the cap removed fails (c) while (a) and
(b) stay green.
"""
import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import authz
from api import dsh_driver_routers
from core.db_manager import DBManager
from core.drivers import Identity
from core.dsh_driver import DshDrivers

# The three clocks this file is about. Named here so the numbers are quotable.
LOOP_TICK_SECONDS = 0.005        # the counter coroutine's period
BLOCKING_WRITE_SECONDS = 0.5     # how long the stamp write blocks in arm (a)
MIN_TICKS = 25                   # arm (a) floor; a frozen loop measures 0
ONE_BUSY_TIMEOUT = 5.0           # sqlite3.connect's default, measured in arm (b)
DISCRIMINATING_LOCK = 7.0        # arm (b): longer than ONE_BUSY_TIMEOUT
OUTLIVING_LOCK = 21.0            # arm (c): longer than the whole stamp budget
STAMP_CEILING = 20.0             # arm (c): the stamp must give up inside this


class _BlockingRuntime:
    """A runtime whose stamp write blocks the CALLING thread for `delay` seconds.

    Only the three members the handler reaches are modelled (`get`, `running`,
    `update`); the point of arm (a) is which thread the write runs on, so the
    write is a plain `time.sleep` - a database lock wait is exactly that shape:
    a synchronous call that does not return until the lock is gone.
    """

    def __init__(self, driver_id="dsh-stamp-fixture", delay=BLOCKING_WRITE_SECONDS):
        self.driver_id = driver_id
        self.delay = delay
        self.writes = []

    def get(self, project_id):
        return {"project_id": project_id, "driver_id": self.driver_id, "enabled": 1}

    def running(self, project_id):
        return True

    def update(self, project_id, **fields):
        time.sleep(self.delay)
        self.writes.append({"project_id": project_id, **fields})


def _activity_app(runtime, monkeypatch):
    """The REAL activity route on a minimal app, with only its two inputs replaced.

    What is real: the router, the handler, `owned(...)`, the 409 branch, the SSE
    push and the write call itself. What is replaced, and nothing else: the
    `manager` dependency (so no driver process and no production database are
    needed), `authz.require_writer` (this file is about threading and lock
    patience, not about who may write - the route-level 403 is covered by its own
    tests), and `authz.request_identity` (which `owned` calls directly rather
    than as a dependency, so it cannot be overridden through FastAPI).
    """
    app = FastAPI()
    app.include_router(dsh_driver_routers.router)
    app.dependency_overrides[dsh_driver_routers.manager] = lambda: runtime
    app.dependency_overrides[authz.require_writer] = lambda: None
    monkeypatch.setattr(authz, "request_identity",
                        lambda request: Identity("driver", runtime.driver_id,
                                                 driver_id=runtime.driver_id))
    return app


async def test_the_event_loop_keeps_ticking_while_the_stamp_write_blocks(tmp_path, monkeypatch):
    """Arm (a): the write waits on a thread, so other coroutines keep running.

    Measured, not argued: a 5 ms counter runs for the duration of one request
    whose write blocks 0.5 s. Off the loop that is ~100 opportunities to run, so
    the floor of 25 is a generous pass; ON the event loop the same request leaves
    the counter with ZERO opportunities, because nothing else gets the thread
    until the handler returns.
    """
    runtime = _BlockingRuntime()
    app = _activity_app(runtime, monkeypatch)

    ticks = 0

    async def counter(stop):
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(LOOP_TICK_SECONDS)

    stop = asyncio.Event()
    task = asyncio.create_task(counter(stop))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/dsh-drivers/alpha/activity",
                                     json={"phase": "running", "subagents": ["child-1"], "error": ""})
        stop.set()
        await task

    print(f"[arm a] ticks during one blocked stamp: {ticks} (floor {MIN_TICKS})")
    assert response.status_code == 200, response.text
    assert ticks >= MIN_TICKS, (
        f"the event loop was blocked by the stamp write: the counter advanced only "
        f"{ticks} times in {BLOCKING_WRITE_SECONDS}s of a {LOOP_TICK_SECONDS}s period "
        f"(floor {MIN_TICKS})")
    # The write still happened, with the SAME keyword arguments as before.
    assert runtime.writes == [{"project_id": "alpha", "phase": "running",
                               "subagents_json": json.dumps(["child-1"]), "last_error": ""}]


@pytest.fixture
def stamp_runtime(tmp_path):
    """A real `dsh_drivers` row in a real host database (5 s busy timeout)."""
    db = DBManager(str(tmp_path / "activity-stamp.sqlite"))
    runtime = DshDrivers(db, None, tmp_path / "drivers")
    with db.get_connection() as conn:
        conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,updated_at) "
                     "VALUES(?,?,?,?,?,?)",
                     ("alpha", "dsh-stamp-fixture", "fixture", "driver",
                      "aitelier-dsh-stamp-fixture", "2020-01-01T00:00:00+00:00"))
        conn.commit()
    return runtime


class _ForeignWriteLock:
    """Another connection holds a real write lock on the database, for `hold` s.

    `BEGIN IMMEDIATE` is what takes the write lock in WAL mode; the UPDATE under
    it is the same table the stamp writes, so the stamp is contending for exactly
    the lock a concurrent writer would hold.
    """

    def __init__(self, db_path, project_id, hold):
        self.db_path, self.project_id, self.hold = db_path, project_id, hold
        self.held = threading.Event()

    def __enter__(self):
        def lock_and_hold():
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE dsh_drivers SET phase='lockholder' WHERE project_id=?",
                             (self.project_id,))
                self.held.set()
                time.sleep(self.hold)
                conn.commit()
            finally:
                conn.close()

        self.thread = threading.Thread(target=lock_and_hold, daemon=True)
        self.thread.start()
        assert self.held.wait(10), "the foreign connection never acquired the write lock"
        return self

    def __exit__(self, *exc_info):
        self.thread.join(self.hold + 15)
        return False


def test_stamp_survives_a_lock_longer_than_one_busy_timeout(stamp_runtime):
    """Arm (b): a 7.0 s lock - longer than ONE busy timeout - no longer loses it.

    On the base revision this same body raises
    `sqlite3.OperationalError: database is locked` out of `update(...)` after
    ~5 s, and the row keeps its old phase: the activity tick is lost.
    """
    db_path = stamp_runtime.db.db_path
    started_at = time.monotonic()
    with _ForeignWriteLock(db_path, "alpha", DISCRIMINATING_LOCK):
        stamp_runtime.update("alpha", phase="busy")
        elapsed = time.monotonic() - started_at

    print(f"[arm b] stamp returned after {elapsed:.2f}s against a {DISCRIMINATING_LOCK}s lock")
    # It really waited the lock out (a write that never contended would prove
    # nothing) and it was patient past ONE busy timeout (5 s), not merely inside it.
    assert elapsed >= DISCRIMINATING_LOCK - 0.3, (
        f"the stamp returned after {elapsed:.2f}s, before a {DISCRIMINATING_LOCK}s lock "
        f"could be released - the arm did not measure contention")

    row = stamp_runtime.get("alpha")
    assert row["phase"] == "busy"
    assert row["updated_at"] > "2020-01-01T00:00:00+00:00", "updated_at did not advance"


def test_stamp_gives_up_inside_the_ceiling_when_the_lock_outlives_patience(stamp_runtime):
    """Arm (c): bounded. A lock longer than the whole budget still raises.

    The budget is `STAMP_LOCK_ATTEMPTS` attempts of one full busy timeout each,
    so ~15.4 s here; the ceiling asserted is 20 s and the measured value is
    printed. The original error object must come back unchanged (its message
    names the locked database) and NOTHING may be written - a give-up is a
    failure the caller sees, never a silent success. The row's phase afterwards
    is the LOCK HOLDER's, which is how this arm also shows that the refused stamp
    left no trace of its own."""

    db_path = stamp_runtime.db.db_path
    started_at = time.monotonic()
    with _ForeignWriteLock(db_path, "alpha", OUTLIVING_LOCK):
        with pytest.raises(sqlite3.OperationalError) as raised:
            stamp_runtime.update("alpha", phase="busy")
        elapsed = time.monotonic() - started_at

    print(f"[arm c] stamp gave up after {elapsed:.2f}s against a {OUTLIVING_LOCK}s lock "
          f"(ceiling {STAMP_CEILING}s): {raised.value}")
    assert "locked" in str(raised.value), f"not the lock error: {raised.value!r}"
    assert elapsed < STAMP_CEILING, (
        f"the stamp waited {elapsed:.2f}s, past the {STAMP_CEILING}s ceiling: patience "
        f"is not bounded")
    row = stamp_runtime.get("alpha")
    assert row["phase"] != "busy", "the stamp claims a write the database refused"
    assert row["updated_at"] == "2020-01-01T00:00:00+00:00", (
        "a refused stamp still moved updated_at")


def test_patience_is_a_small_named_budget():
    """The budget is a named constant, and small enough to stay a stamp."""
    from core import dsh_driver

    attempts = getattr(dsh_driver, "STAMP_LOCK_ATTEMPTS", None)
    pause = getattr(dsh_driver, "STAMP_LOCK_PAUSE", None)
    assert isinstance(attempts, int) and 2 <= attempts <= 3, (
        f"STAMP_LOCK_ATTEMPTS must be 2 or 3, found {attempts!r}")
    assert isinstance(pause, float) and 0 < pause <= 0.5, (
        f"STAMP_LOCK_PAUSE must be a short pause, found {pause!r}")
