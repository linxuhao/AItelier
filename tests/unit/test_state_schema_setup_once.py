"""State schema setup runs once per database handle and FILE (core.state_schema_setup).

Every State request builds a StateService. Before this, every construction re-ran
every store's schema setup: on a scratch database 19 connections, 168 statements
and ~45 ms per construction (backend latency study, 2026-11-22). These tests count
what a construction costs once the file is set up, and pin that a different or
replaced file still gets its own setup. The attempt foreign-key verification stays
per request; its own tests are in test_state_service_construction_takes_no_write_lock.py.
"""
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import state_attempt_schema, state_channels, state_graph, state_schema_setup
from core.db_manager import DBManager
from core.state_service import StateService

SCHEMA_STATEMENT = ("CREATE", "ALTER", "DROP")


class Counted:
    """Every sqlite3 connection opened, and every statement run on one, while active."""

    def __init__(self, monkeypatch):
        self.connections, self.statements = 0, []
        original = sqlite3.connect

        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            self.connections += 1
            conn.set_trace_callback(lambda sql: self.statements.append(" ".join(sql.split())))
            return conn
        monkeypatch.setattr(sqlite3, "connect", connect)

    def reset(self):
        self.connections, self.statements = 0, []

    def schema_statements(self):
        return [s for s in self.statements
                if s.upper().startswith(SCHEMA_STATEMENT) or "TABLE_INFO" in s.upper()]


def construct(db, trusted=True):
    started = time.perf_counter()
    StateService(db, None, actor="fixture", project_read_trusted=trusted)
    return (time.perf_counter() - started) * 1000


@pytest.fixture
def window(monkeypatch):
    """A foreign-key verdict kept for the whole test, so a construction that finds
    nothing committed since the last pass opens nothing at all."""
    monkeypatch.setenv(state_attempt_schema._WINDOW_ENV, "600")
    yield
    state_attempt_schema._SCAN_MEMO.forget()


def test_construction_after_the_first_runs_no_setup_on_the_same_file(tmp_path, monkeypatch, window):
    db = DBManager(str(tmp_path / "state.db"))
    counted = Counted(monkeypatch)
    first_ms = construct(db)
    first = (counted.connections, len(counted.statements), len(counted.schema_statements()))
    assert first[0] > 0 and first[2] > 0, "the first construction must set the file up"

    # The second construction runs no setup either. It runs the foreign-key
    # verification the setup's migration path left unanswered (the gate drops its
    # kept verdict when it writes), and opens that file's one sentinel reader.
    counted.reset()
    second_ms = construct(db)
    second = (counted.connections, len(counted.statements))
    assert counted.schema_statements() == [], counted.schema_statements()[:5]
    assert counted.connections <= 2

    later = []
    for trusted in (True, False, True):
        counted.reset()
        ms = construct(db, trusted)
        later.append((counted.connections, len(counted.statements), round(ms, 2)))
        assert counted.connections == 0, f"construction #{len(later) + 2} opened {counted.connections} connections"
        # At most the kept verdict's one check, on the sentinel that already exists.
        assert counted.statements in ([], ["PRAGMA data_version"]), counted.statements[:5]
    print(f"SCHEMA_ONCE first: connections={first[0]} statements={first[1]} ms={first_ms:.1f}; "
          f"second: connections={second[0]} statements={second[1]} ms={second_ms:.1f}; later={later}")


