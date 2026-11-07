"""The per-request retire must tolerate losing a race with another construction.

``StateService`` is built per request and every construction runs
``core.state_subagents.initialize`` -> ``_retire``. That path was
check-then-DESTROY, so two constructions could both pass a check and the loser's
``DROP``/``RENAME`` raised ``sqlite3.OperationalError`` straight into the request
as a bare 500.

Measured before the fix (main a3ea874db6, probe kept at
~/.AItelier/dsh-drivers/dsh-c61eed4a13ba4aaaae96/evidence/subagents-retire-race):
two threads calling ``initialize`` over 200 fresh pre-P3 databases raised in
200 of 200 rounds, always ``no such table: state_subagent_contexts``.

The takeover-era registry is the half that must not be dropped: an empty one is
dropped, a non-empty one is RENAMED to ``driver_subagents_p3_takeover``.
"""
import sqlite3
import threading

import pytest

from core import state_subagents
from core.db_manager import DBManager
from core.state_subagents import RETIRED_TABLES, TAKEOVER_TABLE, initialize

ROUNDS = 24  # the criterion asks for at least 20 fresh databases


def _pre_p3_database(path, *, registry_rows=1, retired_rows=0):
    """The shape an upgrade out of the takeover era leaves behind.

    ``driver_subagents`` is created explicitly: the post-P3 ``SCHEMA`` is what
    normally creates it, and the column ``_retire`` tests for only exists in the
    takeover-era table.
    """
    DBManager(str(path))
    with sqlite3.connect(path) as conn:
        for table in RETIRED_TABLES:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
            for i in range(retired_rows):
                conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (i,))
        conn.execute("CREATE TABLE driver_subagents (project_id TEXT, driver_id TEXT,"
                     " enabled INTEGER DEFAULT 0, phase TEXT, provider TEXT, model TEXT,"
                     " created_at TEXT, updated_at TEXT, context_sha256 TEXT)")
        for i in range(registry_rows):
            conn.execute("INSERT INTO driver_subagents (project_id, driver_id, phase, context_sha256)"
                         " VALUES (?,?,'stopped','abc')", (f"p{i}", f"d{i}"))
        conn.commit()
    return str(path)


def _columns(path, table="driver_subagents"):
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _rows(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_losing_the_drop_race_is_absorbed(tmp_path, monkeypatch):
    """The loser's DROP finds the table already gone: absorb, never raise."""
    path = _pre_p3_database(tmp_path / "drop-loser.db", registry_rows=1)
    real_check = state_subagents._table_exists
    seeded = []

    def check(conn, table):
        present = real_check(conn, table)
        if table == RETIRED_TABLES[0] and not seeded:
            seeded.append(table)
            with sqlite3.connect(path) as winner:  # the winner dropped it first
                winner.execute(f"DROP TABLE {table}")
                winner.commit()
        return present

    monkeypatch.setattr(state_subagents, "_table_exists", check)
    initialize(DBManager(path))  # must not raise

    assert seeded == [RETIRED_TABLES[0]], "the stale check was never exercised"
    with sqlite3.connect(path) as conn:
        gone = {t for t in RETIRED_TABLES
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (t,)).fetchone()}
    assert gone == set(), "the empty retired tables are all dropped"
    # The registry is renamed, never dropped, so its row survives the takeover.
    assert "context_sha256" in _columns(path, TAKEOVER_TABLE)
    assert _rows(path, TAKEOVER_TABLE) == 1
    assert "context_sha256" not in _columns(path), "the post-P3 registry is recreated"


