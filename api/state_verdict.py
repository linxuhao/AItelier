"""Bind a `/api/state` route's declaration to the action its handler serves.

Two halves make an anonymous read of a private record possible, and this module
closes the second one:

1. a route declares a PUBLIC read action, so `_router_guard` applies no verdict;
2. the handler does not actually SERVE that action — it executes and returns a
   PRIVATE one, whose body then leaves over a door the declaration calls open.

The declaration is read off the endpoint object, and so is the answer: the
handler's own source, parsed, reduced to the actions a reachable `return` can
hand to a caller. A declaration is honoured only when every action the handler
delivers is public. Anything this module cannot resolve — unreadable source, a
delivered action that is not a literal, a delivery through a value it cannot
bind — is REFUSED. An unread declaration is not an approval.

The rules, in full, because a rule nobody writes down is a rule that drifts:

* DELIVERY, not mention. Only reachable `return` expressions decide what a
  route can hand to a caller. A call whose result is discarded cannot deliver a
  body, so a bare `execute(service, "get_graph", {})` line binds nothing.
* Dead code is not reachable: `if False:` / `if 0:` bodies, statements after a
  `return` or `raise` in the same block, and commented-out lines (absent from
  the AST entirely) are never examined.
* An inner `def` is examined only when the handler actually calls it, so an
  `execute` inside a function nobody calls binds nothing.
* A handler that DELIVERS several actions may declare a public one only when
  every action it delivers is public: the stricter action wins. "It mentioned a
  public action" is not a rule.
* The action-dispatch families (`/query/{action}`) hand `execute` the very
  parameter the route takes from its path — the value the guard has already
  judged — and are bound by that identity rather than exempted.

WHY THERE IS NO STAND-DOWN. An earlier revision let a route stand the guard
down instead of judging it, keyed on something the route author could reach: an
endpoint attribute, then a dependency attribute, then object identity against
two `WeakSet` registries. Each was the same exemption in a new carrier. It
existed so that a route a router-level guard had already judged would not be
judged a second time. Measuring that settles it: applying a verdict is
idempotent — the verdict is a pure function of the request's credential, so
judging a request N times yields the same status and the same body as judging
it once — therefore the second judgment costs nothing and protects nothing.
There is nothing to stand down and nothing for a route author to reach for.

`VerdictLedger` watches which routes the one guard ACTUALLY judged while
requests are in flight, so route coverage can be measured as "did the verdict
run", not as "is the guard object somewhere in this route's tree".
"""
from __future__ import annotations

import ast
import functools
import inspect
import textwrap
from typing import NamedTuple

from core.state_commands import is_public_read


# The attribute a route's endpoint carries its declaration on.
DECLARATION_ATTR = "_state_route"
# Every route this repository serves out of the State router lives here.
STATE_PREFIX = "/api/state"

# The callables a handler uses to run a State action, and the wrapper
# `api/state_http.query` uses to hand the call to a worker thread.
_ACTION_CALLABLES = ("execute", "_call")


class Delivery(NamedTuple):
    """What a handler's source says it can deliver.

    `actions`  action names a reachable `return` can hand to a caller.
    `params`   handler parameter names handed to `execute` as the action.
    `opaque`   True when a delivery site's action is neither a literal nor a
               recognized parameter, so this module cannot bind it.
    `readable` False when the handler's source could not be read at all.
    """

    actions: frozenset
    params: frozenset
    opaque: bool
    readable: bool


class Binding(NamedTuple):
    """Whether a declaration is bound to the action its handler delivers."""

    ok: bool
    reason: str
    delivered: frozenset


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _action_slot(node: ast.Call) -> ast.expr | None:
    """The argument holding the action name for a recognized delivery site.

    `execute(service, "get_graph", {...})` and `_call(service, "get_graph", ...)`
    hold it second; `partial(_call, service, "get_graph", ...)` third, because
    the wrapped callee occupies the first slot.
    """
    name = _callee_name(node)
    if name in _ACTION_CALLABLES:
        index = 1
    elif name == "partial" and node.args and isinstance(node.args[0], ast.Name) \
            and node.args[0].id in _ACTION_CALLABLES:
        index = 2
    else:
        return None
    if len(node.args) <= index:
        return None
    return node.args[index]


