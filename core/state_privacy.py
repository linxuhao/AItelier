"""The confidentiality verdicts for State reads: the ACTION, and the ROWS.

1. The ACTION verdict. Confidentiality of a read is decided by the action being
   read, at the moment the read EXECUTES - never by inspecting a route's or a
   handler's source. The single classification table is
   ``core.state_commands.read_visibility`` (unknown action -> private, so it
   fails closed); ``refuse_private_read`` turns it into one refusal, applied at
   ``core.state_commands.execute`` and inside the service methods themselves
   (``@writer_only_read("<action>")``).

2. The ROW verdict. An untrusted caller's objects hold exactly one kind of
   database handle, ``UntrustedDatabase``: a PATH and the rules below, and no
   other handle. Every connection it opens is armed before anyone sees it, so a
   reader written later - registered nowhere, decorated by nobody, named in no
   table - that reaches a private table through ``svc``, ``svc.store``,
   ``svc.db`` or anything rebuilt from them reads through such a connection:

   * a PUBLIC state table is readable;
   * a PRIVATE state table - and the host's ``run_isolation`` - is never read
     from the database itself. Where a public read needs part of one, the
     connection carries a copy made before
     it was armed that holds only that part: the visibility of opened projects
     (``state_project_access``), the event watermark of opened projects
     (``state_events``), the provenance rows of opened projects' runs
     (``run_isolation``), and - only when a notebook read asks for one
     project's notebook - that project's notebook rows if and only if the
     project is opened. Columns outside that part are refused;
   * every other table, every write, every schema change, ATTACH and every
     pragma except ``foreign_keys``/``busy_timeout`` are refused (default deny).

   The decision data (which projects are opened) is therefore computed on a
   connection nobody else holds, before the authorizer is installed, and
   handed to the caller as rows - never as a channel. There is no privacy
   decision method, unrestricted handle or write path on an untrusted object.

   NOT guaranteed by this verdict (director's scope ruling, 2026-09-23): code
   holding an armed connection can call that connection's own methods -
   ``set_authorizer(None)``, ``backup(...)``, ``blobopen(...)`` - and any code
   in the process can reopen the database file by its path or declare trust
   at a construction point. Closing those needs a process or file boundary.
   PUBLIC state tables are judged by table, not by project: an armed
   connection reads their rows for unopened projects too.

The trust level comes from the credential at a real construction point:
``StateService`` derives it once per request from the raw credential
(``api.authz.may_read_private``). An owner that never declared a level is
UNTRUSTED, and a store built on an ``UntrustedDatabase`` is untrusted whatever
it declares, because the handle - not the flag - decides which rows exist.
"""
from __future__ import annotations

from contextlib import contextmanager
import functools
import inspect
import sqlite3

# THE one classification of State tables. Every ``state_*`` table in the schema
# must appear in exactly one of these sets: a table added to the schema and left
# unclassified fails `tests/unit/test_undeclared_reader_is_untrusted.py`, so
# ``nobody decided`` can never silently mean ``public``. The private set is the
# data whose reads must pass the execution-point verdict AND the connection
# verdict.
PRIVATE_STATE_TABLES = frozenset({
    "state_driver_notes", "state_driver_note_revisions", "state_driver_note_entries",
    "state_events", "state_project_access",
    "state_director_messages", "state_director_deliveries",
    "state_director_inbox_sequences", "state_director_idempotency",
})

PUBLIC_STATE_TABLES = frozenset({
    "state_projects", "state_nodes", "state_dependencies", "state_node_revisions",
    "state_attempts", "state_evidence", "state_acceptances",
    "state_external_observations", "state_external_report_blobs", "state_external_owners",
    "state_design_revisions", "state_design_baselines", "state_design_heads",
    "state_design_bindings", "state_issues", "state_issue_nodes",
    "state_project_policy", "state_node_holds", "state_source_bindings",
    "state_history_links",
})


def is_private_table(name: str) -> bool:
    """True only for a table the one classification calls private."""
    return name in PRIVATE_STATE_TABLES


