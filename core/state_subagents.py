"""Subagent registry, record only (multi-driver P3, design §4.6).

A subagent is a worker a driver started (a tmux process, a Codex thread, a
SkillFlow run, a remote session). State starts, probes and stops none of them.
This registry records who it is (``<parent driver>/<label>``), which checkout
it writes and which driver started it, for visibility - nothing more. It
grants nothing and is required by nothing: claims, evidence and heartbeats do
not consult it. After a takeover or handoff the old owner's workers are fenced
out by the attempt's fence+1 alone (core.state_recovery); the new owner
registers the workers it starts itself. No instructions or other private
working context are stored.
"""
from __future__ import annotations

import re
import sqlite3

from core.state_attempts import ACTIVE
from core.state_claims import ClaimError, digest, now_stamp
from core.state_graph import StateGraphError, StateNotFound, key, text
from core.state_privacy import UntrustedDatabase, writer_only_read

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_subagents (
    subagent_id TEXT PRIMARY KEY,
    parent_driver_id TEXT NOT NULL,
    project_id TEXT NOT NULL, attempt_id TEXT NOT NULL, node_key TEXT NOT NULL,
    workspace TEXT NOT NULL,
    request_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE INDEX IF NOT EXISTS driver_subagents_by_attempt ON driver_subagents(attempt_id);
CREATE INDEX IF NOT EXISTS driver_subagents_by_project ON driver_subagents(project_id, parent_driver_id);
CREATE TRIGGER IF NOT EXISTS driver_subagent_records_no_delete BEFORE DELETE ON driver_subagents
BEGIN SELECT RAISE(ABORT,'subagent records are kept'); END;
"""
# Tables of the P3 mechanisms the slimming removed (2026-10-10): private
# subagent instructions, the pending copy of driver notices, checkpoint
# decisions. Each is dropped only while EMPTY, so nothing is lost; one that
# holds rows is left as it is (no code reads or writes it any more).
RETIRED_TABLES = ("state_subagent_contexts", "driver_notices", "driver_checkpoint_decisions")


TAKEOVER_TABLE = "driver_subagents_p3_takeover"


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _registry_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(driver_subagents)")}


def _registry_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM driver_subagents").fetchone()[0]


def _already_done(exc, source: str, target=None) -> bool:
    """Is this error the loss of a race, i.e. did another construction get there first?

    A DROP - or the COUNT before it - on a table another construction already
    dropped says ``no such table: <source>``; a RENAME whose target another
    construction already created says ``there is already another table or index
    with this name: <target>``. Those two, and nothing else, may be absorbed.
    """
    message = str(exc)
    if message == f"no such table: {source}":
        return True
    return target is not None and \
        message == f"there is already another table or index with this name: {target}"


def _drop_if_empty(conn, table: str) -> None:
    """Drop one retired table that holds no rows, absorbing a losing race."""
    try:
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
            return
        conn.execute(f"DROP TABLE {table}")
    except sqlite3.OperationalError as exc:
        # The count loses the race as readily as the drop does, so both sit
        # inside the guard; either way the table is already gone.
        if not _already_done(exc, table):
            raise


def _retire_registry(conn) -> None:
    """Drop an empty takeover-era registry, rename a non-empty one."""
    try:
        if _registry_rows(conn) == 0:
            conn.execute("DROP TABLE driver_subagents")
            return
        conn.execute(f"ALTER TABLE driver_subagents RENAME TO {TAKEOVER_TABLE}")
    except sqlite3.OperationalError as exc:
        if not _already_done(exc, "driver_subagents", TAKEOVER_TABLE):
            raise
        # Absorb while the winner getting there first is what happened: our legacy
        # registry is gone, and whatever `driver_subagents` may exist by now is the
        # post-P3 table another construction just created. One still carrying
        # context_sha256 is the legacy registry sitting beside an existing takeover
        # table - a state nobody has explained, and not ours to leave in place.
        if "context_sha256" in _registry_columns(conn):
            raise


def _retire(conn) -> None:
    """Drop the tables the P3 slimming retired, tolerating a concurrent winner.

    This runs on EVERY StateService construction (the service is built per
    request), so two requests can both pass a check and the loser's DROP or
    RENAME raises sqlite3.OperationalError straight into the request. The loser
    has nothing left to do - what it wanted gone is already gone, or already
    renamed - so exactly those two texts are absorbed. Every other error, and a
    registry that is somehow still here beside an existing takeover table, still
    reaches the caller. No transaction is opened here, so construction keeps
    taking no write lock.
    """
    for table in RETIRED_TABLES:
        if _table_exists(conn, table):
            _drop_if_empty(conn, table)
    if "context_sha256" in _registry_columns(conn):
        _retire_registry(conn)


def initialize(db) -> None:
    with db.get_connection() as conn:
        _retire(conn)
        conn.executescript(SCHEMA)
        conn.commit()


class StateSubagents:
    """The ONE writer of ``driver_subagents``."""

    def __init__(self, store, actor, driver_id=None):
        self.store, self.actor = store, text(actor, "authenticated actor", 320)
        self.driver_id = driver_id
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

    def register_subagent(self, project_id, attempt_id, label, workspace):
        if not self.driver_id:
            raise ClaimError("driver_identity_required", "subagents belong to a registered driver")
        driver = self.driver_id
        if not isinstance(label, str) or not _LABEL.match(label):
            raise StateGraphError("label must match [A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
        workspace = text(workspace, "workspace", 500)
        subagent_id = f"{driver}/{label}"
        request_hash = digest({"attempt_id": attempt_id, "workspace": workspace})
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            old = conn.execute("SELECT * FROM driver_subagents WHERE subagent_id=?", (subagent_id,)).fetchone()
            if old is not None:
                if old["request_hash"] == request_hash and old["project_id"] == project_id:
                    return {**_view(old), "idempotent": True}
                raise ClaimError("subagent_exists", f"{subagent_id} is already registered (attempt "
                                 f"{old['attempt_id']}); choose another label")
            attempt = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=? AND project_id=?",
                                   (key(attempt_id, "attempt_id"), project_id)).fetchone()
            if attempt is None:
                raise StateNotFound(f"attempt {attempt_id!r} not found in project {project_id!r}")
            if attempt["status"] not in ACTIVE:
                raise ClaimError("attempt_not_active", f"attempt is {attempt['status']}")
            if attempt["owner_driver_id"] != driver:
                raise ClaimError("not_attempt_owner", f"attempt belongs to driver {attempt['owner_driver_id']}")
            row = {"subagent_id": subagent_id, "parent_driver_id": driver, "project_id": project_id,
                   "attempt_id": attempt_id, "node_key": attempt["node_key"], "workspace": workspace,
                   "request_hash": request_hash, "created_at": now_stamp()}
            conn.execute("INSERT INTO driver_subagents(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
                         tuple(row.values()))
            self.store._event(conn, project_id, attempt["node_key"], "subagent_registered", {
                "subagent_id": subagent_id, "attempt_id": attempt_id, "parent_driver_id": driver,
                "workspace": workspace, "actor": self.actor})
            return {**_view(row), "idempotent": False}

    @writer_only_read("list_subagents")
    def list_subagents(self, project_id, attempt_id=None, parent_driver_id=None, limit=100):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            sql, args = "SELECT * FROM driver_subagents WHERE project_id=?", [project_id]
            if attempt_id is not None:
                sql += " AND attempt_id=?"
                args.append(attempt_id)
            if parent_driver_id is not None:
                sql += " AND parent_driver_id=?"
                args.append(parent_driver_id)
            rows = conn.execute(sql + " ORDER BY created_at, subagent_id LIMIT ?", [*args, limit + 1]).fetchall()
        return {"subagents": [_view(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def _view(row) -> dict:
    data = dict(row)
    data.pop("request_hash", None)
    return data
