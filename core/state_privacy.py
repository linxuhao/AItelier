"""The one confidentiality verdict for a WRITER-ONLY READ, taken at execution.

Confidentiality is decided by the ACTION that is being read, at the moment the
read EXECUTES - never by inspecting a route's or a handler's source. The single
classification table is ``core.state_commands.read_visibility`` (unknown action
-> private, so it fails closed); this module turns that classification into one
refusal and applies it from every place a private read can be performed:

* ``core.state_commands.execute``, the chokepoint every transport shares; and
* the service methods themselves - ``@writer_only_read("<action>")`` - so a
  route that bypasses ``execute`` and calls ``service.driver_notes.get(...)``
  (or any other writer-only read) directly is refused by the same function.

The trust level comes from the object that owns the data. ``StateService``
derives it once per request from the raw credential (``api.authz.may_read_private``)
and hands it to every object that can reach a private table. An owner that never
declared a level is UNTRUSTED, exactly like an anonymous transport: trust only
comes from an explicit declaration at a real construction point.

A registered method and a decorated one are still a LIST, and every list has a
layer below it. So the verdict also lives where a private read cannot avoid it:
``read_authorizer`` judges each TABLE a connection touches, and
``TrustBoundDatabase`` installs it from the handle's declared trust. A function
written later, imported by nobody and named in no table, is judged the same way
when it reads ``state_driver_notes``, because the connection it reads through
refuses before any row is returned.
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


class TrustBoundDatabase:
    """A database handle that carries a DECLARED trust level onto every
    connection it hands out.

    This is the layer every private read must pass. ``sqlite3`` calls a
    connection's authorizer for every table a statement touches, so a verdict
    installed here judges a read of a private table no matter WHO wrote the
    reading function, whether it is registered anywhere, or whether a route
    calls it. Nothing about the reader's name, module or decoration is
    consulted - only the table it reads and the trust this handle declared.
    """

    def __init__(self, db, project_read_trusted):
        self.__dict__["_db"] = db
        self.__dict__["_trusted"] = bool(project_read_trusted)

    def __getattr__(self, name):
        # Everything else about the handle is untouched: paths, migrations and
        # every legacy method stay reachable, so only the read verdict changes.
        # `_db` is read out of `__dict__`, never through an attribute lookup: a
        # copy of this object is reconstructed without `__init__`, and delegating
        # for a missing `_db` would recurse forever.
        if name.startswith("_"):
            raise AttributeError(name)
        db = self.__dict__.get("_db")
        if db is None:
            raise AttributeError(name)
        return getattr(db, name)

    @property
    def db_path(self):
        return self._db.db_path

    @property
    def unrestricted(self):
        """The raw handle, for the privacy DECISION itself: opening/closing a
        project and reading who opened it. Only that machinery may use it; a
        read that delivers data goes through ``get_connection``."""
        return self._db

    @contextmanager
    def get_connection(self):
        """A connection that JUDGES the private tables the caller reads.

        This is the layer every private read must pass. ``sqlite3`` calls the
        authorizer for every table a statement touches, so a reader written
        later - registered nowhere, decorated by nobody, named in no table - is
        refused here the moment it reads ``state_driver_notes``. Nothing about
        the reader is consulted, only the table and this handle's trust.
        """
        with self._db.get_connection() as connection:
            verdict = install_read_authorizer(connection, self._trusted)
            try:
                yield connection
            except sqlite3.DatabaseError:
                # sqlite3 turns a denying authorizer into "not authorized",
                # losing which denial it was. The verdict remembers its OWN
                # refusal, so it leaves here as the one refusal every other
                # layer raises - a 403, not a 500 - and an unrelated database
                # error is re-raised untouched.
                if verdict is not None and verdict.denied:
                    from core.state_commands import ProjectPrivate
                    raise ProjectPrivate() from None
                raise

    @contextmanager
    def decision_connection(self):
        """The privacy machinery's own channel, for a DECISION not a delivery.

        Deciding whether a project is open must read the visibility row, and a
        public aggregate carries its event watermark - a counter, never a
        payload. Nothing that leaves through here is record text, and the reads
        that are private ACTIONS (``project_visibility``, ``events``) stay
        refused by the action verdict on their own methods.
        """
        with self._db.get_connection() as connection:
            install_read_authorizer(connection, None)
            yield connection


def read_authorizer(trusted):
    """The callback that judges a table read on the connection.

    A caller that DECLARED trust (the internal driver, MCP with a real
    credential, a test fixture) and the decision channel (``trusted is None``)
    are unrestricted. Everyone else - including a reader that does not exist
    yet - may read PUBLIC tables only: any ``SQLITE_READ`` of a table the one
    classification calls private is DENIED by sqlite3 itself, so a read that
    reaches a private table without passing any list of readers still cannot
    return a row.
    """
    if trusted is True or trusted is None:
        return None

    class ReadVerdict:
        """One denial per connection, remembered so the owner can name it."""

        def __init__(self):
            self.denied = False

        def __call__(self, action, arg1, arg2, db_name, trigger):
            # arg1 is the table name for SQLITE_READ. Only reads are judged: a
            # refusal must never stop the schema work a write needs.
            if action == sqlite3.SQLITE_READ and arg1 in PRIVATE_STATE_TABLES:
                self.denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

    return ReadVerdict()


def install_read_authorizer(connection, trusted):
    """Install the table verdict on ``connection`` and hand it back.

    It is returned so the handle that owns the connection can translate
    sqlite3's own refusal into the one refusal; see
    ``TrustBoundDatabase.get_connection``.
    """
    verdict = read_authorizer(trusted)
    connection.set_authorizer(verdict)
    return verdict
