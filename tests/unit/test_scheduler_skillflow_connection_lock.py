"""Two scheduler threads, ONE SkillFlow connection, one lock.

SkillFlow opens a single connection for the whole process
(`sqlite3.connect(db_path, check_same_thread=False)`) and guards it with a
`threading.RLock`. The scheduler's tick prologue, the 30 s supervisor and inline
tools all issue statements on that one connection from different threads, and
the connection's statement cache is keyed by SQL TEXT: two threads running the
same text are handed the SAME prepared statement, one thread's reset/bind/step
lands inside the other, SQLite reports SQLITE_MISUSE, and CPython raises
`InterfaceError: bad parameter or other API misuse`.

Measured 2026-11-18 (issue iss-c79deaa9f8a84ad5, investigation commit
a34ae17836ca): two ticks for two DIFFERENT free projects, both on
`asyncio.to_thread` workers, both in `_get_or_create_skillflow_run`'s
`skillflow_runs` SELECT. The tick's `except Exception` then wrote
`failed:could not start run — bad parameter or other API misuse` onto two live
projects, and only a manual retry could take them out of it.

What is asserted here — and what is deliberately NOT:

* (a) every statement the host issues on that connection was issued while
  `sf._lock` was owned by the calling thread, and
* (b) no SQL text was ever executing on two threads at once,
* (c) the transient class is recorded as a retryable observation and the project
  is NOT marked failed, while a deterministic run-start defect still writes the
  terminal status it was always written for.

NOT asserted: that `InterfaceError` never appears in the process. Three
engine-internal sites are still unlocked inside skillflow itself and are a
separate fix; and "no exception in N rounds" is a probabilistic gate, while (a)
is the invariant that makes the exception impossible — it is red on the base
tree on the very first statement, without depending on timing. Tick speed is not
asserted either: the fix intentionally lets a tick wait for the lock.
"""

import sqlite3
import threading

import pytest
from skillflow import PipelineGraph, SkillFlow

from core import scheduler
from core.db_manager import DBManager

GRAPH_NAME = "conn_race_t"
PROJECTS = ("race-p1", "race-p2")
ROUNDS = 8