def test_after_a_commit_only_the_foreign_key_verification_runs(tmp_path, monkeypatch, window):
    """Something committed since the last pass: the construction re-runs the
    foreign-key verification (one connection, a read transaction) and nothing else."""
    db = DBManager(str(tmp_path / "state.db"))
    construct(db)
    construct(db)           # the file's sentinel reader is open from here on
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tick(n INTEGER)")
        conn.execute("INSERT INTO tick VALUES(1)")
        conn.commit()
    counted = Counted(monkeypatch)
    passes = []
    monkeypatch.setattr(state_attempt_schema, "_FOREIGN_KEY_SCAN_HOOK", lambda: passes.append(1))
    ms = construct(db)
    assert len(passes) == 1, "the commit must re-run the per-request foreign-key pass"
    assert counted.connections == 1
    print(f"SCHEMA_ONCE after a commit: connections={counted.connections} "
          f"statements={len(counted.statements)} ms={ms:.1f}")
    assert counted.schema_statements() == [], counted.schema_statements()
    assert not [s for s in counted.statements if s.upper().startswith(("COMMIT", "BEGIN IMMEDIATE", "INSERT", "UPDATE"))]


def test_a_driver_request_after_the_start_was_settled_opens_nothing(tmp_path, monkeypatch, window):
    """Every DSH request carries a driver_id. Once the steward's start repair has run
    for this file, building that request's service asks nothing more of the database
    (the steward check used to open a connection on every such request)."""
    db = DBManager(str(tmp_path / "state.db"))
    construct(db)
    construct(db)
    monkeypatch.setattr(state_channels, "_START_SETTLED", {db.db_path})
    counted = Counted(monkeypatch)
    StateService(db, None, actor="driver:ada", project_read_trusted=True, driver_id="ada")
    assert counted.connections == 0, counted.statements[:5]


def test_a_new_file_gets_its_own_setup(tmp_path, monkeypatch, window):
    construct(DBManager(str(tmp_path / "a.db")))
    counted = Counted(monkeypatch)
    construct(DBManager(str(tmp_path / "b.db")))
    assert counted.connections > 0 and counted.schema_statements(), "a second file must be set up"
    with sqlite3.connect(str(tmp_path / "b.db")) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='driver_inbox_messages'").fetchone()


def test_a_new_handle_on_the_same_file_sets_it_up_again(tmp_path, monkeypatch, window):
    """Opening a database through a new handle runs the setup, as opening always did
    (a test that rewrites a schema and reopens it relies on that); production holds
    one handle per process."""
    path = str(tmp_path / "state.db")
    construct(DBManager(path))
    # Built before counting starts: DBManager's own init runs CREATE TABLE statements,
    # which would satisfy the assertion with no StateService setup at all.
    second = DBManager(path)
    counted = Counted(monkeypatch)
    construct(second)
    assert [s for s in counted.statements if "TABLE_INFO(DRIVER_INBOX_MESSAGES)" in s.upper()], \
        "a new handle must run the StateService setup"


def test_a_file_replaced_at_the_same_path_gets_its_own_setup(tmp_path, monkeypatch, window):
    path = str(tmp_path / "state.db")
    db = DBManager(path)
    construct(db)
    fresh = str(tmp_path / "fresh.db")
    sqlite3.connect(fresh).close()
    # Leave nothing in the old file's WAL for SQLite to apply to the new one.
    with db.get_connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.replace(fresh, path)
    counted = Counted(monkeypatch)
    construct(db)
    assert counted.schema_statements(), "a file moved over the path must be set up again"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_attempts'").fetchone()


def test_concurrent_first_constructions_set_the_file_up_once(tmp_path, monkeypatch, window):
    db = DBManager(str(tmp_path / "state.db"))
    runs, lock = [], threading.Lock()
    original = state_graph.initialize_graph_schema

    def counted(handle):
        with lock:
            runs.append(1)
        time.sleep(0.05)        # widen the window two first constructions share
        return original(handle)
    monkeypatch.setattr(state_graph, "initialize_graph_schema", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: construct(db), range(8)))
    assert len(runs) == 1, f"the graph schema setup ran {len(runs)} times for one file"


def test_a_setup_that_fails_is_run_again(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "state.db"))
    calls = []

    def setup(handle):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError):
        state_schema_setup.once(db, "probe", setup, db)
    assert state_schema_setup.once(db, "probe", setup, db) is True
    assert state_schema_setup.once(db, "probe", setup, db) is False
    assert len(calls) == 2
