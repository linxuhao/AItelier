"Project-scoped, revisioned director handoff notes stored beside State facts."
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.state_driver_index import (ENTRY_SCHEMA, index_line, MAX_INDEX_LIMIT, address_of,
                                     assertion_text, body_text, entry_detail,
                                     entry_id_value, entry_summary, force_value,
                                     landed_text, new_entry_id, reason_text,
                                     referenced_addresses)
from core.state_graph import (StateConflict, StateGraphError, StateNotFound, key, now,
                              text)
from core.state_privacy import UntrustedDatabase, writer_only_read

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


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StateGraphError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StateGraphError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time_boundary(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise StateGraphError(f"{label} must be an ISO-8601 timestamp of at most 64 characters")
    return _instant(value, label)


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
        (r"(?i)\bxox[baprs]-[A-Za-z0-9-]{8,}\b", "[REDACTED]"),
        (r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def _excerpt(value: str, query: str, limit: int) -> str:
    redacted = _redact(value)
    if len(redacted) <= limit:
        return redacted
    folded_parts, offsets = [], []
    for index, character in enumerate(redacted):
        part = character.casefold()
        folded_parts.append(part)
        offsets.extend([index] * len(part))
    folded = "".join(folded_parts)
    folded_position = folded.find(query.casefold()) if query else 0
    position = offsets[folded_position] if folded_position >= 0 and offsets else folded_position
    if position < 0:
        position = 0
    start = max(0, min(position - limit // 3, len(redacted) - limit))
    excerpt = redacted[start:start + limit]
    if start:
        excerpt = "…" + excerpt[1:]
    if start + limit < len(redacted):
        excerpt = excerpt[:-1] + "…"
    return excerpt


def _matches(value: str, query: str) -> bool:
    return not query or query.casefold() in value.casefold()


def initialize(db) -> None:
    """Create the notebook tables on a real handle (the leaf's own, or the one
    an untrusted store runs ``initialize_state_schema`` on before dropping it)."""
    with db.get_connection() as conn:
        conn.executescript(SCHEMA + ENTRY_SCHEMA)
        conn.commit()


class StateDriverNotes:
    """One CAS-protected notebook per State project; State remains authoritative."""

    def __init__(self, store, actor: str, project_read_trusted: bool = False):
        self.store = store
        # The trust level of whoever built THIS notebook. `StateService` passes
        # its own, derived once per request from the raw credential. Any other
        # constructor that never declared one is UNTRUSTED, so a notebook rebuilt
        # from an anonymous service's store cannot read private notes by staying
        # silent. Every writer-only read below is refused from this value, at the
        # moment the read runs.
        self.project_read_trusted = bool(project_read_trusted)
        self.actor = text(actor, "authenticated actor", 320)
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

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

    @writer_only_read("get_driver_note")
    def get(self, project_id: str) -> dict:
        project_id = key(project_id, "project_id")
        with self.store.transaction(notebook=project_id) as conn:
            self.store._project(conn, project_id)
            row = conn.execute("SELECT * FROM state_driver_notes WHERE project_id=?",
                               (project_id,)).fetchone()
            index = self._index_projection(conn, project_id)
        return {**self._result(project_id, row), **index}

    @writer_only_read("driver_note_history")
    def history(self, project_id: str, after_revision: int = 0, limit: int = 100) -> dict:
        project_id = key(project_id, "project_id")
        with self.store.transaction(notebook=project_id) as conn:
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

    @writer_only_read("search_driver_note_history")
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
                           ("section=?", section), ("actor=?", actor),
                           ("director_identity=?", director_identity)):
            if value is not None:
                clauses.append(sql)
                args.append(value)
        selected = []
        with self.store.transaction(notebook=project_id) as conn:
            self.store._project(conn, project_id)
            rows = conn.execute(
                "SELECT revision,actor,director_identity,operation,section,created_at,"
                "CASE section WHEN 'permanent' THEN permanent_text ELSE temporary_text END AS changed_text "
                "FROM state_driver_note_revisions WHERE " + " AND ".join(clauses) +
                " ORDER BY revision ASC", args)
            for row in rows:
                created_at = _instant(row["created_at"], "stored driver note created_at")
                if created_after is not None and created_at <= created_after:
                    continue
                if created_before is not None and created_at >= created_before:
                    continue
                if not _matches(row["changed_text"], query):
                    continue
                selected.append(row)
                if len(selected) > limit:
                    break
        entries = [{
            "revision": row["revision"], "section": row["section"],
            "operation": row["operation"], "actor": _redact(row["actor"]),
            "director_identity": _redact(row["director_identity"]),
            "created_at": row["created_at"],
            "excerpt": _excerpt(row["changed_text"], query, excerpt_chars),
        } for row in selected[:limit]]
        return {"project_id": project_id, "entries": entries,
                "truncated": len(selected) > limit,
                "next_after_revision": entries[-1]["revision"] if entries else after_revision}

    @writer_only_read("get_driver_note")
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
            dangling = self._unresolved(conn, project_id, [(f"section:{section}", changed)])
            if dangling:
                raise StateGraphError(
                    "driver note section references addresses that do not resolve: "
                    + "; ".join(f"{item['address']} ({item['reason']})" for item in dangling))
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

    # ---- index mode: short assertions on the index, bodies fetched by address ----

    @staticmethod
    def _unresolved(conn, project_id: str, sources) -> list[dict]:
        """Every note:// address in ``sources`` that does not resolve to a body.

        This is the check that bites. It is not a comment and not a snapshot of
        today's data: it runs on every section write, on every entry write and
        on demand, and it reports the exact dangling addresses.
        """
        dangling = []
        for label, value in sources:
            for ref_project, ref_entry in referenced_addresses(value):
                address = f"note://{ref_project}/{ref_entry}"
                if ref_project != project_id:
                    dangling.append({"source": label, "address": address,
                                     "reason": "address leaves this project's notebook"})
                    continue
                found = conn.execute(
                    "SELECT 1 FROM state_driver_note_entries WHERE project_id=? AND entry_id=?",
                    (project_id, ref_entry)).fetchone()
                if found is None:
                    dangling.append({"source": label, "address": address,
                                     "reason": "no entry at this address"})
        return dangling

    @staticmethod
    def _entry(conn, project_id: str, entry_id: str):
        row = conn.execute(
            "SELECT * FROM state_driver_note_entries WHERE project_id=? AND entry_id=?",
            (project_id, entry_id)).fetchone()
        if row is None:
            raise StateNotFound(f"no driver note entry at {address_of(project_id, entry_id)}")
        return row

    @staticmethod
    def _counts(conn, project_id: str) -> dict:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(listing='listed') AS listed, SUM(listing='delisted') AS delisted "
            "FROM state_driver_note_entries WHERE project_id=?", (project_id,)).fetchone()
        return {"entry_count": row["total"] or 0, "listed_count": row["listed"] or 0,
                "delisted_count": row["delisted"] or 0}

    @classmethod
    def _index_projection(cls, conn, project_id: str) -> dict:
        """The part of the notebook that is cheap enough to inject everywhere.

        Bodies are NOT here; each line carries the address that fetches its body.
        delisted_count is here so a short index can never hide how much left it.
        """
        rows = conn.execute(
            "SELECT * FROM state_driver_note_entries WHERE project_id=? AND listing='listed' "
            "ORDER BY created_at, entry_id", (project_id,)).fetchall()
        counts = cls._counts(conn, project_id)
        return {"index": [{"address": address_of(project_id, row["entry_id"]),
                           "index_line": index_line(row)} for row in rows], **counts}

    def _write_entry(self, conn, project_id, assertion, body, director_identity,
                     force, landed, timestamp) -> str:
        entry_id = new_entry_id()
        dangling = self._unresolved(conn, project_id, [("body", body), ("assertion", assertion)])
        if dangling:
            raise StateGraphError(
                "driver note entry references addresses that do not resolve: "
                + "; ".join(f"{item['address']} ({item['reason']})" for item in dangling))
        conn.execute(
            "INSERT INTO state_driver_note_entries(project_id,entry_id,assertion,body,force,"
            "landed,listing,superseded_by,supersede_reason,delist_reason,actor,director_identity,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,'listed',NULL,'','',?,?,?,?)",
            (project_id, entry_id, assertion, body, force, landed, self.actor,
             director_identity, timestamp, timestamp))
        return entry_id

    @writer_only_read("get_driver_note_entry")
    def write_entry(self, project_id: str, assertion: str, body: str, director_identity: str,
                    force: str = "in_force", landed: str = "") -> dict:
        """Write one assertion plus its body and return the address of both."""
        project_id = key(project_id, "project_id")
        assertion = assertion_text(assertion)
        body = body_text(body)
        force = force_value(force)
        landed = landed_text(landed)
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            entry_id = self._write_entry(conn, project_id, assertion, body,
                                         director_identity, force, landed, timestamp)
            row = self._entry(conn, project_id, entry_id)
            projection = self._index_projection(conn, project_id)
            self.store._event(conn, project_id, None, "driver_note_updated", {
                "operation": "write_entry", "entry_id": entry_id,
                "address": address_of(project_id, entry_id), "actor": self.actor,
                "director_identity": director_identity})
            return {**entry_detail(row), **projection}

    @writer_only_read("get_driver_note_entry")
    def supersede_entry(self, project_id: str, entry_id: str, assertion: str, body: str,
                        reason: str, director_identity: str, force: str = "in_force",
                        landed: str = "") -> dict:
        """Retire an assertion IN PLACE: the old address keeps a tombstone, the old body stays."""
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        assertion = assertion_text(assertion)
        body = body_text(body)
        reason = reason_text(reason, "reason")
        force = force_value(force)
        landed = landed_text(landed)
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            previous = self._entry(conn, project_id, entry_id)
            if previous["superseded_by"]:
                raise StateConflict(
                    f"{address_of(project_id, entry_id)} is already superseded by "
                    f"{address_of(project_id, previous['superseded_by'])}; supersede that one")
            successor_id = self._write_entry(conn, project_id, assertion, body,
                                             director_identity, force, landed, timestamp)
            conn.execute(
                "UPDATE state_driver_note_entries SET superseded_by=?,supersede_reason=?,"
                "updated_at=? WHERE project_id=? AND entry_id=?",
                (successor_id, reason, timestamp, project_id, entry_id))
            retired = self._entry(conn, project_id, entry_id)
            successor = self._entry(conn, project_id, successor_id)
            projection = self._index_projection(conn, project_id)
            self.store._event(conn, project_id, None, "driver_note_updated", {
                "operation": "supersede_entry", "entry_id": entry_id,
                "successor_entry_id": successor_id,
                "address": address_of(project_id, successor_id), "actor": self.actor,
                "director_identity": director_identity})
            return {"superseded": entry_detail(retired), "successor": entry_detail(successor),
                    **projection}

    @writer_only_read("get_driver_note_entry")
    def delist_entry(self, project_id: str, entry_id: str, reason: str,
                     director_identity: str) -> dict:
        """Evict a line from the index without deleting its body.

        Refused while the entry can still change a decision. Both halves of that
        test are read from the stored row, never from a caller-supplied flag:
        there is no force override on this call.
        """
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        reason = reason_text(reason, "reason")
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            row = self._entry(conn, project_id, entry_id)
            if row["listing"] == "delisted":
                raise StateConflict(
                    f"{address_of(project_id, entry_id)} is already delisted; its body stays "
                    "readable at that address")
            still_binding = row["force"] == "in_force" and row["superseded_by"] is None
            if still_binding:
                raise StateGraphError(
                    f"{address_of(project_id, entry_id)} is still in force: reading it can still "
                    "change a decision, so it may not be delisted. Retire it with "
                    "supersede_driver_note_entry, which names the successor that replaces it.")
            conn.execute(
                "UPDATE state_driver_note_entries SET listing='delisted',delist_reason=?,"
                "updated_at=? WHERE project_id=? AND entry_id=?",
                (reason, timestamp, project_id, entry_id))
            delisted = self._entry(conn, project_id, entry_id)
            projection = self._index_projection(conn, project_id)
            self.store._event(conn, project_id, None, "driver_note_updated", {
                "operation": "delist_entry", "entry_id": entry_id,
                "address": address_of(project_id, entry_id),
                "delisted_count": projection["delisted_count"], "actor": self.actor,
                "director_identity": director_identity})
            return {**entry_detail(delisted), **projection}

    @writer_only_read("get_driver_note_entry")
    def get_entry(self, project_id: str, entry_id: str) -> dict:
        """Fetch one body by address. Bodies are never injected; they are fetched."""
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        with self.store.transaction(notebook=project_id) as conn:
            self.store._project(conn, project_id)
            return entry_detail(self._entry(conn, project_id, entry_id))

    @writer_only_read("driver_note_index")
    def entry_index(self, project_id: str, include_delisted: bool = False,
                    limit: int = 100) -> dict:
        project_id = key(project_id, "project_id")
        if type(limit) is not int or not 1 <= limit <= MAX_INDEX_LIMIT:
            raise StateGraphError(f"limit must be an integer between 1 and {MAX_INDEX_LIMIT}")
        clause = "" if include_delisted else " AND listing='listed'"
        with self.store.transaction(notebook=project_id) as conn:
            self.store._project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM state_driver_note_entries WHERE project_id=?" + clause +
                " ORDER BY created_at, entry_id LIMIT ?", (project_id, limit + 1)).fetchall()
            counts = self._counts(conn, project_id)
        return {"project_id": project_id,
                "entries": [entry_summary(row) for row in rows[:limit]],
                "truncated": len(rows) > limit, **counts}

    @writer_only_read("check_driver_note_index")
    def check_index(self, project_id: str) -> dict:
        """Run the address check over the whole notebook and report what dangles."""
        project_id = key(project_id, "project_id")
        with self.store.transaction(notebook=project_id) as conn:
            self.store._project(conn, project_id)
            note = conn.execute("SELECT * FROM state_driver_notes WHERE project_id=?",
                                (project_id,)).fetchone()
            sources = []
            if note is not None:
                sources.append(("section:permanent", note["permanent_text"]))
                sources.append(("section:temporary", note["temporary_text"]))
            rows = conn.execute(
                "SELECT * FROM state_driver_note_entries WHERE project_id=? "
                "ORDER BY created_at, entry_id", (project_id,)).fetchall()
            for row in rows:
                label = address_of(project_id, row["entry_id"])
                sources.append((f"{label}#assertion", row["assertion"]))
                sources.append((f"{label}#body", row["body"]))
                if row["superseded_by"]:
                    sources.append((f"{label}#superseded_by",
                                    address_of(project_id, row["superseded_by"])))
            dangling = self._unresolved(conn, project_id, sources)
            counts = self._counts(conn, project_id)
        checked = sum(len(referenced_addresses(value)) for _, value in sources)
        return {"project_id": project_id, "ok": not dangling, "addresses_checked": checked,
                "dangling": dangling, **counts}
