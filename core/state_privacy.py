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
and hands it to the objects it builds. An owner that never declared a level is
treated as trusted, which only ever NARROWS a read when a transport explicitly
opted in - the same default ``core.state_commands._anonymous`` uses.
"""
from __future__ import annotations

import functools
import inspect


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
    """The declared trust level of the object that owns a read."""
    trust = getattr(owner, "project_read_trusted", None)
    if isinstance(trust, bool):
        return trust
    service = getattr(owner, "service", None)
    trust = getattr(service, "project_read_trusted", None)
    if isinstance(trust, bool):
        return trust
    return True


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
        return wrapper
    return decorate