def _static_truth(test: ast.expr) -> bool | None:
    """The truth value of a statically decidable test, else None."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    return None


class _Analysis:
    """Reduce a handler's source to the actions it can deliver.

    Straight-line reachability over the handler body: a block stops at the first
    `return`/`raise`, a statically false test takes its `else` branch only, and
    nested `def`s are remembered but not examined until the handler calls them.
    """

    def __init__(self, source: str):
        tree = ast.parse(textwrap.dedent(source))
        function = next((node for node in tree.body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if function is None:
            raise SyntaxError("no function definition in the handler source")
        self.delivered: set[str] = set()
        self.params: set[str] = set()
        self.opaque = False
        self._locals: dict[str, ast.AST] = {}
        self._called: set[str] = set()
        self._body = function.body

    def run(self) -> "_Analysis":
        self._walk_block(self._body)
        return self

    # ── reachability ────────────────────────────────────────────────────────
    def _walk_block(self, statements) -> bool:
        """Walk a block; return True when control can continue past it."""
        for statement in statements:
            if not self._walk_statement(statement):
                return False
        return True

    def _walk_statement(self, statement) -> bool:
        if isinstance(statement, ast.Return):
            self._delivery_of(statement.value)
            return False
        if isinstance(statement, ast.Raise):
            return False
        if isinstance(statement, ast.If):
            truth = _static_truth(statement.test)
            if truth is False:
                return self._walk_block(statement.orelse) if statement.orelse else True
            if truth is True:
                return self._walk_block(statement.body)
            self._walk_block(statement.body)
            self._walk_block(statement.orelse)
            return True
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._locals[statement.name] = statement
            return True
        if isinstance(statement, (ast.For, ast.While)):
            self._walk_block(statement.body)
            self._walk_block(statement.orelse)
            return True
        if isinstance(statement, ast.Try):
            self._walk_block(statement.body)
            for handler in statement.handlers:
                self._walk_block(handler.body)
            self._walk_block(statement.orelse)
            self._walk_block(statement.finalbody)
            return True
        if isinstance(statement, ast.With):
            self._walk_block(statement.body)
            return True
        # Any other statement can still RUN an action (a discarded call, an
        # assignment whose value is returned later). It cannot DELIVER a body by
        # itself, but the action it names, if it is one of the handler's own
        # parameters, is how the dispatch families bind to the value the guard
        # judged — and an action slot that is neither a literal nor such a
        # parameter is unresolvable, so the declaration is refused.
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                slot = _action_slot(node)
                if slot is None:
                    continue
                if isinstance(slot, ast.Constant) and isinstance(slot.value, str):
                    continue  # discarded delivery: binds nothing
                if isinstance(slot, ast.Name):
                    self.params.add(slot.id)
                else:
                    self.opaque = True
        return True

    # ── delivery ────────────────────────────────────────────────────────────
    def _delivery_of(self, expression: ast.expr | None) -> None:
        if expression is None:
            return
        for node in ast.walk(expression):
            if not isinstance(node, ast.Call):
                continue
            slot = _action_slot(node)
            if slot is not None:
                if isinstance(slot, ast.Constant) and isinstance(slot.value, str):
                    self.delivered.add(slot.value)
                elif isinstance(slot, ast.Name):
                    self.params.add(slot.id)
                else:
                    self.opaque = True
                continue
            name = _callee_name(node)
            if name in self._locals and name not in self._called:
                self._called.add(name)
                self._walk_block(self._locals[name].body)


@functools.lru_cache(maxsize=512)
def delivered_actions(handler) -> Delivery:
    """The actions `handler`'s own source can deliver, or `readable=False`."""
    try:
        source = inspect.getsource(handler)
    except (OSError, TypeError):
        return Delivery(frozenset(), frozenset(), False, False)
    try:
        analysis = _Analysis(source).run()
    except (SyntaxError, IndentationError, ValueError):
        return Delivery(frozenset(), frozenset(), False, False)
    return Delivery(frozenset(analysis.delivered), frozenset(analysis.params),
                    analysis.opaque, True)


