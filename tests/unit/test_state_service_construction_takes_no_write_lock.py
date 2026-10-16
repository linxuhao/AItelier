"""A StateService is built per MCP/REST request, reads included. Building one on
an already-migrated database must not take SQLite's write lock: with a long
write in flight (a candidate's Git-bundle BLOB), every such request used to queue
in ``state_attempt_schema.initialize``'s BEGIN IMMEDIATE and a plain read failed
with "database is locked" after its 10 s busy timeout."""
import sqlite3
import time

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
