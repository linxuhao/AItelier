"""A StateService is built per MCP/REST request, reads included. Building one on
an already-migrated database must not take SQLite's write lock: with a long
write in flight (a candidate's Git-bundle BLOB), every such request used to queue
in ``state_attempt_schema.initialize``'s BEGIN IMMEDIATE and a plain read failed
with "database is locked" after its 10 s busy timeout.

The same per-request gate also had to stop paying for its one expensive step
(seven foreign-key anti-joins, 20.2 ms of a 19.7 ms median call) on every read:
a burst of 58 polls in ten seconds filled all 40 anyio worker slots with it.
The tests below pin the trade that fixes it - the pass is reused for at most one
window per database file, and no answer is inherited by a replaced file, by a
schema mutation, or across a commit another connection made.
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from core import state_attempt_schema
from core.state_attempts import SCHEMA
from core.state_database import StateDatabase
from core.state_service import StateService


def node(key):
    return {"key": key, "goal": "Goal " + key,
            "acceptance": [{"id": "a", "kind": "test", "description": "d"}]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    database = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(database, actor="fixture", project_read_trusted=True)
    service.create_project("p", "P")
    service.store.add_nodes("p", [node("n")])
    attempt = service.start_external_attempt("p", "n", 1, "fixture", "x", "x")
    return database, attempt["attempt_id"]


def test_read_request_is_served_while_another_writer_holds_the_lock(db):
    database, attempt_id = db
    holder = sqlite3.connect(database.db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        reader = StateService(database, actor="fixture", project_read_trusted=True)
        assert reader.get_attempt(attempt_id)["attempt_id"] == attempt_id
        assert time.monotonic() - start < 3, "construction waited on the write lock"
    finally:
        holder.rollback()
        holder.close()


def test_pending_migration_and_owner_backfill_still_run_under_the_lock(db):
    database, attempt_id = db
    with database.get_connection() as conn:
        conn.execute("DROP INDEX state_attempts_lease")
        conn.execute("DROP TRIGGER state_external_owners_no_delete")
        conn.execute("DELETE FROM state_external_owners WHERE attempt_id=?", (attempt_id,))
        conn.commit()
        assert not state_attempt_schema._current(conn, SCHEMA)
    StateService(database, actor="fixture", project_read_trusted=True)
    with database.get_connection() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"state_attempts_lease", "state_external_owners_no_delete"} <= names
        assert conn.execute("SELECT status FROM state_external_owners WHERE attempt_id=?",
                            (attempt_id,)).fetchone()["status"] == "active"
        assert state_attempt_schema._current(conn, SCHEMA)


def test_existing_foreign_key_violation_is_still_refused(db):
    database, _ = db
    with database.get_connection() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO state_external_owners(attempt_id,project_id,node_key,harness,external_id,"
                     "status,admitted_at,updated_at) VALUES('ghost','p','n','h','e','active','t','t')")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="foreign-key validation"):
        StateService(database, actor="fixture", project_read_trusted=True)


# ---------------------------------------------------------------------------
# The one step the gate may reuse (iss-5de9f15b64f54027)
# ---------------------------------------------------------------------------
def request(database):
    """One State request's construction - exactly what a route does per call."""
    return StateService(database, actor="fixture", project_read_trusted=True)


def count_passes(monkeypatch, passes):
    """Count the expensive pass itself, not the cheap checks around it."""
    monkeypatch.setattr(state_attempt_schema, "_FOREIGN_KEY_SCAN_HOOK", lambda: passes.append(1))


def open_window(monkeypatch, seconds):
    monkeypatch.setenv(state_attempt_schema._WINDOW_ENV, str(seconds))


def current_now(database):
    """``_current`` asked the way ``initialize`` asks it, in its own read txn."""
    path = database.db_path
    with database.get_connection() as conn:
        return state_attempt_schema._current(conn, SCHEMA, path, state_attempt_schema._file_identity(path))


def kept_verdict(database):
    path = database.db_path
    return state_attempt_schema._SCAN_MEMO.verdict(path, state_attempt_schema._file_identity(path))


def plant_violation(database, attempt_id="ghost"):
    with database.get_connection() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO state_external_owners(attempt_id,project_id,node_key,harness,external_id,"
                     "status,admitted_at,updated_at) VALUES(?, 'p','n','h','e','active','t','t')",
                     (attempt_id,))
        conn.commit()


@pytest.fixture(autouse=True)
def no_window_outlives_a_test():
    """The memo is process state; a test must neither inherit nor leave a verdict."""
    yield
    state_attempt_schema._SCAN_MEMO.forget()


