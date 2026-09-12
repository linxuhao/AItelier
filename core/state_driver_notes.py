"Project-scoped, revisioned director handoff notes stored beside State facts."
from __future__ import annotations

from core.state_graph import StateConflict, StateGraphError, key, now, text

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_driver_notes (
    project_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision > 0),
    permanent_text TEXT NOT NULL,
    temporary_text TEXT NOT NULL,
    updated_by_actor TEXT NOT NULL,
    updated_by_director TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_driver_note_revisions (
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    permanent_text TEXT NOT NULL,
    temporary_text TEXT NOT NULL,
    actor TEXT NOT NULL,
    director_identity TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('replace','append')),
    section TEXT NOT NULL CHECK(section IN ('permanent','temporary')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, revision),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TRIGGER IF NOT EXISTS state_driver_note_revisions_no_update
BEFORE UPDATE ON state_driver_note_revisions
BEGIN SELECT RAISE(ABORT,'driver note revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_driver_note_revisions_no_delete
BEFORE DELETE ON state_driver_note_revisions
BEGIN SELECT RAISE(ABORT,'driver note revisions are append-only'); END;
"""


def note_text(value: str) -> str:
    if not isinstance(value, str) or len(value) > 100000:
        raise StateGraphError("driver note content must be text of at most 100000 characters")
    return value


class StateDriverNotes:
    """One CAS-protected notebook per State project; State remains authoritative."""

    def __init__(self, store, actor: str):
        self.store = store
        self.actor = text(actor, "authenticated actor", 320)
        with store.db.get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @staticmethod
    def _result(project_id: str, row) -> dict:
        if row is None:
            return {"project_id": project_id, "revision": 0, "permanent": "",
                    "temporary": "", "updated_at": None, "updated_by": None}
        return {
            "project_id": project_id,
            "revision": row["revision"],
            "permanent": row["permanent_text"],
            "temporary": row["temporary_text"],
            "updated_at": row["updated_at"],
            "updated_by": {"actor": row["updated_by_actor"],
                           "director_identity": row["updated_by_director"]},
        }

    def get(self, project_id: str) -> dict:
        project_id = key(project_id, "project_id")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            row = conn.execute("SELECT * FROM state_driver_notes WHERE project_id=?",
                               (project_id,)).fetchone()
        return self._result(project_id, row)

    def history(self, project_id: str, after_revision: int = 0, limit: int = 100) -> dict:
        project_id = key(project_id, "project_id")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM state_driver_note_revisions "
                "WHERE project_id=? AND revision>? ORDER BY revision LIMIT ?",
                (project_id, after_revision, limit + 1)).fetchall()
        entries = [{
            "project_id": project_id, "revision": row["revision"],
            "permanent": row["permanent_text"], "temporary": row["temporary_text"],
            "actor": row["actor"], "director_identity": row["director_identity"],
            "operation": row["operation"], "section": row["section"],
            "created_at": row["created_at"],
        } for row in rows[:limit]]
        return {"project_id": project_id, "entries": entries,
                "truncated": len(rows) > limit,
                "next_after_revision": entries[-1]["revision"] if entries else after_revision}

    def update(self, project_id: str, section: str, content: str, expected_revision: int,
               director_identity: str, operation: str = "replace") -> dict:
        project_id = key(project_id, "project_id")
        if section not in {"permanent", "temporary"}:
            raise StateGraphError("section must be permanent or temporary")
        if operation not in {"replace", "append"}:
            raise StateGraphError("operation must be replace or append")
        content = note_text(content)
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            row = conn.execute("SELECT * FROM state_driver_notes WHERE project_id=?",
                               (project_id,)).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise StateConflict(
                    f"driver note revision conflict: expected {expected_revision}, current {current}; "
                    "read the project note and retry with its revision")
            permanent = row["permanent_text"] if row else ""
            temporary = row["temporary_text"] if row else ""
            previous = permanent if section == "permanent" else temporary
            changed = content if operation == "replace" else previous + content
            if section == "permanent":
                permanent = changed
            else:
                temporary = changed
            revision = current + 1
            if row:
                conn.execute(
                    "UPDATE state_driver_notes SET revision=?,permanent_text=?,temporary_text=?,"
                    "updated_by_actor=?,updated_by_director=?,updated_at=? WHERE project_id=?",
                    (revision, permanent, temporary, self.actor, director_identity, timestamp, project_id))
            else:
                conn.execute(
                    "INSERT INTO state_driver_notes(project_id,revision,permanent_text,temporary_text,"
                    "updated_by_actor,updated_by_director,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id, revision, permanent, temporary, self.actor, director_identity, timestamp))
            conn.execute("INSERT INTO state_driver_note_revisions VALUES(?,?,?,?,?,?,?,?,?)",
                (project_id, revision, permanent, temporary, self.actor, director_identity,
                 operation, section, timestamp))
            self.store._event(conn, project_id, None, "driver_note_updated", {
                "revision": revision, "section": section, "operation": operation,
                "actor": self.actor, "director_identity": director_identity})
        return {
            "project_id": project_id, "revision": revision,
            "permanent": permanent, "temporary": temporary,
            "updated_at": timestamp,
            "updated_by": {"actor": self.actor, "director_identity": director_identity},
        }
