"Project-scoped, revisioned director handoff notes stored beside State facts."
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.state_driver_index import (ENTRY_SCHEMA, DRIVER_ENTRY_SCHEMA, index_line, MAX_INDEX_LIMIT, address_of,
                                     assertion_text, body_text, entry_detail,
                                     entry_id_value, entry_summary, force_value,
                                     landed_text, new_entry_id, reason_text,
                                     referenced_addresses)
from core.state_graph import (StateConflict, StateGraphError, StateNotFound, key, now,
                              text)
from core.state_privacy import UntrustedDatabase, writer_only_read

_DEFAULT_FORCE = object()
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


# Owner ruling 2026-10-06 (node driver.note-has-no-free-text): the notebook holds
# ONLY entries. The permanent/temporary sections are closed to writes and are no
# longer part of get_driver_note; their past text stays readable through
# driver_note_history and search_driver_note_history, and no row is dropped.
FREE_TEXT_CLOSED = (
    "update_driver_note is closed: the director notebook has no free-text sections "
    "(owner ruling 2026-10-06). Write a rule with write_driver_note_entry instead. In-flight "
    "state does not go into the notebook: it lives in the State DAG (attempts, node hold, "
    "issues, node priority). Past section text stays readable via driver_note_history and "
    "search_driver_note_history.")
NOTE_AFTER_REVISION_RETIRED = (
    "note_after_revision is retired: the driver notebook revision no longer advances "
    "(owner ruling 2026-10-06, entries only), so a wait on it could never wake. Wait on "
    "director messages, or on the project's driver_note_updated events that every entry "
    "write, supersede and delist emits, instead.")


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
        conn.executescript(SCHEMA + ENTRY_SCHEMA + DRIVER_ENTRY_SCHEMA)
        conn.commit()


