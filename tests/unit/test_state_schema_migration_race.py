"""Additive State migrations accept a concurrent winner, and nothing else.

Two writers that both see a column missing both run ALTER TABLE ... ADD
COLUMN; the loser gets "duplicate column name: <col>". The deterministic
witnesses below make the existence check answer "missing" while the column is
already there, so the loser path runs with no thread timing. The two-thread
test documents the real shape only; its interleaving is not forced.
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core import state_schema_migration as migration
from core.db_manager import DBManager
from core.state_schema_migration import add_column_if_missing


def _plain_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    return conn


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_adds_a_missing_column_and_reports_only_its_own_addition(tmp_path):
    conn = _plain_db(str(tmp_path / "plain.sqlite"))
    try:
        assert add_column_if_missing(conn, "t", "b", "TEXT") is True
        assert _columns(conn, "t") == ["a", "b"]
        assert add_column_if_missing(conn, "t", "b", "TEXT") is False
    finally:
        conn.close()


def test_helper_leaves_commit_to_the_caller(tmp_path):
    conn = _plain_db(str(tmp_path / "plain.sqlite"))
    try:
        conn.execute("BEGIN")
        assert add_column_if_missing(conn, "t", "b", "TEXT") is True
        assert conn.in_transaction
        conn.rollback()
        assert _columns(conn, "t") == ["a"]
    finally:
        conn.close()


def test_loser_column_check_says_missing_but_column_exists(tmp_path, monkeypatch):
    # Deterministic loser witness: the winner already added `b`, and the
    # existence check is made to answer "missing", as it does to a racing
    # loser that read the table before the winner's ALTER committed.
    conn = _plain_db(str(tmp_path / "plain.sqlite"))
    try:
        conn.execute("ALTER TABLE t ADD COLUMN b TEXT")
        conn.commit()
        monkeypatch.setattr(migration, "column_names", lambda _conn, _table: set())
        raised = None
        try:
            add_column_if_missing(conn, "t", "b", "TEXT")
        except sqlite3.OperationalError as exc:
            raised = exc
        assert raised is None, f"duplicate column name: b was not absorbed: {raised}"
        assert _columns(conn, "t") == ["a", "b"]
    finally:
        conn.close()


class _FailingAlter:
    """Connection proxy: PRAGMA reads go through, every ALTER raises ``error``."""

    def __init__(self, conn, error):
        self._conn, self._error = conn, error

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("ALTER"):
            raise self._error
        return self._conn.execute(sql, *args)


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("no such table: t"),
    sqlite3.OperationalError("disk I/O error"),
    sqlite3.OperationalError("duplicate column name: other"),
    sqlite3.OperationalError("duplicate column name: b extra"),
    sqlite3.IntegrityError("duplicate column name: b"),
], ids=lambda e: f"{type(e).__name__}:{e}")
def test_scoping_witness_every_other_error_propagates(tmp_path, error):
    conn = _plain_db(str(tmp_path / "plain.sqlite"))
    try:
        with pytest.raises(type(error)) as caught:
            add_column_if_missing(_FailingAlter(conn, error), "t", "b", "TEXT")
        assert caught.value is error
        assert _columns(conn, "t") == ["a"]
    finally:
        conn.close()


def test_loser_at_state_graph_site_does_not_raise(tmp_path, monkeypatch):
    from core.state_graph import initialize_graph_schema
    db = DBManager(str(tmp_path / "state.sqlite"))
    initialize_graph_schema(db)
    monkeypatch.setattr(migration, "column_names", lambda _conn, _table: set())
    initialize_graph_schema(db)
    with db.get_connection() as conn:
        assert {"facet"} <= set(_columns(conn, "state_nodes"))
        assert {"multi_driver"} <= set(_columns(conn, "state_project_policy"))
        assert {"review_independence"} <= set(_columns(conn, "state_project_enforcement"))


def test_loser_at_director_messaging_site_does_not_raise(tmp_path, monkeypatch):
    from core.director_messaging import initialize
    db = DBManager(str(tmp_path / "state.sqlite"))
    initialize(db)
    monkeypatch.setattr(migration, "column_names", lambda _conn, _table: set())
    initialize(db)
    with db.get_connection() as conn:
        assert {"delivery_mode", "sender_driver_id", "ack_mode", "ack_quorum"} <= set(
            _columns(conn, "state_director_messages"))


def test_loser_at_driver_inbox_site_does_not_raise(tmp_path, monkeypatch):
    from core.driver_inbox import DriverInbox
    db = DBManager(str(tmp_path / "state.sqlite"))
    store = SimpleNamespace(db=db, project_read_trusted=True)
    DriverInbox(store, "driver-a", "driver-a")
    monkeypatch.setattr(migration, "column_names", lambda _conn, _table: set())
    DriverInbox(store, "driver-a", "driver-a")
    with db.get_connection() as conn:
        assert {"audience", "channel", "segments_json"} <= set(
            _columns(conn, "driver_inbox_messages"))


def test_two_writers_racing_one_additive_column_shape(tmp_path):
    # Shape only: the barrier releases both writers together, but which one
    # wins the ALTER is up to SQLite. Whatever the interleaving, exactly one
    # writer adds the column and the other is absorbed, with no exception.
    path = str(tmp_path / "race.sqlite")
    setup = _plain_db(path)
    setup.close()
    barrier = threading.Barrier(2)

    def writer(_):
        conn = sqlite3.connect(path, timeout=10)
        try:
            barrier.wait(timeout=30)
            return add_column_if_missing(conn, "t", "b", "TEXT")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert sorted(results) == [False, True]
    check = sqlite3.connect(path)
    try:
        assert _columns(check, "t") == ["a", "b"]
    finally:
        check.close()
