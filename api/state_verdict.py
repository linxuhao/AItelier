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
from core.state_commands import execute as _execute

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
    followed: bool = False


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


def _action_slot(node: ast.Call, resolver=None) -> ast.expr | None:
    """The argument holding the action name for a recognized delivery site.

    `execute(service, "get_graph", {...})` and `_call(service, "get_graph", ...)`
    hold it second; `partial(_call, service, "get_graph", ...)` third, because
    the wrapped callee occupies the first slot. The name may also arrive as the
    KEYWORD `action=...` — passing the private action as a keyword is a hiding
    shape, not a different call. And the callable may reach this module under an
    ALIAS (`from core.state_commands import execute as runner`); an alias is
    resolved by OBJECT IDENTITY against the real `execute`, derived from the
    action table's own module — never from a hand-written name list.
    """
    name = _callee_name(node)
    if name in _ACTION_CALLABLES or (resolver is not None and name is not None
                                     and resolver(name) is _execute):
        index = 1
    elif name == "partial" and node.args and isinstance(node.args[0], ast.Name) \
            and node.args[0].id in _ACTION_CALLABLES:
        index = 2
    else:
        return None
    if len(node.args) > index:
        return node.args[index]
    for keyword in node.keywords:
        if keyword.arg == "action":
            return keyword.value
    return None


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

    `resolver` maps a callee NAME to the object the handler's own globals bind it
    to. It is what lets the reader DERIVE past a module-level helper (its source
    is read and analyzed the same way) and past a renamed import (object identity
    against the real `execute`) instead of trusting either. A delivered call the
    resolver cannot explain is OPAQUE — see `_delivery_of`.
    """

    def __init__(self, source: str, resolver=None, depth: int = 0):
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
        self._resolver = resolver
        self._depth = depth
        self.followed = False

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
            slot = _action_slot(node, self._resolver)
            if slot is not None:
                if isinstance(slot, ast.Constant) and isinstance(slot.value, str):
                    self.delivered.add(slot.value)
                elif isinstance(slot, ast.Name):
                    self.params.add(slot.id)
                else:
                    self.opaque = True
                continue
            name = _callee_name(node)
            if isinstance(node.func, ast.Name) and name in self._locals:
                if name not in self._called:
                    self._called.add(name)
                    self._walk_block(self._locals[name].body)
                continue
            if isinstance(node.func, ast.Name) and self._follow_helper(name):
                continue
            if not isinstance(node.func, ast.Name):
                # A method call (`x.f(...)`) resolves on its receiver at
                # runtime; an action reaching it through an attribute named
                # `execute`/`_call` was already bound above by its name.
                continue
            # FAIL-CLOSED. A bare-name call in a DELIVERED position that this
            # reader can neither bind to an action site, nor trace into a local
            # def, nor follow into a readable helper, is a delivery it cannot
            # see. It may hand `execute` a private action this source never
            # spells out — the four hiding shapes (module-level helper, renamed
            # import, async inner, keyword action) each hid exactly here. An
            # unresolvable delivery is a refusal, not an approval; `opaque` is
            # the observable, mutable state that records the derivation failure.
            self.opaque = True

    def _follow_helper(self, name: str) -> bool:
        """Follow a module-level helper the handler delivers through.

        The helper is resolved through the handler's OWN globals (derivation, not
        a name list); its source is read and analyzed with the same reader, so
        whatever it delivers is delivered by the handler too. Returns False when
        the callee cannot be resolved to a readable function — the caller then
        refuses (opaque).
        """
        if self._depth >= 3 or self._resolver is None:
            return False
        try:
            target = self._resolver(name)
        except Exception:
            return False
        if target is _execute or not inspect.isfunction(target):
            return False
        try:
            source = inspect.getsource(target)
        except (OSError, TypeError):
            return False
        try:
            sub = _Analysis(source, resolver=self._resolver,
                            depth=self._depth + 1).run()
        except (SyntaxError, IndentationError, ValueError, RecursionError):
            return False
        # Record that this handler's action sites were reached THROUGH an
        # intermediate call: the judged parameter is not handed to the action
        # site directly, so the dispatch identity can no longer be trusted.
        self.followed = True
        self.delivered |= sub.delivered
        self.params |= sub.params
        self.opaque = self.opaque or sub.opaque
        return True


@functools.lru_cache(maxsize=512)
def delivered_actions(handler) -> Delivery:
    """The actions `handler`'s own source can deliver, or `readable=False`."""
    try:
        source = inspect.getsource(handler)
    except (OSError, TypeError):
        return Delivery(frozenset(), frozenset(), False, False, False)
    try:
        globals_map = getattr(handler, "__globals__", None)
        analysis = _Analysis(source,
                             resolver=(globals_map.get if globals_map else None)).run()
    except (SyntaxError, IndentationError, ValueError):
        return Delivery(frozenset(), frozenset(), False, False, False)
    return Delivery(frozenset(analysis.delivered), frozenset(analysis.params),
                    analysis.opaque, True, analysis.followed)


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
    def _private_delivery() -> "Binding | None":
        """A reachable delivery of an action the visibility table keeps private.

        This runs before ANY early ``ok=True``: a handler that dispatches on the
        very path parameter the guard judged is still refused when it ALSO returns
        a private body. Trusting the parameter name alone is a stand-down of the
        verdict, so no branch below may grant the declaration without first
        clearing every delivered action against the table.
        """
        for action in sorted(delivery.actions):
            if not is_public_read(action):
                return Binding(False, f"handler delivers the private action {action}",
                               delivery.actions)
        return None

    if delivery.params:
        # Dispatch on the path action itself: the handler hands `execute` the
        # same string the guard just judged, so the binding is that identity -
        # but only once the literal actions the handler ALSO returns are cleared
        # as public. A dispatch that also delivers a private read is refused.
        if not (delivery.params == {"action"} and "{action}" in route_path
                and judged_action is not None):
            return Binding(False, "handler dispatches on a value the route does not judge",
                           delivery.actions)
        if delivery.followed:
            # The judged parameter reaches the action site through an
            # intermediate call, so the reader cannot bind the value the guard
            # judged to what the handler executes. Fail closed.
            return Binding(False, "the judged action is dispatched through an "
                                  "intermediate call the reader had to follow",
                           delivery.actions)
        private = _private_delivery()
        if private is not None:
            return private
        return Binding(True, "", delivery.actions)
    if not delivery.actions:
        return Binding(False, "handler delivers no state action", delivery.actions)
    private = _private_delivery()
    if private is not None:
        return private
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

    Each entry records TWO facts: WHICH ruling ran, and whether it was backed by
    an authorization dependency actually EXECUTING. `judged(app)` is the
    dependency-backed set — `judged` means "a real ruling was made", not "the
    guard was entered". A public read cleared by the declaration binding makes a
    decision without running a dependency; it is recorded as `public-clearance`
    and reported as `cleared`, never as `judged`.
    """

    def __init__(self):
        self._judged: dict[int, dict[str, tuple]] = {}

    def __enter__(self) -> "VerdictLedger":
        _LEDGERS.append(self)
        return self

    def __exit__(self, *exc_info) -> bool:
        _LEDGERS.remove(self)
        return False

    def _record(self, app, path: str, ruling: str, dep_backed: bool) -> None:
        # The key fact is not merely that a path is present but WHICH verdict the
        # guard applied to it, and whether a dependency executed to make it.
        # "Arrived at the guard" and "a ruling was applied" were the same set
        # while the leak hid here; recording the ruling AND its backing keeps
        # them distinct, so a route the guard never ruled on cannot masquerade
        # as judged.
        self._judged.setdefault(id(app), {})[path] = (ruling, bool(dep_backed))

    def _entries(self, app) -> dict:
        return dict(self._judged.get(id(app), {}))

    def judged(self, app) -> frozenset:
        """Paths for which an authorization dependency actually executed."""
        return frozenset(p for p, (_, backed) in self._entries(app).items() if backed)

    def cleared(self, app) -> frozenset:
        """Paths approved by the declaration binding alone (no dependency ran)."""
        return frozenset(p for p, (ruling, backed) in self._entries(app).items()
                         if not backed and ruling == "public-clearance")

    def rulings(self, app) -> dict:
        return {p: ruling for p, (ruling, _) in self._entries(app).items()}

    def clear(self) -> None:
        self._judged.clear()


_LEDGERS: list[VerdictLedger] = []


def record_judged(app, path: str | None, ruling: str = "verdict-applied",
                  dep_backed: bool = True) -> None:
    """Called by the router guard AFTER it applies a ruling to a request.

    `ruling` names the verdict the guard actually ran (an authorization decision,
    a public clearance, or a declaration refusal). `dep_backed` says whether an
    authorization dependency EXECUTED to make it — a public read cleared by its
    declaration binding records `dep_backed=False`, and coverage reports it as
    `cleared`, not `judged`. The call is made only once a decision exists; it is
    not made on arrival, so reaching the guard without ruling on the request is
    recorded as NO judgment and the route is counted uncovered.
    """
    if path is None or not _LEDGERS:
        return
    for ledger in list(_LEDGERS):
        ledger._record(app, path, ruling, dep_backed)


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
    """Per route: judged / ruling / responded / uncovered, plus its derived verdict.

    `exercised` maps a request URL to the status codes it produced; it is matched
    against each route template. `judged` means an authorization dependency
    EXECUTED for the route — not that the guard was entered. A public read whose
    declaration binding approved it answers without one: it is `cleared` (the
    ruling reads `public-clearance`), which is honest — the reader made the
    decision, no dependency ran. A route that RESPONDED with neither a judgment
    nor a clearance is uncovered: that is the number this exists to move, and it
    is what the arrival-count hid while a private body leaked.
    """
    report: dict = {}
    entries = ledger._entries(app)
    for path, row in declaration_table(app).items():
        statuses = exercised_for(path, exercised)
        ruling, dep_backed = entries.get(path, (None, False))
        cleared = not dep_backed and ruling == "public-clearance"
        report[path] = {
            **row,
            "judged": dep_backed,
            "dep_backed": dep_backed,
            "cleared": cleared,
            "ruling": ruling,
            "responded": bool(statuses),
            "statuses": tuple(sorted(set(statuses))),
            "uncovered": bool(statuses) and not (dep_backed or cleared),
        }
    return report


def uncovered_routes(app, ledger: VerdictLedger, exercised: dict) -> list:
    """Routes that answered without the verdict running, by path."""
    return sorted(path for path, row
                  in coverage_report(app, ledger, exercised).items()
                  if row["uncovered"])