class StateDriverNotes:
    """One CAS-protected notebook per State project; State remains authoritative."""

    def __init__(self, store, actor: str, project_read_trusted: bool = False,
                 driver_scope=None, caller_driver_id=None, break_glass_reason=None):
        self.store = store
        self.driver_scope = driver_scope
        self.caller_driver_id = caller_driver_id
        self.break_glass_reason = break_glass_reason
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

    def for_driver(self, driver_id, caller_driver_id=None, break_glass_reason=None):
        return type(self)(self.store, self.actor, self.project_read_trusted,
                          key(driver_id, "driver_id"), caller_driver_id, break_glass_reason)

    def _transaction(self, *, write=False, notebook=None):
        if self.driver_scope:
            if not self.project_read_trusted:
                raise StateGraphError("private_notebook")
            with self.store.transaction() as conn:
                caller = conn.execute("SELECT is_admin,status FROM drivers WHERE driver_id=?",
                                      (self.caller_driver_id,)).fetchone()
                owner = self.actor.startswith("owner:")
                if not owner and (caller is None or caller["status"] != "active"):
                    raise StateGraphError("private_notebook")
                if self.caller_driver_id == "public" and not conn.execute(
                        "SELECT 1 FROM project_drivers WHERE driver_id='public' AND status='member'").fetchone():
                    raise StateGraphError("private_notebook")
                if write and self.caller_driver_id != self.driver_scope:
                    if not self.break_glass_reason or not (owner or caller["is_admin"]):
                        raise StateGraphError("not_notebook_owner")
            return self.store.transaction(write=write)
        return self.store.transaction(write=write, notebook=notebook)

    def _entity(self, conn, scope_id):
        if not self.driver_scope:
            return self.store._project(conn, scope_id)
        if scope_id != self.driver_scope or not conn.execute(
                "SELECT 1 FROM drivers WHERE driver_id=?", (scope_id,)).fetchone():
            raise StateNotFound("unknown driver notebook")

    def _execute(self, conn, sql, params=()):
        if self.driver_scope:
            sql = sql.replace("state_driver_note_entries", "driver_note_entries").replace("project_id", "driver_id")
        cursor = conn.execute(sql, params)
        if self.driver_scope:
            def scoped_row(cur, values):
                row = dict(zip((c[0] for c in cur.description), values))
                if "driver_id" in row:
                    row["project_id"] = row["driver_id"]
                return row
            cursor.row_factory = scoped_row
        return cursor

    def _address(self, scope_id, entry_id):
        return ("dnote" if self.driver_scope else "note") + "://" + scope_id + "/" + entry_id

    def _force(self, value):
        if self.driver_scope:
            value = "informational" if value is _DEFAULT_FORCE else value
            if value not in ("informational", "in_force"):
                raise StateGraphError("invalid force")
            return value
        return force_value("in_force" if value is _DEFAULT_FORCE else value)

    def _event(self, conn, project_id, node_key, event_type, payload):
        if not self.driver_scope:
            self.store._event(conn, project_id, node_key, event_type, payload)

    def _presentation(self, value):
        if not self.driver_scope:
            return value
        if isinstance(value, dict):
            return {("driver_id" if k == "project_id" else k): self._presentation(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self._presentation(v) for v in value]
        return value

    def _detail(self, row):
        detail = entry_detail(row)
        detail["address"] = self._address(row["project_id"], row["entry_id"])
        if row["superseded_by"]:
            detail["superseded_by"] = self._address(row["project_id"], row["superseded_by"])
        detail["index_line"] = self._line(row)
        return self._presentation(detail)

    def _summary(self, row):
        detail = self._detail(row)
        return {k:v for k,v in detail.items() if k not in ("body","project_id","driver_id","director_identity")}

    def _line(self, row):
        line = index_line(row)
        if self.driver_scope:
            line = line.replace("note://" + row["project_id"] + "/", "dnote://" + row["project_id"] + "/")
        return line

    @writer_only_read("get_driver_note")
    def get(self, project_id: str, include_superseded: bool = False,
            include_delisted: bool = False) -> dict:
        """The entry index, its counts and the revision; never section text."""
        project_id = key(project_id, "project_id")
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            row = None if self.driver_scope else self._execute(conn, "SELECT revision FROM state_driver_notes WHERE project_id=?",
                               (project_id,)).fetchone()
            index = self._index_projection(conn, project_id, include_superseded,
                                           include_delisted)
        return self._presentation({"project_id": project_id, "revision": row["revision"] if row else 0, **index})

    @writer_only_read("driver_note_history")
    def history(self, project_id: str, after_revision: int = 0, limit: int = 100) -> dict:
        project_id = key(project_id, "project_id")
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            rows = self._execute(conn, 
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
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            rows = self._execute(conn, 
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

    # ---- index mode: short assertions on the index, bodies fetched by address ----

    def _unresolved(self, conn, project_id: str, sources) -> list[dict]:
        """Every note:// address in ``sources`` that does not resolve to a body.

        This is the check that bites. It is not a comment and not a snapshot of
        today's data: it runs on every entry write and on demand, and it reports
        the exact dangling addresses.
        """
        dangling = []
        for label, value in sources:
            if not self.driver_scope and "dnote://" in value:
                dangling.append({"source":label,"address":"dnote://","reason":"private notebook reference forbidden"})
            if self.driver_scope:
                for match in re.finditer(r"(d?note)://([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([0-9a-f]{12})", value):
                    prefix, scope, entry = match.groups()
                    table, column = ("driver_note_entries","driver_id") if prefix == "dnote" else ("state_driver_note_entries","project_id")
                    if not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? AND entry_id=?", (scope,entry)).fetchone():
                        dangling.append({"source":label,"address":match.group(0),"reason":"no entry at this address"})
                for attempt in re.findall(r"\battempt-[0-9a-f]{32}\b", value):
                    if not conn.execute("SELECT 1 FROM state_attempts WHERE attempt_id=?", (attempt,)).fetchone():
                        dangling.append({"source":label,"address":attempt,"reason":"unknown State attempt"})
                for match in re.finditer(r"(attempt|issue|claim|subagent)://([A-Za-z0-9._-]+)", value):
                    kind, identifier = match.groups()
                    table, column = {"attempt":("state_attempts","attempt_id"),"issue":("state_issues","issue_id"),
                                     "claim":("state_node_claims","claim_id"),"subagent":("driver_subagents","subagent_id")}[kind]
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() or not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (identifier,)).fetchone():
                        dangling.append({"source":label,"address":match.group(0),"reason":"unknown State reference"})
                for match in re.finditer(r"node://([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)", value):
                    if not conn.execute("SELECT 1 FROM state_nodes WHERE project_id=? AND node_key=?", match.groups()).fetchone():
                        dangling.append({"source":label,"address":match.group(0),"reason":"unknown State node"})
                continue
            for ref_project, ref_entry in referenced_addresses(value):
                address = f"note://{ref_project}/{ref_entry}"
                if ref_project != project_id:
                    dangling.append({"source": label, "address": address,
                                     "reason": "address leaves this project's notebook"})
                    continue
                found = self._execute(conn, 
                    "SELECT 1 FROM state_driver_note_entries WHERE project_id=? AND entry_id=?",
                    (project_id, ref_entry)).fetchone()
                if found is None:
                    dangling.append({"source": label, "address": address,
                                     "reason": "no entry at this address"})
        return dangling

    def _entry(self, conn, project_id: str, entry_id: str):
        row = self._execute(conn, 
            "SELECT * FROM state_driver_note_entries WHERE project_id=? AND entry_id=?",
            (project_id, entry_id)).fetchone()
        if row is None:
            raise StateNotFound(f"no driver note entry at {self._address(project_id, entry_id)}")
        return row

    def _counts(self, conn, project_id: str) -> dict:
        # superseded_count is project-wide (listed or delisted), like the other
        # counts: the default index hides superseded entries, and this is the
        # number it hides, so a short index cannot hide how much it left out.
        row = self._execute(conn, 
            "SELECT COUNT(*) AS total, "
            "SUM(listing='listed') AS listed, SUM(listing='delisted') AS delisted, "
            "SUM(superseded_by IS NOT NULL) AS superseded "
            "FROM state_driver_note_entries WHERE project_id=?", (project_id,)).fetchone()
        return {"entry_count": row["total"] or 0, "listed_count": row["listed"] or 0,
                "delisted_count": row["delisted"] or 0,
                "superseded_count": row["superseded"] or 0}

    @staticmethod
    def _visible(include_superseded: bool, include_delisted: bool) -> str:
        """The SQL filter shared by every index read: current and listed by default."""
        clause = "" if include_delisted else " AND listing='listed'"
        if not include_superseded:
            clause += " AND superseded_by IS NULL"
        return clause

    def _index_projection(self, conn, project_id: str, include_superseded: bool = False,
                          include_delisted: bool = False) -> dict:
        """The part of the notebook that is cheap enough to inject everywhere.

        Bodies are NOT here; each line carries the address that fetches its body.
        By default only current, listed entries are here; delisted_count and
        superseded_count say how many lines the default leaves out.
        """
        rows = self._execute(conn, 
            "SELECT * FROM state_driver_note_entries WHERE project_id=?"
            + self._visible(include_superseded, include_delisted)
            + " ORDER BY created_at, entry_id", (project_id,)).fetchall()
        counts = self._counts(conn, project_id)
        # Counts come BEFORE the list and every item carries its position i, so a
        # reader whose copy was truncated can tell how many lines it did not see.
        return {**counts, "index_count": len(rows),
                "index": [{"i": i, "address": self._address(project_id, row["entry_id"]),
                           "index_line": self._line(row), "force": row["force"],
                           "lifecycle": "superseded" if row["superseded_by"] else "current",
                           "superseded_by": (self._address(project_id, row["superseded_by"])
                                             if row["superseded_by"] else None)}
                          for i, row in enumerate(rows)]}

    def _write_entry(self, conn, project_id, assertion, body, director_identity,
                     force, landed, timestamp) -> str:
        if not self.driver_scope and ("dnote://" in assertion or "dnote://" in body):
            raise StateGraphError("private_notebook_reference")
        entry_id = new_entry_id()
        dangling = self._unresolved(conn, project_id, [("body", body), ("assertion", assertion)])
        if dangling:
            raise StateGraphError(
                "driver note entry references addresses that do not resolve: "
                + "; ".join(f"{item['address']} ({item['reason']})" for item in dangling))
        self._execute(conn, 
            "INSERT INTO state_driver_note_entries(project_id,entry_id,assertion,body,force,"
            "landed,listing,superseded_by,supersede_reason,delist_reason,actor,director_identity,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,'listed',NULL,'','',?,?,?,?)",
            (project_id, entry_id, assertion, body, force, landed, self.actor,
             director_identity, timestamp, timestamp))
        return entry_id

    @writer_only_read("get_driver_note_entry")
    def write_entry(self, project_id: str, assertion: str, body: str, director_identity: str,
                    force=_DEFAULT_FORCE, landed: str = "") -> dict:
        """Write one assertion plus its body and return the address of both."""
        project_id = key(project_id, "project_id")
        assertion = assertion_text(assertion)
        body = body_text(body)
        force = self._force(force)
        landed = landed_text(landed)
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self._transaction(write=True) as conn:
            self._entity(conn, project_id)
            entry_id = self._write_entry(conn, project_id, assertion, body,
                                         director_identity, force, landed, timestamp)
            row = self._entry(conn, project_id, entry_id)
            projection = self._index_projection(conn, project_id)
            self._event(conn, project_id, None, "driver_note_updated", {
                "operation": "write_entry", "entry_id": entry_id,
                "address": self._address(project_id, entry_id), "actor": self.actor,
                "director_identity": director_identity})
            return {**self._detail(row), **projection}

    @writer_only_read("get_driver_note_entry")
    def supersede_entry(self, project_id: str, entry_id: str, assertion: str, body: str,
                        reason: str, director_identity: str, force=_DEFAULT_FORCE,
                        landed: str = "") -> dict:
        """Retire an assertion IN PLACE: the old address keeps a tombstone, the old body stays."""
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        assertion = assertion_text(assertion)
        body = body_text(body)
        reason = reason_text(reason, "reason")
        force = self._force(force)
        landed = landed_text(landed)
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self._transaction(write=True) as conn:
            self._entity(conn, project_id)
            previous = self._entry(conn, project_id, entry_id)
            if previous["superseded_by"]:
                raise StateConflict(
                    f"{self._address(project_id, entry_id)} is already superseded by "
                    f"{self._address(project_id, previous['superseded_by'])}; supersede that one")
            successor_id = self._write_entry(conn, project_id, assertion, body,
                                             director_identity, force, landed, timestamp)
            self._execute(conn, 
                "UPDATE state_driver_note_entries SET superseded_by=?,supersede_reason=?,"
                "updated_at=? WHERE project_id=? AND entry_id=?",
                (successor_id, reason, timestamp, project_id, entry_id))
            retired = self._entry(conn, project_id, entry_id)
            successor = self._entry(conn, project_id, successor_id)
            projection = self._index_projection(conn, project_id)
            self._event(conn, project_id, None, "driver_note_updated", {
                "operation": "supersede_entry", "entry_id": entry_id,
                "successor_entry_id": successor_id,
                "address": self._address(project_id, successor_id), "actor": self.actor,
                "director_identity": director_identity})
            return {"superseded": self._detail(retired), "successor": self._detail(successor),
                    **projection}

    @writer_only_read("get_driver_note_entry")
    def delist_entry(self, project_id: str, entry_id: str, reason: str,
                     director_identity: str) -> dict:
        """Evict a line from the index without deleting its body.

        Every entry is a rule, so this is how an obsolete rule with no successor
        leaves the index (owner ruling 2026-10-06, node driver.note-rules-pruned):
        the reason is stored, the body still resolves at its address and
        delisted_count reports the eviction.
        """
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        reason = reason_text(reason, "reason")
        director_identity = text(director_identity, "director_identity", 320)
        timestamp = now()
        with self._transaction(write=True) as conn:
            self._entity(conn, project_id)
            row = self._entry(conn, project_id, entry_id)
            if row["listing"] == "delisted":
                raise StateConflict(
                    f"{self._address(project_id, entry_id)} is already delisted; its body stays "
                    "readable at that address")
            self._execute(conn, 
                "UPDATE state_driver_note_entries SET listing='delisted',delist_reason=?,"
                "updated_at=? WHERE project_id=? AND entry_id=?",
                (reason, timestamp, project_id, entry_id))
            delisted = self._entry(conn, project_id, entry_id)
            projection = self._index_projection(conn, project_id)
            self._event(conn, project_id, None, "driver_note_updated", {
                "operation": "delist_entry", "entry_id": entry_id,
                "address": self._address(project_id, entry_id),
                "delisted_count": projection["delisted_count"], "actor": self.actor,
                "director_identity": director_identity})
            return {**self._detail(delisted), **projection}

    @writer_only_read("get_driver_note_entry")
    def get_entry(self, project_id: str, entry_id: str) -> dict:
        """Fetch one body by address. Bodies are never injected; they are fetched."""
        project_id = key(project_id, "project_id")
        entry_id = entry_id_value(entry_id)
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            return self._detail(self._entry(conn, project_id, entry_id))

    @writer_only_read("driver_note_index")
    def entry_index(self, project_id: str, include_delisted: bool = False,
                    limit: int = 100, include_superseded: bool = False) -> dict:
        project_id = key(project_id, "project_id")
        if type(limit) is not int or not 1 <= limit <= MAX_INDEX_LIMIT:
            raise StateGraphError(f"limit must be an integer between 1 and {MAX_INDEX_LIMIT}")
        clause = self._visible(include_superseded, include_delisted)
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            rows = self._execute(conn, 
                "SELECT * FROM state_driver_note_entries WHERE project_id=?" + clause +
                " ORDER BY created_at, entry_id LIMIT ?", (project_id, limit + 1)).fetchall()
            counts = self._counts(conn, project_id)
        return {"project_id": project_id, **counts,
                "index_count": len(rows[:limit]), "truncated": len(rows) > limit,
                "entries": [{"i": i, **self._summary(row)}
                            for i, row in enumerate(rows[:limit])]}

    @writer_only_read("search_driver_note_entries")
    def search_entries(self, project_id: str, query: str = "", limit: int = 20,
                       after: str | None = None, excerpt_chars: int = 320,
                       include_superseded: bool = False,
                       include_delisted: bool = False) -> dict:
        """Search entry assertions and bodies; stable (created_at, entry_id) order.

        Matching is the same Unicode case-insensitive literal test as
        search_driver_note_history. ``after`` is the entry_id of the last hit of
        the previous page; created_at is immutable, so the cursor never moves.
        """
        project_id = key(project_id, "project_id")
        if not isinstance(query, str) or len(query) > MAX_SEARCH_QUERY_CHARS:
            raise StateGraphError(
                f"query must be text of at most {MAX_SEARCH_QUERY_CHARS} characters")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise StateGraphError(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if type(excerpt_chars) is not int or not 64 <= excerpt_chars <= MAX_EXCERPT_CHARS:
            raise StateGraphError(
                f"excerpt_chars must be an integer between 64 and {MAX_EXCERPT_CHARS}")
        if after is not None:
            after = entry_id_value(after)
        clause = self._visible(include_superseded, include_delisted)
        selected = []
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            args: list[object] = [project_id]
            if after is not None:
                cursor = self._entry(conn, project_id, after)
                clause += " AND (created_at>? OR (created_at=? AND entry_id>?))"
                args += [cursor["created_at"], cursor["created_at"], after]
            rows = self._execute(conn, 
                "SELECT * FROM state_driver_note_entries WHERE project_id=?" + clause +
                " ORDER BY created_at, entry_id", args)
            for row in rows:
                matched_in = [field for field in ("assertion", "body")
                              if _matches(row[field], query)]
                if not matched_in:
                    continue
                selected.append((row, matched_in))
                if len(selected) > limit:
                    break
        entries = [{
            "address": self._address(project_id, row["entry_id"]), "entry_id": row["entry_id"],
            "force": row["force"],
            "lifecycle": "superseded" if row["superseded_by"] else "current",
            "superseded_by": (self._address(project_id, row["superseded_by"])
                              if row["superseded_by"] else None),
            "listing": row["listing"], "assertion": row["assertion"],
            "matched_in": matched_in,
            "excerpt": _excerpt(row["body"] if "body" in matched_in else row["assertion"],
                                query, excerpt_chars),
        } for row, matched_in in selected[:limit]]
        truncated = len(selected) > limit
        return {"project_id": project_id, "entries": entries, "truncated": truncated,
                "next_after": entries[-1]["entry_id"] if truncated else None}

    @writer_only_read("check_driver_note_index")
    def check_index(self, project_id: str) -> dict:
        """Run the address check over the whole notebook and report what dangles."""
        project_id = key(project_id, "project_id")
        with self._transaction(notebook=project_id) as conn:
            self._entity(conn, project_id)
            note = None if self.driver_scope else self._execute(conn, "SELECT * FROM state_driver_notes WHERE project_id=?",
                                (project_id,)).fetchone()
            sources = []
            if note is not None:
                sources.append(("section:permanent", note["permanent_text"]))
                sources.append(("section:temporary", note["temporary_text"]))
            rows = self._execute(conn, 
                "SELECT * FROM state_driver_note_entries WHERE project_id=? "
                "ORDER BY created_at, entry_id", (project_id,)).fetchall()
            for row in rows:
                label = self._address(project_id, row["entry_id"])
                sources.append((f"{label}#assertion", row["assertion"]))
                sources.append((f"{label}#body", row["body"]))
                if row["superseded_by"]:
                    sources.append((f"{label}#superseded_by",
                                    self._address(project_id, row["superseded_by"])))
            dangling = self._unresolved(conn, project_id, sources)
            counts = self._counts(conn, project_id)
        checked = sum(len(referenced_addresses(value)) for _, value in sources)
        return {"project_id": project_id, "ok": not dangling, "addresses_checked": checked,
                "dangling": dangling, **counts}