def test_losing_the_rename_race_is_absorbed(tmp_path, monkeypatch):
    """The loser's RENAME finds its target already created: absorb, never raise.

    The winner's work is seeded INSIDE the registry path - after the row count
    has read the legacy registry, before the ALTER - so the ALTER really runs and
    really loses. Seeding it before the call (as this test first did) makes the
    row COUNT fail first and proves nothing about the rename branch.
    """
    path = _pre_p3_database(tmp_path / "rename-loser.db", registry_rows=2)
    real_rows = state_subagents._registry_rows
    real_done = state_subagents._already_done
    seeded, absorbed = [], []

    def rows(conn):
        count = real_rows(conn)  # the legacy registry, still ours at this point
        if not seeded:
            seeded.append(count)
            with sqlite3.connect(path) as winner:  # ... and now the winner lands
                winner.execute(f"ALTER TABLE driver_subagents RENAME TO {TAKEOVER_TABLE}")
                winner.executescript(state_subagents.SCHEMA)
                winner.commit()
        return count

    def record(exc, source, target=None):
        verdict = real_done(exc, source, target)
        absorbed.append((str(exc), verdict))
        return verdict

    monkeypatch.setattr(state_subagents, "_registry_rows", rows)
    monkeypatch.setattr(state_subagents, "_already_done", record)

    initialize(DBManager(path))  # must not raise

    collision = f"there is already another table or index with this name: {TAKEOVER_TABLE}"
    assert seeded == [2], "the row count never saw the legacy registry"
    assert absorbed == [(collision, True)], f"the ALTER branch was not what lost: {absorbed}"
    assert _rows(path, TAKEOVER_TABLE) == 2, "the legacy rows survive the takeover"
    assert "context_sha256" not in _columns(path)


def test_a_registry_beside_an_existing_takeover_table_still_raises(tmp_path):
    """A state nobody has explained is not absorbed into silence."""
    path = _pre_p3_database(tmp_path / "anomaly.db", registry_rows=1)
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {TAKEOVER_TABLE} (project_id TEXT)")
        conn.commit()

    with pytest.raises(sqlite3.OperationalError) as refused:
        initialize(DBManager(path))

    assert TAKEOVER_TABLE in str(refused.value), str(refused.value)
    assert "context_sha256" in _columns(path), "the legacy registry is left untouched"
    assert _rows(path, "driver_subagents") == 1


def test_a_non_empty_retired_table_is_never_dropped(tmp_path):
    path = _pre_p3_database(tmp_path / "kept.db", registry_rows=0, retired_rows=1)

    initialize(DBManager(path))

    for table in RETIRED_TABLES:
        assert _rows(path, table) == 1, f"{table} holds rows and must be kept"


def test_an_unrelated_sqlite_error_is_not_absorbed(tmp_path, monkeypatch):
    """Only the two already-done texts are absorbed."""
    path = _pre_p3_database(tmp_path / "view.db", registry_rows=0)
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TABLE {RETIRED_TABLES[0]}")
        # An empty VIEW passes the count check but cannot be DROPped as a table.
        conn.execute(f"CREATE VIEW {RETIRED_TABLES[0]} AS SELECT 1 WHERE 0")
        conn.commit()
    real_check = state_subagents._table_exists
    monkeypatch.setattr(state_subagents, "_table_exists",
                        lambda conn, table: True if table == RETIRED_TABLES[0] else real_check(conn, table))

    with pytest.raises(sqlite3.OperationalError) as refused:
        initialize(DBManager(path))

    assert "DROP VIEW" in str(refused.value), str(refused.value)


def test_two_concurrent_initialises_over_fresh_databases_never_raise(tmp_path):
    """The real race: same round, two constructions, one fresh database each."""
    errors, rounds = [], ROUNDS
    for i in range(rounds):
        path = _pre_p3_database(tmp_path / f"race-{i}.db", registry_rows=1)
        barrier = threading.Barrier(2, timeout=60)

        def construct():
            try:
                barrier.wait()
                initialize(DBManager(path))
            except BaseException as exc:  # noqa: BLE001 - this is the measurement
                errors.append(f"round {i}: {type(exc).__name__}: {exc}")
                raise
            finally:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass

        threads = [threading.Thread(target=construct) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert "context_sha256" in _columns(path, TAKEOVER_TABLE), f"round {i} kept the registry"
        assert _rows(path, TAKEOVER_TABLE) == 1, f"round {i} preserved the row"
        assert "context_sha256" not in _columns(path), f"round {i} rebuilt the registry"

    assert rounds >= 20, rounds
    assert errors == [], errors
