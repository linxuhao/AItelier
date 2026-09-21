"""An INDEPENDENT reader of which action a state route serves.

The route declaration (`_state_route` on the endpoint) is what the router
guard consults - but the declaration is also what the invariant tests walk.
A declaration alone can therefore lie to the guard and to its own checker at
the same time: declare a private-reading handler as a public action and both
agree on the wrong answer.

This module is the second opinion. It reads the endpoint's SOURCE and reports
which state actions the handler actually executes, so a declaration naming an
action the handler never serves is caught twice: by the guard (403 at request
time) and by the suite (the declaration/reader consistency test goes red).
"""
import inspect
import re

# A literal state action passed to the executor: `_call(service, "get_graph", ...)`
# or `execute(service, "get_driver_note", ...)`. Endpoints whose action lives
# in the URL pass a variable, not a literal, and match nothing - by design.
_ACTION_CALL = re.compile(
    r"(?:_call|execute)\(\s*service,\s*['\"]([a-z_]+)['\"]")


def served_actions(endpoint) -> frozenset:
    """The state actions the endpoint's own source executes.

    An endpoint whose source cannot be read (a builtin, a C function) reports
    an empty set: the reader never invents an action it did not see. Empty is
    not an approval - it only means the declaration/reader cross-check has
    nothing to compare for that endpoint.
    """
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        return frozenset()
    return frozenset(_ACTION_CALL.findall(source))