def _read_visibility(action: str) -> str:
    from core.state_commands import read_visibility
    return read_visibility(action)


def refuse_private_read(trusted, action: str) -> None:
    """Raise the ONE refusal when an untrusted caller reads ``action``.

    ``trusted is False`` is the only value that refuses, matching
    ``core.state_commands._anonymous`` and ``trust_of`` below: only an explicit
    ``True`` is trusted, and an owner that never declared a level reads as
    False. The refusal is raised for any action the table does not call
    ``public``, including one nobody classified.
    """
    if trusted is False and _read_visibility(action) != "public":
        from core.state_commands import ProjectPrivate
        raise ProjectPrivate()


def trust_of(owner) -> bool:
    """The declared trust level of the object that owns a read.

    Only an explicit ``True`` is trusted. An object that never declared a level
    is UNTRUSTED, so a leaf reconstructed from an untrusted store or db cannot
    become trusted by staying silent - the same rule ``StateService`` applies to
    its own construction.
    """
    trust = getattr(owner, "project_read_trusted", None)
    if isinstance(trust, bool):
        return trust
    service = getattr(owner, "service", None)
    trust = getattr(service, "project_read_trusted", None)
    if isinstance(trust, bool):
        return trust
    return False


def writer_only_read(action: str):
    """Mark a method as EXECUTING the writer-only read ``action``.

    The refusal is raised inside the method, from the owning object's own trust
    level, so it does not depend on how - or whether - a route reaches it. It is
    the same ``refuse_private_read`` call ``execute`` makes, which is what makes
    the verdict one function rather than one per call site.
    """
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapper(self, *args, **kwargs):
                refuse_private_read(trust_of(self), action)
                return await function(self, *args, **kwargs)
        else:
            @functools.wraps(function)
            def wrapper(self, *args, **kwargs):
                refuse_private_read(trust_of(self), action)
                return function(self, *args, **kwargs)
        wrapper.__state_read_action__ = action
        # `functools.wraps` copies `__wrapped__`, which would expose the
        # undecorated body - a bypass around the verdict. Drop it and preserve the
        # signature explicitly so introspection still reports the real parameters.
        try:
            del wrapper.__wrapped__
        except AttributeError:  # pragma: no cover - wraps always sets it
            pass
        try:
            wrapper.__signature__ = inspect.signature(function)
        except (TypeError, ValueError):  # pragma: no cover - builtins only
            pass
        return wrapper
    return decorate


class UntrustedDatabase:
    """The database handle of a caller that did not prove it may read private
    records. It holds the database path and nothing else.

    There is no attribute that returns another handle, no privacy-decision
    channel and no write path: every connection comes from ``get_connection``
    and is armed (see ``_arm``) before it is yielded.
    """

    __slots__ = ("_path",)

    def __init__(self, db_path):
        self._path = str(db_path)

    @property
    def db_path(self) -> str:
        return self._path

    @contextmanager
    def get_connection(self, notebook: str | None = None):
        """An armed connection. ``notebook`` names the one project whose
        notebook rows a notebook read needs; they are present only when that
        project is opened."""
        connection = sqlite3.connect(self._path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            verdict = _arm(connection, notebook)
            try:
                yield connection
            except sqlite3.DatabaseError:
                # sqlite3 reports a denying authorizer as "not authorized",
                # losing which denial it was. The verdict remembers its own
                # refusal, so it leaves here as the one refusal every other
                # layer raises; an unrelated database error is re-raised.
                if verdict.denied:
                    from core.state_commands import ProjectPrivate
                    raise ProjectPrivate() from None
                raise
        finally:
            connection.close()


_OPENED = "SELECT project_id FROM main.state_project_access WHERE visibility='public'"

# The public part of each private (or host) table an untrusted connection may
# read: the rows a public read needs, and the columns that may be read. Each is
# COPIED into a temp table of the same name before the connection is armed, so
# the unqualified name resolves to the copy and the database's own table is
# never readable on that connection.
PROJECTIONS = {
    "state_project_access": (
        "SELECT project_id, visibility, NULL AS opened_by, NULL AS opened_at, "
        "NULL AS changed_by, NULL AS changed_at FROM main.state_project_access "
        "WHERE visibility='public'",
        frozenset({"project_id", "visibility"})),
    "state_events": (
        "SELECT MAX(seq) AS seq, project_id, NULL AS node_key, NULL AS event_type, "
        "NULL AS payload_json, NULL AS created_at FROM main.state_events "
        f"WHERE project_id IN ({_OPENED}) GROUP BY project_id",
        frozenset({"seq", "project_id"})),
    "run_isolation": (
        "SELECT * FROM main.run_isolation WHERE run_id IN (SELECT run_id FROM "
        f"main.state_attempts WHERE project_id IN ({_OPENED}))",
        None),
}

# The notebook, copied for ONE project and only when that project is opened
# (owner ruling 2026-09-22, note://aitelier/546f3b521eca).
NOTEBOOK_TABLES = ("state_driver_notes", "state_driver_note_revisions",
                   "state_driver_note_entries")

# sqlite's own schema tables and the pure JSON table-valued functions.
_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})
_PURE_FUNCTIONS = frozenset({"json_each", "json_tree"})
_ALLOWED_PRAGMAS = frozenset({"foreign_keys", "busy_timeout"})
_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT, getattr(sqlite3, "SQLITE_RECURSIVE", 33)})


