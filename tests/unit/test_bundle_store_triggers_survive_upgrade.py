"""A database that predates the release ledger is UPGRADED in place.

Criterion W8 of ``evidence/bundle-store-release-20261120/report.json``: the
``CREATE TRIGGER IF NOT EXISTS`` trap. ``state_git_bundle_store_no_delete`` used
to refuse every DELETE; the release path narrows it to "refuse unless a release
row names this digest". ``IF NOT EXISTS`` cannot change an existing trigger's
body, and ``_current`` only asks whether an object of that NAME exists - so a
deployed database would keep the unconditional guard while every caller believed
the ledger was the door, and a release would be refused forever by a trigger the
code thought it had replaced.

The test therefore builds the PRE-CHANGE shape exactly (the old guard, no
``state_git_bundle_releases`` table, no custody view), shows that the gate says
the schema is not current, runs one ordinary initialization, and then measures
the RESULT BEHAVIOURALLY as well as textually: a delete without a release row is
still refused, and the same delete after that row exists succeeds.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3

import pytest

from core import state_attempt_schema
from core.state_attempts import SCHEMA as ATTEMPT_SCHEMA
from core.state_database import StateDatabase
from core.state_git_artifacts import (
    BUNDLE_STORE, CUSTODY_VIEW, RELEASE_TABLE, load_stored_bundle, store_bundle)
from core.state_service import StateService

OLD_GUARD = ("CREATE TRIGGER state_git_bundle_store_no_delete "
             "BEFORE DELETE ON state_git_bundle_store "
             "BEGIN SELECT RAISE(ABORT,'retained Git bundle bytes are retained'); END")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(seed, size):
    return random.Random(seed).randbytes(size)


def edit(data, seed, edits=8):
    """A near-identical successor, so the store keeps it as a zstd delta."""
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        at = rng.randrange(len(out) - 64)
        out[at:at + 16] = rng.randbytes(16)
    return bytes(out)


def state_db(tmp_path, name="state.db"):
    path = tmp_path / name
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    return path


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def regress_to_the_pre_release_shape(path):
    """Turn a current database back into the one a deployed pre-change file is."""
    conn = connect(path)
    conn.execute("DROP TRIGGER state_git_bundle_store_no_delete")
    conn.execute(OLD_GUARD)
    conn.execute("DROP VIEW " + CUSTODY_VIEW)
    conn.execute(f"DROP TRIGGER {RELEASE_TABLE}_no_update")
    conn.execute(f"DROP TRIGGER {RELEASE_TABLE}_no_delete")
    conn.execute(f"DROP INDEX {RELEASE_TABLE}_commit")
    conn.execute(f"DROP TABLE {RELEASE_TABLE}")
    conn.close()


def trigger_sql(path):
    conn = connect(path)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                           "AND name='state_git_bundle_store_no_delete'").fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def test_a_database_with_the_old_unconditional_guard_is_upgraded_in_place(tmp_path):
    path = state_db(tmp_path)
    conn = connect(path)
    v1 = blob(1, 256 * 1024)
    v2 = edit(v1, 2)
    assert store_bundle(conn, sha(v1), v1)[0] == "raw"
    assert store_bundle(conn, sha(v2), v2)[0] == "zstd-prefix"
    rows = [dict(row) for row in conn.execute(f"SELECT bundle_sha256,codec FROM {BUNDLE_STORE}")]
    conn.close()
    regress_to_the_pre_release_shape(path)
    # The pre-change shape really is pre-change, and the gate can see it.
    assert RELEASE_TABLE not in trigger_sql(path)
    conn = connect(path)
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (RELEASE_TABLE,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
                            (CUSTODY_VIEW,)).fetchone() is None
        assert not state_attempt_schema._current(conn, ATTEMPT_SCHEMA), \
            "_current() short-circuited on a database whose guard it never narrowed"
    finally:
        conn.close()
    # One ordinary construction point: the upgrade runs on the request path.
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    upgraded = trigger_sql(path)
    assert RELEASE_TABLE in upgraded, upgraded
    assert "OLD.bundle_sha256" in upgraded, upgraded
    conn = connect(path)
    try:
        assert state_attempt_schema._current(conn, ATTEMPT_SCHEMA), \
            "the gate still reports a pending upgrade after it ran"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (RELEASE_TABLE,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
                            (CUSTODY_VIEW,)).fetchone() is not None
        # Nothing was lost: the stored bytes are exactly the ones that were there.
        assert [dict(row) for row in conn.execute(
            f"SELECT bundle_sha256,codec FROM {BUNDLE_STORE}")] == rows
        assert load_stored_bundle(conn, sha(v1)) == v1
        assert load_stored_bundle(conn, sha(v2)) == v2
        # ...and the upgraded guard is BEHAVIOURALLY the narrowed one.
        with pytest.raises(sqlite3.IntegrityError, match="released"):
            conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (sha(v2),))
        conn.execute(
            f"INSERT INTO {RELEASE_TABLE}(bundle_sha256,commit_sha,codec,base_sha256,raw_size,"
            "payload_bytes,released_at,actor,rule,rows_before,payload_before,rows_after,payload_after) "
            "VALUES(?,?,'zstd-prefix',?,?,?,'2026-11-20T00:00:00+00:00','fixture',"
            "'no-live-consumer@1',2,2,1,1)",
            (sha(v2), "f" * 40, sha(v1), len(v2), 1))
        assert conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                            (sha(v2),)).rowcount == 1
    finally:
        conn.close()


def test_a_second_initialization_is_a_no_op_on_the_upgraded_database(tmp_path):
    path = state_db(tmp_path)
    regress_to_the_pre_release_shape(path)
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = connect(path)
    try:
        first = {row["name"]: row["sql"] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        assert state_attempt_schema._current(conn, ATTEMPT_SCHEMA)
    finally:
        conn.close()
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = connect(path)
    try:
        second = {row["name"]: row["sql"] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    finally:
        conn.close()
    assert second == first


def test_a_current_guard_without_the_ledger_is_still_repaired(tmp_path):
    """The other half of the trap: the body is right, the table it reads is gone."""
    path = state_db(tmp_path)
    conn = connect(path)
    conn.execute(f"DROP TRIGGER {RELEASE_TABLE}_no_update")
    conn.execute(f"DROP TRIGGER {RELEASE_TABLE}_no_delete")
    conn.execute(f"DROP INDEX {RELEASE_TABLE}_commit")
    conn.execute(f"DROP TABLE {RELEASE_TABLE}")
    try:
        assert not state_attempt_schema._current(conn, ATTEMPT_SCHEMA)
    finally:
        conn.close()
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = connect(path)
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (RELEASE_TABLE,)).fetchone() is not None
        assert state_attempt_schema._current(conn, ATTEMPT_SCHEMA)
    finally:
        conn.close()


def test_an_old_guard_on_an_otherwise_current_database_is_still_caught(tmp_path):
    """The staleness check, isolated from "the ledger is missing".

    A database that has every object the current schema declares - ledger,
    custody view, triggers - but kept the OLD unconditional bundle-store guard
    is what a restored backup or a reverted deployment looks like. If the gate
    only noticed the missing TABLE, this shape would report "current", skip the
    upgrade, and refuse every release forever.
    """
    path = state_db(tmp_path)
    conn = connect(path)
    conn.execute("DROP TRIGGER state_git_bundle_store_no_delete")
    conn.execute(OLD_GUARD)
    try:
        # Every other object is in place, so only the guard's TEXT can be why.
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (RELEASE_TABLE,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
                            (CUSTODY_VIEW,)).fetchone() is not None
        assert not state_attempt_schema._current(conn, ATTEMPT_SCHEMA), \
            "the gate called a database with the old unconditional guard current"
    finally:
        conn.close()
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    assert RELEASE_TABLE in trigger_sql(path)
    conn = connect(path)
    try:
        assert state_attempt_schema._current(conn, ATTEMPT_SCHEMA)
    finally:
        conn.close()
