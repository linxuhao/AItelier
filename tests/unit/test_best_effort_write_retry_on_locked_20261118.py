"""Two best-effort rows that a held write lock used to cost, measured with a lock.

The defect (`iss-0297fe77883241b5`, read out of the live backend log, not
suspected): `DshDrivers.record_model_call` (called from the completion route)
and `driver_presence._wait_end` each lost their row to

    sqlite3.OperationalError: database is locked

- twice and three times respectively in one three-hour window. The first loss
  silently drops a turn from the owner's usage/cost accounting; the second
  leaves the driver reading as "still waiting" for the rest of the grace period
  after its wait actually ended.

Both writes now retry THAT failure a bounded three times. What is measured here
is the write against a REAL lock on a REAL database file - a second connection's
`BEGIN IMMEDIATE` - never a stubbed exception:

* the lock is RELEASED INSIDE the window (the release is triggered by the
  writer's own second attempt arriving) and the row must land - this arm FAILS
  without the retry, because the first refusal is the last thing that happens;
* the lock is HELD THROUGH the whole window and the writer must still return /
  the route must still serve the answer, with the existing warning as the only
  trace, and the row stays absent.

The two arms are told apart by the lock release - by construction, since the
release happens inside the connection factory at attempt N - and never by a
wall-clock assertion.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import sys
import threading
import time

import pytest

from core import off_loop_sqlite
from core.driver_presence import driver_waiting
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_database import StateDatabase
from core.state_service import StateService

ENDPOINT = "opencodego/deepseek-v4.1-flash"
LOCKED_MESSAGE = "database is locked"


@pytest.fixture
def runtime(tmp_path):
    """The store under test on its own real database file, with one project."""
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "lock-retry-pepper-" * 3)
    registry.seed("fixture-owner-token")
    registry.register("bob", "bob", actor="fixture")
    return DshDrivers(db, registry, tmp_path / "drivers",
                      command=[sys.executable, "-c", "import time; time.sleep(90)"])


class _LockRig:
    """A REAL write lock on the test's own database file, released ON ATTEMPT N.

    The lock is a second connection's ``BEGIN IMMEDIATE`` against the same file.
    ``release_on_attempt`` names the write attempt that must find it gone: the
    connection factory closes the lock BEFORE handing attempt N its connection,
    so the release is triggered by the writer's own attempt arriving.
    ``release_on_attempt=2`` therefore places the release inside the retry
    window - after the first refusal, before the attempt that has to land the
    row - and ``None`` keeps the lock for the whole window. Nothing here is
    timed, and nothing is asserted about elapsed time.

    The factory is only installed around the write under test, so every open it
    counts is one ATTEMPT by that writer (and only the busy timeout is short:
    the lock, the refusal and the OperationalError are SQLite's own).
    """

    def __init__(self, db, *, release_on_attempt=None, connect_timeout=0.05):
        self.db = db
        self.path = db.db_path
        self.release_on_attempt = release_on_attempt
        self.connect_timeout = connect_timeout
        self.attempts = 0
        self.off_loop = []
        self._held = threading.Event()
        self._release = threading.Event()
        self._holder = None

    # -- the competing writer ------------------------------------------------
    def hold(self):
        self._holder = threading.Thread(target=self._hold, name="lock-holder", daemon=True)
        self._holder.start()
        assert self._held.wait(10), "the competing writer never took the lock"

    def _hold(self):
        conn = sqlite3.connect(self.path, timeout=0.05)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._held.set()
            self._release.wait(30)
        finally:
            conn.rollback()
            conn.close()

    def release(self):
        self._release.set()
        if self._holder is not None:
            self._holder.join(30)

    # -- the writer's attempts ----------------------------------------------
    @contextlib.contextmanager
    def connections(self):
        @contextlib.contextmanager
        def open_connection():
            self.attempts += 1
            # `on_event_loop()` is False on the worker thread the writer runs on;
            # recorded per attempt so "off the loop" is measured, not assumed.
            self.off_loop.append(not off_loop_sqlite.on_event_loop())
            if self.attempts == self.release_on_attempt:
                self.release()
            conn = sqlite3.connect(self.path, timeout=self.connect_timeout)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                yield conn
            finally:
                conn.close()

        self.db.get_connection = open_connection
        try:
            yield self
        finally:
            del self.db.get_connection


@contextlib.contextmanager
def _impatient(db):
    """The REAL database during a whole-window arm, with only a short busy timeout.

    The route also reads through this connection (auth, the driver row), so this
    shape does not count attempts - the counting rig above is used where the
    region holds nothing but the write under test.
    """
    @contextlib.contextmanager
    def open_connection():
        conn = sqlite3.connect(db.db_path, timeout=0.05)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    db.get_connection = open_connection
    try:
        yield
    finally:
        del db.get_connection


# -- the retry's own contract ----------------------------------------------------
def test_the_retry_matches_the_lock_and_nothing_else():
    """`database is locked` is retried; every other permanent failure is not.

    Matching the CONDITION rather than the class is the point: a missing table,
    a read-only file or a malformed statement is an `OperationalError` too, and
    retrying one would triple the latency and delay the report without ever
    landing the row.
    """
    errors = []

    def locked():
        errors.append(LOCKED_MESSAGE)
        raise sqlite3.OperationalError(LOCKED_MESSAGE)

    sleeps = []
    with pytest.raises(sqlite3.OperationalError) as refused:
        off_loop_sqlite.retry_while_locked(locked, sleep=sleeps.append)
    assert LOCKED_MESSAGE in str(refused.value)
    assert len(errors) == off_loop_sqlite.LOCKED_ATTEMPTS == 3, (
        "three attempts: bounded, so a truly held lock costs a short wait, not a hang")
    assert all(0.05 <= delay <= 0.2 for delay in sleeps), (
        "each backoff is in the 50-200 ms window the issue asks for")

    for permanent in (sqlite3.OperationalError("no such table: nope"),
                      sqlite3.OperationalError("attempt to write a readonly database"),
                      sqlite3.IntegrityError("UNIQUE constraint failed")):
        errors.clear()
        sleeps.clear()

        def fails():
            errors.append(str(permanent))
            raise permanent

        with pytest.raises(type(permanent)):
            off_loop_sqlite.retry_while_locked(fails, sleep=sleeps.append)
        assert len(errors) == 1, f"retried a permanent failure: {permanent}"
        assert sleeps == []

    # And the row DOES land when a refusal is followed by a success.
    answers = []

    def eventually():
        answers.append(1)
        if len(answers) == 1:
            raise sqlite3.OperationalError(LOCKED_MESSAGE)
        return "landed"

    assert off_loop_sqlite.retry_while_locked(eventually, sleep=lambda _s: None) == "landed"
    assert len(answers) == 2


# -- writer 1: driver_presence._wait_end -----------------------------------------
async def _wait_and_end_it_under_lock(db, rig):
    """Run a REAL wait, take the competing lock, then end the wait under it.

    `driver_waiting` writes the wait row and hands `_wait_end` to a worker
    thread in its `finally` - the production path. The lock is taken only after
    the row exists, and the counting factory is installed only for the exit, so
    what the rig counts is exactly the `_wait_end` attempts.
    """
    wait = driver_waiting(db, "bob", 1.0)
    await wait.__aenter__()
    rig.hold()
    try:
        with rig.connections():
            await wait.__aexit__(None, None, None)
    finally:
        rig.release()


def _bob_waits(db):
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT wait_id,started_at,ended_at FROM driver_presence_waits WHERE driver_id='bob' "
            "ORDER BY started_at")]


async def test_the_end_of_a_wait_lands_when_the_lock_is_released_inside_the_window(runtime):
    """Arm 1, presence: the lock goes away between attempt 1 and attempt 2."""
    db = runtime.db
    rig = _LockRig(db, release_on_attempt=2)
    await _wait_and_end_it_under_lock(db, rig)

    rows = _bob_waits(db)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None, (
        "the wait ended and the row does not say so: that is the measured loss "
        "(the driver then reads as waiting for the rest of the grace period), and "
        "without the retry the first refusal is where this write stops")
    assert rig.attempts == 2, "attempt 1 refused, attempt 2 landed it"
    assert rig.off_loop == [True, True], (
        "both attempts ran off the event loop: the backoff between them is a "
        "blocking sleep and must never be paid on the loop thread")


async def test_the_end_of_a_wait_warns_and_returns_when_the_lock_is_held(runtime, caplog):
    """Arm 2, presence: the lock outlives the whole window.

    `_wait_end` runs in the `finally` of the wait, so it may never raise: the
    bounded failure is the existing warning, emitted once, and the row keeps its
    bounded `until` (a driver stays online no longer than its wait plus slack).
    """
    db = runtime.db
    rig = _LockRig(db)                      # never released inside the window
    caplog.set_level(logging.WARNING)

    gaps = []

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            moment = time.monotonic()
            gaps.append(moment - last)
            last = moment

    stop = asyncio.Event()
    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        await _wait_and_end_it_under_lock(db, rig)
    finally:
        stop.set()
        await task

    rows = _bob_waits(db)
    assert len(rows) == 1 and rows[0]["ended_at"] is None
    assert rig.attempts == 3, "all three attempts were spent on the held lock"
    assert rig.off_loop == [True, True, True]
    warnings = [r for r in caplog.records
                if "could not record the end of wait" in r.getMessage()]
    assert len(warnings) == 1, "reported once: not swallowed, and not flooded"
    assert warnings[0].levelname == "WARNING"
    assert len(gaps) > 10 and max(gaps) < 0.4, (
        "the writer blocked a bounded window on a worker thread; the event loop "
        "kept running through it instead of waiting for the lock")


# -- writer 2: DshDrivers.record_model_call --------------------------------------
def test_the_answering_model_lands_when_the_lock_is_released_inside_the_window(runtime):
    """Arm 1, the completion route's write: the row exists after one refusal."""
    db = runtime.db
    rig = _LockRig(db, release_on_attempt=2)
    rig.hold()
    try:
        with rig.connections():
            runtime.record_model_call("alpha", "native-child-locked", route="worker",
                                      served_by=ENDPOINT)
    finally:
        rig.release()

    rows = {r["session_id"]: r for r in runtime.session_models("alpha")}
    assert "native-child-locked" in rows, (
        "the turn was answered and paid for; the row that attributes it was lost "
        "to a lock that was released one attempt later")
    assert rows["native-child-locked"]["served_by"] == ENDPOINT
    assert rows["native-child-locked"]["served_by_seen"] == [ENDPOINT]
    assert rows["native-child-locked"]["calls"] == 1, (
        "a retried attempt is a whole transaction, so it cannot double-count the turn")
    assert rig.attempts == 2
    assert rig.off_loop == [True, True]


def test_the_answering_model_is_given_up_after_the_window_and_raises(runtime):
    """Arm 2, the writer itself: the lock outlives all three attempts.

    `record_model_call` raises by contract - its CALLER is what must not fail the
    turn, and that is measured through the route below. What is measured here is
    that the bounded retry gives up instead of looping, and that nothing half
    landed: the session has no row and the earlier session's row is untouched.
    """
    db = runtime.db
    runtime.record_model_call("alpha", "native-child-ok", route="worker", served_by=ENDPOINT)
    rig = _LockRig(db)                      # never released inside the window
    rig.hold()
    try:
        with rig.connections():
            with pytest.raises(sqlite3.OperationalError) as refused:
                runtime.record_model_call("alpha", "native-child-held", route="worker",
                                          served_by=ENDPOINT)
    finally:
        rig.release()

    assert LOCKED_MESSAGE in str(refused.value)
    assert rig.attempts == 3, "three attempts, then the caller's own failure branch"
    rows = {r["session_id"]: r for r in runtime.session_models("alpha")}
    assert "native-child-held" not in rows
    assert rows["native-child-ok"]["calls"] == 1


async def test_a_permanent_lock_still_serves_the_answer_and_says_it_was_unrecorded(
        runtime, tmp_path, monkeypatch, caplog):
    """Criterion: bookkeeping loss never costs the driver an answered turn.

    Driven through the REAL completion handler (the same app the production
    route serves) with a REAL lock held for the whole window: the answer is 200
    and unchanged, the retry is exhausted, and the turn is reported as
    `served unrecorded` - the existing warning, now the trace of a bounded
    failure rather than of a single refusal.
    """
    from fastapi.testclient import TestClient
    from tests.unit.test_dsh_driver import WORKER_ENDPOINTS, _driver_app, _driver_token

    app = _driver_app(runtime, tmp_path, monkeypatch)
    try:
        await runtime.start("alpha", actor="fixture")
        headers = {"X-AItelier-Driver-Token": _driver_token(runtime, "alpha")}
        payload = lambda session: {"session_id": session,
                                   "messages": [{"role": "user", "content": "work"}]}
        caplog.set_level(logging.WARNING)
        with TestClient(app) as client:
            ok = client.post("/api/dsh-drivers/alpha/completion",
                             json=payload("native-child-ok"), headers=headers)
            assert ok.status_code == 200, ok.text

            rig = _LockRig(runtime.db)
            rig.hold()
            try:
                with _impatient(runtime.db):
                    held = client.post("/api/dsh-drivers/alpha/completion",
                                       json=payload("native-child-held"), headers=headers)
            finally:
                rig.release()

            assert held.status_code == 200, (
                "the answer is already paid for: a failed record must not 500 the turn")
            assert held.json()["text"] == "ok"
            assert held.json()["served_by"] == ok.json()["served_by"] == WORKER_ENDPOINTS[0]

            lost = [r for r in caplog.records
                    if "could not record the answering model" in r.getMessage()
                    and "native-child-held" in r.getMessage()]
            assert len(lost) == 1, "reported once: not swallowed, and not flooded"
            message = lost[0].getMessage()
            assert "served unrecorded" in message
            assert "OperationalError" in message and "locked" in message

            stored = {s["session_id"]: s for s in runtime.session_models("alpha")}
            assert "native-child-held" not in stored, "the write really failed"
            assert stored["native-child-ok"]["calls"] == 1, (
                "the retries did not disturb the row already recorded")
    finally:
        await runtime.close()