def test_concurrent_gate_calls_inside_one_window_run_the_pass_once(db, monkeypatch):
    """N simultaneous requests inside one window: one pass, N identical answers.

    This is the burst that filled every anyio worker slot: 40 requests re-running
    the same seven anti-joins. The released group shows the reuse changes what a
    call costs and never what it answers.
    """
    database, attempt_id = db
    passes = []
    count_passes(monkeypatch, passes)
    open_window(monkeypatch, 600)

    def one(_):
        return request(database).get_attempt(attempt_id)["attempt_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        windowed = list(pool.map(one, range(8)))
    assert windowed == [attempt_id] * 8
    assert len(passes) == 1, f"one window ran the pass {len(passes)} times"

    passes.clear()
    open_window(monkeypatch, 0)          # the present behaviour, per call
    with ThreadPoolExecutor(max_workers=8) as pool:
        uncached = list(pool.map(one, range(8)))
    assert uncached == windowed
    assert len(passes) == 8, "the window at zero must run the pass on every call"


def test_an_open_window_does_not_hide_a_schema_mutation(db, monkeypatch):
    """Only the foreign-key pass is reused: a dropped object is seen at once."""
    database, _ = db
    passes = []
    count_passes(monkeypatch, passes)
    open_window(monkeypatch, 600)
    request(database)
    assert len(passes) == 1

    with database.get_connection() as conn:
        conn.execute("DROP INDEX state_attempts_lease")
        conn.commit()
    assert not current_now(database), "the open window hid a missing CREATE"

    request(database)                    # repairs it under the write lock
    assert len(passes) == 1, "the structural check answered; the pass was not re-run"
    with database.get_connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_attempts_lease'").fetchone()
        assert state_attempt_schema._current(conn, SCHEMA)


def test_planted_violation_is_refused_with_the_window_at_zero(db, monkeypatch):
    """The documented way back to a per-call scan still condemns corruption."""
    database, _ = db
    passes = []
    count_passes(monkeypatch, passes)
    open_window(monkeypatch, 0)
    plant_violation(database)
    assert not current_now(database)
    assert passes, "the window at zero must run the pass"
    with pytest.raises(sqlite3.IntegrityError, match="foreign-key validation"):
        request(database)


def test_a_commit_by_another_connection_ends_an_open_window(db, monkeypatch):
    """The window is not a grace period for writes: the answer follows the file.

    An FK-OFF insert commits through another connection, so the sentinel's
    data_version moves and the very next gate call re-runs the pass - which is
    also why the corruption tests above pass with the default window.
    """
    database, _ = db
    passes = []
    count_passes(monkeypatch, passes)
    open_window(monkeypatch, 600)
    request(database)
    assert kept_verdict(database) is True and len(passes) == 1

    plant_violation(database)
    assert kept_verdict(database) is None, "the window outlived a commit"
    with pytest.raises(sqlite3.IntegrityError, match="foreign-key validation"):
        request(database)
    assert len(passes) == 2, "the next call after the commit must run the pass"


def test_a_replaced_database_file_does_not_inherit_the_verdict(db, monkeypatch, tmp_path):
    """Keyed on the file (st_dev/st_ino), not on the path string."""
    database, _ = db
    passes = []
    count_passes(monkeypatch, passes)
    open_window(monkeypatch, 600)
    request(database)
    assert kept_verdict(database) is True and len(passes) == 1

    # A second file: the same State schema, with the corruption the loud path
    # must see. Built on its own path, then moved over the one the gate knows.
    successor = str(tmp_path / "successor.sqlite")
    with closing(sqlite3.connect(database.db_path)) as source, closing(sqlite3.connect(successor)) as copy:
        source.backup(copy)
    with closing(sqlite3.connect(successor)) as planted:
        planted.execute("PRAGMA foreign_keys=OFF")
        planted.execute("INSERT INTO state_external_owners(attempt_id,project_id,node_key,harness,external_id,"
                        "status,admitted_at,updated_at) VALUES('ghost','p','n','h','e','active','t','t')")
        planted.commit()
    # Leave nothing in the destination's WAL for SQLite to apply to the new file.
    with database.get_connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.replace(successor, database.db_path)

    # The wal_checkpoint above moves the sentinel's data_version as well, so this
    # arm also passes with _file_identity disabled: two witnesses, one verdict.
    # Neutralise the commit witness here, or the file identity is never what a
    # reader sees answer. (Director, 2026-11-16: without this line the identity
    # check survives a mutant that makes it constant.)
    monkeypatch.setattr(state_attempt_schema, "_meanwhile_committed", lambda *a: False)

    assert state_attempt_schema._file_identity(database.db_path) is not None
    assert kept_verdict(database) is None, "the replaced file inherited the old verdict"
    with pytest.raises(sqlite3.IntegrityError, match="foreign-key validation"):
        request(database)
    assert len(passes) == 2, "the gate must scan the file that is there now"