def binding_for(endpoint, judged_action: str | None, route_path: str = "") -> Binding:
    """Whether `endpoint`'s declaration of `judged_action` holds against its source.

    A declaration is bound when the handler DELIVERS that very action and
    delivers nothing private. Naming a public action the handler never hands to
    a caller is a mismatch, not a free pass: `if False: execute(service,
    "get_graph", {})` in a handler that returns `get_driver_note` binds nothing.
    """
    delivery = delivered_actions(endpoint)
    if not delivery.readable:
        return Binding(False, "handler source is unreadable", delivery.actions)
    if delivery.opaque:
        return Binding(False, "a delivered action is not a literal", delivery.actions)
    if delivery.params:
        # Dispatch on the path action itself: the handler hands `execute` the
        # same string the guard just judged, so the binding is that identity.
        if not (delivery.params == {"action"} and "{action}" in route_path
                and judged_action is not None):
            return Binding(False, "handler dispatches on a value the route does not judge",
                           delivery.actions)
        return Binding(True, "", delivery.actions)
    if not delivery.actions:
        return Binding(False, "handler delivers no state action", delivery.actions)
    for action in sorted(delivery.actions):
        if not is_public_read(action):
            return Binding(False, f"handler delivers the private action {action}",
                           delivery.actions)
    if judged_action not in delivery.actions:
        return Binding(False,
                       f"declaration names {judged_action}; handler serves "
                       f"{sorted(delivery.actions)}",
                       delivery.actions)
    return Binding(True, "", delivery.actions)



# ── the verdict-run ledger, and the coverage it makes measurable ─────────────
class VerdictLedger:
    """Which routes the one guard ACTUALLY judged while it was armed.

    Coverage must answer "did the verdict run for this route", so it is measured
    from runs, not from the shape of a route's dependency list. A route that
    answered while no judgment was recorded is uncovered — and named.
    """

    def __init__(self):
        self._judged: dict[int, set[str]] = {}

    def __enter__(self) -> "VerdictLedger":
        _LEDGERS.append(self)
        return self

    def __exit__(self, *exc_info) -> bool:
        _LEDGERS.remove(self)
        return False

    def _record(self, app, path: str) -> None:
        self._judged.setdefault(id(app), set()).add(path)

    def judged(self, app) -> frozenset:
        return frozenset(self._judged.get(id(app), ()))

    def clear(self) -> None:
        self._judged.clear()


_LEDGERS: list[VerdictLedger] = []


def record_judged(app, path: str | None) -> None:
    """Called by the router guard the moment it judges a request."""
    if path is None or not _LEDGERS:
        return
    for ledger in list(_LEDGERS):
        ledger._record(app, path)


