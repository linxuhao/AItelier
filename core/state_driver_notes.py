"Project-scoped, revisioned director handoff notes stored beside State facts."
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.state_graph import StateConflict, StateGraphError, key, now, text

MAX_SECTION_CHARS = 100000
MAX_SEARCH_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 100
MAX_EXCERPT_CHARS = 1000

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
    if not isinstance(value, str) or len(value) > MAX_SECTION_CHARS:
        raise StateGraphError(
            f"driver note section must be text of at most {MAX_SECTION_CHARS} characters")
    return value


def _time_boundary(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise StateGraphError(f"{label} must be an ISO-8601 timestamp of at most 64 characters")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateGraphError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StateGraphError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _redact(value: str) -> str:
    value = re.sub(
        r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]", value)
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(://[^\s/:@]+:)[^\s/@]+(@)", r"\1[REDACTED]\2"),
        (r"(?i)((?:password|passwd|pwd|passphrase|private[_ -]?key|credential(?:s)?|"
         r"x-aitelier-admin-token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
         r"auth[_-]?token|client[_-]?secret|secret)\s*[\"']?\s*[:=]\s*)"
         r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]}]+)", r"\1[REDACTED]"),
        (r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_\-]{16,}\b", "[REDACTED]"),
        (r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def _excerpt(value: str, query: str, limit: int) -> str:
    redacted = _redact(value)
    if len(redacted) <= limit:
        return redacted
    folded = redacted.casefold()
    position = folded.find(query.casefold()) if query else 0
    if position < 0:
        position = 0
    start = max(0, min(position - limit // 3, len(redacted) - limit))
    excerpt = redacted[start:start + limit]
    if start:
        excerpt = "…" + excerpt[1:]
    if start + limit < len(redacted):
        excerpt = excerpt[:-1] + "…"
    return excerpt


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

    def search(self, project_id: str, query: str = "", section: str | None = None,
               actor: str | None = None, director_identity: str | None = None,
               after_revision: int = 0, min_revision: int | None = None,
               max_revision: int | None = None, created_after: str | None = None,
               created_before: str | None = None, limit: int = 20,
               excerpt_chars: int = 320) -> dict:
        """Search changed sections only; return stable revision-ordered bounded excerpts."""
        project_id = key(project_id, "project_id")
        if not isinstance(query, str) or len(query) > MAX_SEARCH_QUERY_CHARS:
            raise StateGraphError(
                f"query must be text of at most {MAX_SEARCH_QUERY_CHARS} characters")
        if section not in {None, "permanent", "temporary"}:
            raise StateGraphError("section must be permanent or temporary")
        for value, label in ((actor, "actor"), (director_identity, "director_identity")):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 320):
                raise StateGraphError(f"{label} must be nonempty text of at most 320 characters")
        bounds = ((after_revision, "after_revision", 0),
                  (min_revision, "min_revision", 1), (max_revision, "max_revision", 1))
        for value, label, minimum in bounds:
            if value is not None and (type(value) is not int or not minimum <= value <= 2**63 - 1):
                raise StateGraphError(
                    f"{label} must be an integer between {minimum} and {2**63 - 1}")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise StateGraphError(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if type(excerpt_chars) is not int or not 64 <= excerpt_chars <= MAX_EXCERPT_CHARS:
            raise StateGraphError(
                f"excerpt_chars must be an integer between 64 and {MAX_EXCERPT_CHARS}")
        created_after = _time_boundary(created_after, "created_after")
        created_before = _time_boundary(created_before, "created_before")
        if created_after and created_before and created_after >= created_before:
            raise StateGraphError("created_after must be earlier than created_before")
        if min_revision is not None and max_revision is not None and min_revision > max_revision:
            raise StateGraphError("min_revision must not exceed max_revision")

        clauses = ["project_id=?", "revision>?"]
        args: list[object] = [project_id, after_revision]
        for sql, value in (("revision>=?", min_revision), ("revision<=?", max_revision),
                           ("created_at>?", created_after), ("created_at<?", created_before),
                           ("section=?", section), ("actor=?", actor),
                           ("director_identity=?", director_identity)):
            if value is not None:
                clauses.append(sql)
                args.append(value)
        if query:
            clauses.append("instr(lower(CASE section WHEN 'permanent' THEN permanent_text "
                           "ELSE temporary_text END), lower(?)) > 0")
            args.append(query)
        args.append(limit + 1)
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = conn.execute(
                "SELECT revision,actor,director_identity,operation,section,created_at,"
                "CASE section WHEN 'permanent' THEN permanent_text ELSE temporary_text END AS changed_text "
                "FROM state_driver_note_revisions WHERE " + " AND ".join(clauses) +
                " ORDER BY revision ASC LIMIT ?", args).fetchall()
        entries = [{
            "revision": row["revision"], "section": row["section"],
            "operation": row["operation"], "actor": row["actor"],
            "director_identity": row["director_identity"],
            "created_at": row["created_at"],
            "excerpt": _excerpt(row["changed_text"], query, excerpt_chars),
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
            # Validate the resulting section inside the transaction and before
            # any note, revision, or event write.
            changed = note_text(changed)
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