def _graph():
    return PipelineGraph._from_dict({
        "name": GRAPH_NAME,
        "begin": "a",
        "end_conditions": {"combinator": "or",
                           "conditions": [{"type": "step_complete", "step": "a"}]},
        "steps": [{"id": "a", "step_type": "agent", "agent_config": "x",
                   "timeout_seconds": 30}],
    })


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A real SkillFlow over a real temp DB, with two projects owning runs.

    Real, not a mock: the defect is about how two threads drive one real
    sqlite3 connection, so a MagicMock would prove nothing about the statement
    cache. `scheduler.db` is a real DBManager too — `_get_or_create_skillflow_run`
    starts by reading the project row.
    """
    sf = SkillFlow(str(tmp_path / "skillflow.db"),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"),
                   stale_threshold_seconds=180)
    sf.register_agent_config("x", model="m")
    sf.register_graph(_graph())
    run_ids = {}
    for pid in PROJECTS:
        run_id = sf.get_or_create_run(GRAPH_NAME, pid, {})
        sf.start_run(run_id)
        run_ids[pid] = run_id

    db = DBManager(str(tmp_path / "aitelier.db"))
    for pid in PROJECTS:
        db.ensure_project(pid, name=pid, config_name=GRAPH_NAME)

    monkeypatch.setattr(scheduler, "db", db)
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    return sf, db, run_ids


class _CursorRecorder:
    """A cursor that keeps its statement "running" until the row is consumed."""

    def __init__(self, recorder, cursor, sql):
        self._recorder = recorder
        self._cursor = cursor
        self._sql = sql

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        finally:
            self._recorder.finished(self._sql)

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        finally:
            self._recorder.finished(self._sql)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _ConnectionRecorder:
    """A stand-in for `sf._conn` that records HOW the connection is driven.

    Every statement still reaches the real sqlite3 connection, on the calling
    thread, with the caller's parameters; nothing is mocked, faked or dropped.
    The proxy records, at the moment a statement is handed to the connection,
    which thread issued it and whether `sf._lock` was owned by that thread — and
    keeps the statement "in flight" until its row is consumed (the step that
    actually touches the prepared statement is the fetch, not the execute), so a
    second thread inside the same SQL text is caught rather than inferred.
    """

    def __init__(self, conn, lock, hold_s=0.0):
        self._conn = conn
        self._lock = lock
        self.hold_s = hold_s
        self.statements = []        # (sql, thread name, lock_owned)
        self.overlaps = []          # (sql, first thread, second thread)
        self._in_flight = {}        # sql -> thread name
        self._guard = threading.Lock()

    def execute(self, sql, params=()):
        thread = threading.current_thread().name
        owned = bool(self._lock._is_owned())
        with self._guard:
            self.statements.append((sql, thread, owned))
            first = self._in_flight.get(sql)
            if first is not None and first != thread:
                self.overlaps.append((sql, first, thread))
            self._in_flight[sql] = thread
        try:
            if self.hold_s:
                # Widen the window on purpose: the production window is
                # microseconds, and this test must not depend on luck.
                threading.Event().wait(self.hold_s)
            cursor = self._conn.execute(sql, params)
        except BaseException:
            self.finished(sql)
            raise
        return _CursorRecorder(self, cursor, sql)

    def finished(self, sql):
        with self._guard:
            self._in_flight.pop(sql, None)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _drive_together(barrier, work, rounds=ROUNDS):
    """Run `work()` on two threads, one barrier per round, and collect errors."""
    errors = []

    def _thread():
        barrier.wait()
        for _ in range(rounds):
            barrier.wait()
            try:
                work()
            except BaseException as exc:          # noqa: BLE001 — surfaced below
                errors.append(exc)
            barrier.wait()

    threads = [threading.Thread(target=_thread, name=f"tick-worker-{i}")
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a worker thread never finished"
    return errors


# ── (a) + (b): the two-thread invariant ─────────────────────────────


def test_two_ticks_never_drive_the_shared_connection_unlocked(engine):
    """THE measured collision: two ticks, two projects, the same SELECT text.

    Both threads call the real `_get_or_create_skillflow_run` against one real
    SkillFlow temp DB, synchronised by a barrier, with the connection proxy in
    place. On the base tree the FIRST statement is issued with `sf._lock`
    unowned — the lock assertion below is red immediately, no timing needed.
    """
    sf, _db, run_ids = engine
    recorder = _ConnectionRecorder(sf._conn, sf._lock, hold_s=0.02)
    sf._conn = recorder

    def work():
        # One project per worker: two DIFFERENT free projects is exactly the
        # measured population (the same statement text is what collides).
        index = 0 if threading.current_thread().name.endswith("-0") else 1
        pid = PROJECTS[index]
        assert scheduler._get_or_create_skillflow_run(pid) == run_ids[pid]

    errors = _drive_together(threading.Barrier(2), work)

    assert not errors, f"the driven ticks raised: {errors!r}"
    assert recorder.statements, "the proxy recorded no statement at all"
    # (a) every statement ran while this thread owned the engine's lock
    unlocked = [(sql, thread) for sql, thread, owned in recorder.statements
                if not owned]
    assert unlocked == [], (
        "statements were issued on the shared SkillFlow connection WITHOUT "
        f"sf._lock: {unlocked!r}")
    # (b) no SQL text was executing on two threads at once
    assert recorder.overlaps == [], (
        "the same SQL text ran on two threads at once: "
        f"{recorder.overlaps!r}")
    # the driven statement IS the measured one, not a bystander
    assert any("FROM skillflow_runs" in sql for sql, _t, _o in recorder.statements)


def test_the_inflight_guards_read_the_connection_under_the_clock_lock(engine):
    """The tick's other two identical-text reads.

    `_has_active_claim` is dispatched with `asyncio.to_thread`, so two ticks put
    two threads inside the `skillflow_active_ops` and `skillflow_steps` SELECTs
    with the SAME text — the next collision after the run lookup. This run has
    no claim, so only the two scheduler statements run: no engine-internal
    statement can make the lock assertion mean something else.
    """
    sf, _db, run_ids = engine
    recorder = _ConnectionRecorder(sf._conn, sf._lock, hold_s=0.02)
    sf._conn = recorder

    def work():
        run_id = run_ids[PROJECTS[0]]
        assert scheduler._has_unsettled_operation(sf, run_id) is False
        assert scheduler._has_active_claim(sf, run_id) is False

    errors = _drive_together(threading.Barrier(2), work)

    assert not errors, f"the driven guards raised: {errors!r}"
    unlocked = [(sql, thread) for sql, thread, owned in recorder.statements
                if not owned]
    assert unlocked == [], (
        "statements were issued on the shared SkillFlow connection WITHOUT "
        f"sf._lock: {unlocked!r}")
    assert recorder.overlaps == [], (
        "the same SQL text ran on two threads at once: "
        f"{recorder.overlaps!r}")
    texts = " ".join(sql for sql, _t, _o in recorder.statements)
    assert "skillflow_active_ops" in texts and "skillflow_steps" in texts


def test_the_witness_notices_an_unlocked_statement(engine):
    """Control: the recorder itself can fail, so the assertions above have teeth.

    Two threads run the SAME SQL text through the proxy while holding nothing —
    the shape of the base tree. The proxy must report both an unlocked statement
    and an overlap; whether sqlite also raises is not asserted (that is the
    probabilistic half of the defect, and its appearance is not a test gate).
    """
    sf, _db, _run_ids = engine
    recorder = _ConnectionRecorder(sf._conn, sf._lock, hold_s=0.05)
    sql = ("SELECT id, status FROM skillflow_runs "
           "WHERE project_id = ? AND graph_name = ?")

    def work():
        try:
            recorder.execute(sql, (PROJECTS[0], GRAPH_NAME)).fetchone()
        except sqlite3.Error:
            pass                     # the real race may surface; not the point

    _drive_together(threading.Barrier(2), work, rounds=3)

    assert recorder.statements, "the control recorded no statement"
    assert all(not owned for _sql, _t, owned in recorder.statements), (
        "the control was supposed to run WITHOUT the lock")
    assert recorder.overlaps, (
        "the recorder did not notice two threads inside the same SQL text")


# ── (c): a transient concurrency error is not a terminal verdict ────


class _RaisingStatement:
    """`sf._conn` with one statement class replaced by an injected failure.

    Everything else still goes to the real connection, so the tick runs its real
    path and only the failing statement is synthetic.
    """

    def __init__(self, conn, needle, exc):
        self._conn = conn
        self._needle = needle
        self._exc = exc

    def execute(self, sql, params=()):
        if self._needle in sql:
            raise self._exc
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _drive_one_tick(monkeypatch, project_id):
    """Run one REAL `_run_skillflow_tick`, recording every tick_log line."""
    observed = []

    def _record(pid, outcome, **detail):
        observed.append((pid, outcome, detail))

    monkeypatch.setattr(scheduler, "tick_log", _record)
    import asyncio
    asyncio.run(scheduler._run_skillflow_tick(project_id, None))
    return observed


def test_a_transient_concurrency_error_is_not_a_terminal_project_failure(
        engine, monkeypatch):
    """The measured exception, on the measured path, must not fail the project.

    The injection is the exception CPython raised in production —
    `sqlite3.InterfaceError: bad parameter or other API misuse` — raised by the
    run-lookup statement inside `_get_or_create_skillflow_run`, i.e. exactly
    where the two ticks collided. The project must keep the status it had (so
    `get_next_active_project` picks it up again) and the tick must say so, as a
    RETRYABLE observation, instead of writing the terminal
    `failed:could not start run — …` row that made a healthy project look dead.
    """
    sf, db, _run_ids = engine
    pid = PROJECTS[0]
    before = db.get_project(pid)["status"]
    sf._conn = _RaisingStatement(
        sf._conn, "FROM skillflow_runs",
        sqlite3.InterfaceError("bad parameter or other API misuse"))

    observed = _drive_one_tick(monkeypatch, pid)

    status = db.get_project(pid)["status"]
    assert not status.startswith("failed"), (
        f"a transient concurrency error was recorded as a terminal project "
        f"failure: {status!r}")
    assert status == before, (
        "the transient arm must not touch the project status at all — the next "
        f"tick retries it: {before!r} -> {status!r}")
    retryable = [(pid_, outcome, detail) for pid_, outcome, detail in observed
                 if detail.get("retryable")]
    assert retryable, (
        f"no retryable observation was recorded for the transient error: "
        f"{observed!r}")
    pid_, outcome, detail = retryable[0]
    assert pid_ == pid
    assert outcome == "tick_error"
    assert detail.get("op") == "run_start"
    assert "misuse" in str(detail.get("error")), detail
    assert not any(outcome == "run_start_failed" for _p, outcome, _d in observed)


@pytest.mark.parametrize("exc", [
    sqlite3.InterfaceError("bad parameter or other API misuse"),
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("SQLITE_MISUSE: bad parameter or other API misuse"),
])
def test_the_transient_class_is_recognised_by_type_and_by_message(exc):
    """The three shapes measured/expected from the same root cause."""
    assert scheduler._is_transient_run_start_error(exc) is True


@pytest.mark.parametrize("exc", [
    sqlite3.IntegrityError(
        "UNIQUE constraint failed: skillflow_edge_counts.run_id"),
    ValueError("no such config: dpe_default_v2"),
    RuntimeError("isolation unavailable: run has no worktree"),
])
def test_a_deterministic_run_start_defect_is_not_transient(exc):
    """The terminal arm exists for these: they fail identically on every retry."""
    assert scheduler._is_transient_run_start_error(exc) is False


def test_a_deterministic_run_start_defect_is_still_terminal(engine, monkeypatch):
    """The control for the arm above: the real create_run defect still lands.

    A second max_loop edge on one (from, to) pair breaks `create_run` on a
    UNIQUE constraint, and no retry can fix it. That project must be told, once,
    with the reason — the behaviour the transient classification must not take
    away.
    """
    sf, db, _run_ids = engine
    pid = PROJECTS[0]
    sf._conn = _RaisingStatement(
        sf._conn, "FROM skillflow_runs",
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: skillflow_edge_counts.run_id"))

    observed = _drive_one_tick(monkeypatch, pid)

    status = db.get_project(pid)["status"]
    assert status.startswith("failed:could not start run — "), status
    assert "skillflow_edge_counts" in status
    assert len(status) <= 160
    assert ("run_start_failed" in [outcome for _p, outcome, _d in observed])
    assert not any(detail.get("retryable") for _p, _o, detail in observed)
