"""DshDrivers construction accepts a concurrent served_by_seen column add.

Two constructions that both see ``served_by_seen`` missing both run ALTER TABLE
... ADD COLUMN; the loser used to raise "duplicate column name: served_by_seen".
The deterministic witness makes the existence check answer "missing" while the
column is already there, so the loser path runs with no thread timing. The
two-thread test releases two constructors together over fresh databases and
requires that neither raises. It does not force an interleaving: which one wins
the ALTER is up to SQLite.
"""
import sqlite3
import sys
import threading

from core import state_schema_migration as migration
from core.db_manager import DBManager
from core.dsh_driver import SCHEMA, DshDrivers

TABLE = "dsh_driver_session_models"
COLUMN = "served_by_seen"
ROUNDS = 24  # fresh databases raced by two constructors each (the bar is >= 20)
COMMAND = [sys.executable, "-c", "pass"]


def _database(path, *, with_column):
    """A WAL database built from the real DSH schema, optionally without the column."""
    db = DBManager(str(path))
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        if not with_column:
            conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
        conn.commit()
    return db


def _columns(db):
    with db.get_connection() as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _construct_concurrently(db, root):
    """Two DshDrivers constructions released together by a barrier; returns their errors."""
    barrier = threading.Barrier(2)
    errors = []

    def construct(name):
        try:
            barrier.wait(timeout=30)
            DshDrivers(db, None, root / name, command=COMMAND)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=construct, args=(name,), name=name) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a constructor hung"
    return errors


def test_loser_whose_check_says_missing_constructs_without_raising(tmp_path, monkeypatch):
    # The winner already has served_by_seen; the pre-check is made to answer
    # "missing", as it does to a racing loser that read the table before the
    # winner's ALTER committed. The loser's ALTER must be absorbed, not raised.
    db = _database(tmp_path / "state.sqlite", with_column=True)
    assert COLUMN in _columns(db)
    checks = []

    def stale_check(_conn, table):
        checks.append(table)
        return set()

    monkeypatch.setattr(migration, "column_names", stale_check)
    DshDrivers(db, None, tmp_path / "drivers", command=COMMAND)

    assert checks == [TABLE], "the stale pre-check was never consulted, so nothing was tested"
    assert COLUMN in _columns(db)


def test_two_constructions_racing_over_fresh_databases_never_raise(tmp_path):
    assert ROUNDS >= 20, "the race is only evidence over at least 20 fresh databases"
    for round_no in range(ROUNDS):
        db = _database(tmp_path / f"race-{round_no}.sqlite", with_column=False)
        assert COLUMN not in _columns(db), "the fixture must start without the column"

        errors = _construct_concurrently(db, tmp_path / f"drivers-{round_no}")

        assert errors == [], f"round {round_no}: a concurrent construction raised {errors}"
        assert COLUMN in _columns(db), f"round {round_no}: the column was not added"
