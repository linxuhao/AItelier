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
"""
from __future__ import annotations

import functools
import inspect

# THE one classification of State tables. Every ``state_*`` table in the schema
# must appear in exactly one of these sets: a table added to the schema and left
# unclassified fails `tests/unit/test_undeclared_reader_is_untrusted.py`, so
# ``nobody decided`` can never silently mean ``public``. The private set is the
# data whose reads must pass the execution-point verdict.
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
    """Raise the ONE refusal when an explicitly untrusted caller reads ``action``.

    ``trusted is False`` is the only value that refuses, matching
    ``core.state_commands._anonymous``: every other caller - the internal
    driver, MCP, a writer, an embedder that never declared a level - is trusted,
    so this can only NARROW an existing read. The refusal is raised for any
    action the table does not call ``public``, including one nobody classified.
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