def state_route_paths(app) -> dict:
    """Every route under the State prefix, derived from the app's own table.

    Mounted sub-apps are descended into, so an `app.mount()`ed router and a bare
    Starlette route are measured by the same walk that measures the product's own
    routes — a new route appears here without anybody adding it to a list.
    """
    found: dict = {}
    def walk(routes, prefix: str) -> None:
        for route in routes:
            # FastAPI may wrap an included router in a holder object rather than
            # placing its routes directly on the app; unwrap both shapes and keep
            # descending, so an included router, a mounted sub-app and a bare
            # Starlette route are measured by the SAME walk.
            inner = getattr(route, "original_router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes, prefix + (getattr(route, "path", "") or ""))
                continue
            own = getattr(route, "path", "") or ""
            full = prefix + own
            # A route is under the prefix by its OWN path (a mounted sub-app's
            # routes are absolute, the path the guard sees in the request scope)
            # or by the accumulated one (a router included under a parent prefix).
            state_path = next((candidate for candidate in (own, full)
                               if candidate == STATE_PREFIX
                               or candidate.startswith(STATE_PREFIX + "/")), None)
            nested = getattr(route, "routes", None)
            if nested is not None:
                walk(nested, full)
            if state_path is not None:
                found[state_path] = route


    walk(getattr(app, "routes", []) or [], "")
    return found


def route_verdict(route) -> str:
    """The verdict the guard reaches for a route, derived from its declaration."""
    declaration = getattr(getattr(route, "endpoint", None), DECLARATION_ATTR, None)
    if declaration is None:
        return "refused-undeclared"
    kind, action = declaration
    if kind == "write":
        return "write-verdict"
    if kind == "director":
        return "director-verdict"
    if action is None:
        return "dispatch-on-path-action"
    if not is_public_read(action):
        return "private-verdict"
    path = getattr(route, "path", "") or ""
    binding = binding_for(route.endpoint, action, path)
    return "public" if binding.ok else "refused-declaration-mismatch"


def declaration_table(app) -> dict:
    """route → declaration → actions the source delivers → verdict, one row each."""
    table: dict = {}
    for path, route in state_route_paths(app).items():
        endpoint = getattr(route, "endpoint", None)
        declaration = getattr(endpoint, DECLARATION_ATTR, None)
        delivery = delivered_actions(endpoint) if endpoint is not None else None
        table[path] = {
            "methods": tuple(sorted(getattr(route, "methods", None) or ())),
            "declaration": declaration,
            "delivered": tuple(sorted(delivery.actions)) if delivery else (),
            "dispatch_params": tuple(sorted(delivery.params)) if delivery else (),
            "readable": bool(delivery.readable) if delivery else False,
            "verdict": route_verdict(route),
        }
    return table


def unreadable_routes(app) -> list:
    """The routes whose handler source could not be read, by path."""
    return sorted(path for path, row in declaration_table(app).items()
                  if not row["readable"])

def exercised_for(template: str, exercised: dict) -> tuple:
    """The statuses recorded for a route TEMPLATE — a request URL is concrete.

    `exercised` is keyed by the URL a request went to; the route table is keyed by
    the template (`/api/state/projects/{project_id}`). The two are matched here so
    a response is never missed because a path parameter had a value.
    """
    segments = [s for s in template.split("/") if s]
    out: list = []
    for url, statuses in (exercised or {}).items():
        parts = [s for s in str(url).split("?")[0].split("/") if s]
        if len(parts) != len(segments):
            continue
        if all(seg.startswith("{") or seg == part
               for seg, part in zip(segments, parts)):
            out.extend(statuses or ())
    return tuple(out)


def coverage_report(app, ledger: VerdictLedger, exercised: dict) -> dict:
    """Per route: judged / responded / uncovered, plus its derived verdict.

    `exercised` maps a request URL to the status codes it produced; it is matched
    against each route template. A route that RESPONDED while the ledger holds no
    judgment for it is uncovered — that is the number this exists to move.
    """
    report: dict = {}
    for path, row in declaration_table(app).items():
        statuses = exercised_for(path, exercised)
        judged = path in ledger.judged(app)
        report[path] = {
            **row,
            "judged": judged,
            "responded": bool(statuses),
            "statuses": tuple(sorted(set(statuses))),
            "uncovered": bool(statuses) and not judged,
        }
    return report


def uncovered_routes(app, ledger: VerdictLedger, exercised: dict) -> list:
    """Routes that answered without the verdict running, by path."""
    return sorted(path for path, row
                  in coverage_report(app, ledger, exercised).items()
                  if row["uncovered"])