def _arm(connection, notebook):
    """Copy the public parts in, then install the verdict. Nothing is yielded
    to a caller before the verdict is installed."""
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA foreign_keys=ON")
    present = {row[0] for row in connection.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table'")}
    readable = {}
    for table, (select, columns) in PROJECTIONS.items():
        # run_isolation is a host table: a State-only database has none.
        if table in present:
            connection.execute(f"CREATE TEMP TABLE {table} AS {select}")
            readable[table] = columns
    if notebook is not None:
        opened = connection.execute(
            "SELECT 1 FROM main.state_project_access WHERE project_id=? "
            "AND visibility='public'", (notebook,)).fetchone() is not None
        for table in NOTEBOOK_TABLES:
            connection.execute(
                f"CREATE TEMP TABLE {table} AS SELECT * FROM main.{table} WHERE 0")
            if opened:
                connection.execute(
                    f"INSERT INTO temp.{table} SELECT * FROM main.{table} WHERE project_id=?",
                    (notebook,))
            readable[table] = None
    connection.commit()
    verdict = ReadVerdict(readable)
    connection.set_authorizer(verdict)
    return verdict


class ReadVerdict:
    """The authorizer of an untrusted connection: default deny.

    ``readable`` maps each temp copy on this connection to the columns that may
    be read from it (None: every column). The verdict remembers that it denied,
    so the handle can raise the one refusal instead of a database error.
    """

    def __init__(self, readable: dict):
        self.readable = readable
        self.denied = False

    def _deny(self):
        self.denied = True
        return sqlite3.SQLITE_DENY

    def __call__(self, action, arg1, arg2, db_name, _source):
        if action == sqlite3.SQLITE_READ:
            table, column = arg1, arg2
            if table in self.readable:
                # An unqualified name (db_name None) resolves to the temp copy,
                # which shadows the database's table; "main" is the table itself.
                columns = self.readable[table]
                if db_name in ("temp", None) and (columns is None or column in columns
                                                  or column == ""):
                    return sqlite3.SQLITE_OK
                return self._deny()
            if table in PUBLIC_STATE_TABLES and db_name in ("main", None):
                return sqlite3.SQLITE_OK
            if table in _SCHEMA_TABLES and db_name in ("main", None):
                return sqlite3.SQLITE_OK
            if table in _PURE_FUNCTIONS:
                return sqlite3.SQLITE_OK
            return self._deny()
        if action in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 in _ALLOWED_PRAGMAS:
            return sqlite3.SQLITE_OK
        return self._deny()
