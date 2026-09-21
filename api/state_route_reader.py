"""An INDEPENDENT reader of which action a state route serves.

The route declaration (`_state_route` on the endpoint) is what the router
guard consults - but the declaration is also what the invariant tests walk.
A declaration alone can therefore lie to the guard and to its own checker at
the same time: declare a private-reading handler as a public action and both
agree on the wrong answer.

This module is the second opinion. It parses the endpoint's own SOURCE and
reports HOW that endpoint reaches the state executor: with a LITERAL action
name, with an action DISPATCHED from one of the endpoint's own parameters, or
with NO_ACTION at all. Anything it cannot parse into one of those three is
UNREADABLE, and `api.state_http` refuses an unreadable route - reading nothing
is not permission to serve anything.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field

# The four readings. UNREADABLE is not a fourth flavour of "fine": it is the
# answer this module gives when it cannot say what the endpoint executes, and
# the guard turns it into a refusal.
UNREADABLE = "unreadable"
LITERAL = "literal"
DISPATCH = "dispatch"
NO_ACTION = "no_action"

# The two names a state endpoint may reach the executor through. `_call` is the
# router's local wrapper; `execute` is `core.state_commands.execute` itself.
_EXECUTORS = frozenset({"_call", "execute"})


@dataclass(frozen=True)
class Reading:
    """What the endpoint's own source says it executes.

    `kind` is one of the four constants above. `actions` carries the literal
    action names for a LITERAL reading; `parameter` carries the name of the
    endpoint parameter the action comes from for a DISPATCH reading.
    """

    kind: str
    actions: frozenset = field(default_factory=frozenset)
    parameter: str | None = None


def _executor_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _action_arguments(tree: ast.AST) -> list[ast.AST]:
    """Every expression handed to an executor as the ACTION argument.

    Two call shapes reach the executor in this repository: a direct
    `_call(service, <action>, ...)` / `execute(service, <action>, ...)`, and a
    deferred `partial(_call, service, <action>, ...)` that a worker thread
    calls later. Both are collected here, so neither can quietly drop out of
    the reading by moving between them.
    """
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _executor_name(node.func)
        if name in _EXECUTORS and len(node.args) >= 2:
            found.append(node.args[1])
        elif name == "partial" and len(node.args) >= 3:
            deferred = _executor_name(node.args[0])
            if deferred in _EXECUTORS:
                found.append(node.args[2])
    return found


def read_route(endpoint) -> Reading:
    """Read the endpoint's own source and report how it reaches the executor.

    The result is deliberately three-valued plus a failure: an endpoint that
    executes literal actions reads LITERAL, an endpoint that executes an action
    taken from one of its own parameters reads DISPATCH and names that
    parameter, an endpoint that reaches the executor nowhere reads NO_ACTION,
    and everything else - source that cannot be fetched or parsed, an action
    expression that is neither a literal nor one of the endpoint's parameters,
    a mix of literal and dispatched actions, or two different dispatch
    parameters - reads UNREADABLE.
    """
    try:
        source = textwrap.dedent(inspect.getsource(endpoint))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, IndentationError):
        return Reading(UNREADABLE)
    try:
        parameters = set(inspect.signature(endpoint).parameters)
    except (TypeError, ValueError):
        return Reading(UNREADABLE)

    literals: set[str] = set()
    dispatched: set[str] = set()
    for argument in _action_arguments(tree):
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            literals.add(argument.value)
        elif isinstance(argument, ast.Name) and argument.id in parameters:
            dispatched.add(argument.id)
        else:
            return Reading(UNREADABLE)

    if literals and dispatched:
        return Reading(UNREADABLE)
    if len(dispatched) > 1:
        return Reading(UNREADABLE)
    if dispatched:
        return Reading(DISPATCH, frozenset(), next(iter(dispatched)))
    if literals:
        return Reading(LITERAL, frozenset(literals))
    return Reading(NO_ACTION)


def served_actions(endpoint) -> frozenset:
    """The LITERAL state actions the endpoint's own source executes.

    An endpoint whose source cannot be read (a builtin, a C function) reports
    an empty set: the reader never invents an action it did not see. Empty is
    not an approval - it only means the declaration/reader cross-check has
    nothing to compare for that endpoint, and `api.state_http` refuses that
    route rather than waving it through. Callers that need to tell "read
    nothing" apart from "executes nothing" must use `read_route`.
    """
    return read_route(endpoint).actions
