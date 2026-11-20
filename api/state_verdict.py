"""Bind a `/api/state` route's declaration to the action its handler serves.

Two halves make an anonymous read of a private record possible, and this module
closes the second one:

1. a route declares a PUBLIC read action, so `_router_guard` applies no verdict;
2. the handler does not actually SERVE that action — it executes and returns a
   PRIVATE one, whose body then leaves over a door the declaration calls open.

The declaration is read off the endpoint object, and so is the answer: the
handler's own source, parsed, reduced to the actions a reachable `return` can
hand to a caller. The binding is a ROUTE-SHAPE review, not a grant of
confidentiality: whatever it approves, confidentiality is decided where the
private read EXECUTES (`core.state_privacy`), so this layer cannot widen what a
caller may read. An unread declaration is not an approval.

The rules, in full, because a rule nobody writes down is a rule that drifts:

* DELIVERY, not mention. Only reachable `return` expressions decide what a
  route can hand to a caller. A call whose result is discarded cannot deliver a
  body, so a bare `execute(service, "get_graph", {})` line binds nothing.
* Dead code is not reachable: `if False:` / `if 0:` bodies, statements after a
  `return` or `raise` in the same block, and commented-out lines (absent from
  the AST entirely) are never examined.
* An inner `def` is examined only when the handler actually calls it, so an
  `execute` inside a function nobody calls binds nothing.
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
import builtins
import functools
import inspect
import textwrap
from typing import NamedTuple

from core.state_commands import is_public_read
from core.state_commands import execute as _execute
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response)

# The attribute a route's endpoint carries its declaration on.
DECLARATION_ATTR = "_state_route"
# Every route this repository serves out of the State router lives here.
STATE_PREFIX = "/api/state"

# The callables a handler uses to run a State action, and the wrapper
# `api/state_http.query` uses to hand the call to a worker thread.
_ACTION_CALLABLES = ("execute", "_call")

# Calls that BUILD A VALUE instead of delivering one. The reader walks their
# ARGUMENTS exactly like any other expression, so an action handed to one is
# still delivered: `return JSONResponse(execute(service, "get_driver_note", {}))`
# is refused by the private-delivery branch, not excused here. What such a call
# cannot do is run a State action in a body of its own - these response classes
# format or package the value they are handed at construction time - so following
# one found nothing, and refusing it made an ordinary
# `except ...: return build_the_refusal(exc)` blind the whole action-dispatch
# family: one refusal builder that wraps its body in `JSONResponse` turned
# `POST /api/state/query/get_graph` into a 403 "state route declaration does not
# match its handler" (iss-393c02b9ed844e04), while the same action over its own
# GET route answered 200.
#
# Membership is by OBJECT IDENTITY, never by name, and only for a name the
# analyzed function resolves in ITS OWN module globals or in the builtins THAT
# MODULE resolves through (its `__builtins__`, which a module may replace). A
# name the function binds itself (parameter, assignment, local import, walrus, a
# `match` capture) or closes over is NOT inert - the reader cannot see through
# that binding - and neither is a subclass or a same-named function of the
# author's. The same scope rule governs which FUNCTION a helper call resolves to
# (`_follow_helper`), for the same reason.
#
# The value constructors below are the ones whose only effect on an argument is
# to read it: they can call a dunder the argument defines (`str(x)` runs
# `type(x).__str__`), which is why what they are handed is still walked as a
# delivery. `StreamingResponse` and `FileResponse` are deliberately absent: they
# consume an iterator or a path at send time, so "packages what it is handed at
# construction" is not true of them. Adding an entry here is a claim that the
# callable cannot run a State action.
_VALUE_CONSTRUCTORS = (str, repr, format, bytes, bool, int, float, len,
                       list, tuple, dict, set, frozenset, sorted, min, max,
                       abs, round)
_RESPONSE_CONSTRUCTORS = (Response, HTMLResponse, PlainTextResponse, JSONResponse,
                          RedirectResponse)
_INERT_CALLABLES = _VALUE_CONSTRUCTORS + _RESPONSE_CONSTRUCTORS
# The marker a resolution uses when the "namespace" is a function whose BODY is
# what must not change: `helper.__code__` can be swapped under the same object id.
_CODE_GUARD = "__code__"


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


def _bound_names(function) -> set[str]:
    """Every name BOUND inside `function`, at any depth.

    Parameters, assignment and walrus targets, `for`/`with`/`except` targets,
    imports, nested `def`/`class` names, comprehension targets and the names a
    `match` statement CAPTURES (`case JSONResponse:` and `case [str]:` bind the
    name, they do not compare it). A name in this set is NOT whatever a module
    global, a builtin, or a helper of the same name is - `str` may be a local
    `def`, a function-local import, a loop variable or a capture - so the reader
    may not explain a call to it by identity. Erring towards OVER-counting is
    correct here: a false positive only refuses.
    """
    names: set[str] = set()
    arguments = getattr(function, "args", None)
    if arguments is not None:
        for argument in (list(arguments.posonlyargs) + list(arguments.args)
                         + list(arguments.kwonlyargs)
                         + [arguments.vararg, arguments.kwarg]):
            if argument is not None:
                names.add(argument.arg)
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name is not None:
                names.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest is not None:
                names.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _builtins_namespace(globals_map, builtins_map=None) -> dict:
    """The builtins the analyzed function resolves an unbound name through.

    At runtime a name that is not in the function's globals resolves through that
    function's OWN `__builtins__`, which is usually the `builtins` module but may
    be a dict the module replaced - that is the ordinary way a module rebinds
    `str`. `builtins_map` is that namespace as read off the function object; the
    module global is only the fallback for a function that does not carry one.
    Answering with the process-wide `builtins` regardless would describe a lookup
    the function never performs, and `str` would be called inert while the module
    runs something else.
    """
    namespace = builtins_map
    if not isinstance(namespace, dict):
        namespace = (globals_map.get("__builtins__", builtins)
                     if isinstance(globals_map, dict) else builtins)
    if isinstance(namespace, dict):
        return namespace
    namespace_dict = getattr(namespace, "__dict__", None)
    return namespace_dict if isinstance(namespace_dict, dict) else vars(builtins)


def _function_builtins(function) -> dict | None:
    """The builtins namespace CPython fixed into `function` when it was created.

    A module that swaps its `__builtins__` and later restores it leaves every
    function it created under the swapped namespace looking names up THERE: the
    frame's builtins are copied into the function object at creation. Reading the
    module global instead would describe a lookup this function never performs.
    """
    try:
        namespace = getattr(function, "__builtins__", None)
    except Exception:
        return None
    if isinstance(namespace, dict):
        return namespace
    namespace_dict = getattr(namespace, "__dict__", None)
    return namespace_dict if isinstance(namespace_dict, dict) else None


def _yielded_values(statement) -> list[ast.expr | None]:
    """The value of every `yield`/`yield from` this statement runs in ITS OWN frame.

    A generator hands each yielded value to whoever iterates it, so a yield is a
    delivery position whatever shape surrounds it: `yield v`, `yield from v`,
    `got = yield v` and `x = [yield v]` all hand `v` out. A yield inside a nested
    `def`/`lambda`/`class` body belongs to THAT callable, which the reader follows
    only when the handler hands control to it.
    """
    found: list[ast.expr | None] = []
    stack: list[ast.AST] = [statement]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            found.append(node.value)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _identity_holds(holder, name: str, identity: int) -> bool:
    """Whether `holder` still resolves `name` to the object the read recorded.

    Two kinds of holder, two kinds of name: a mapping (`__globals__` or a builtins
    dict) answers with `get`, and a FUNCTION answers the marker `__code__` with
    its body. The body case cannot be folded into the first: swapping
    `helper.__code__` keeps the function object's id while changing what runs.
    """
    if name == _CODE_GUARD:
        return id(getattr(holder, "__code__", None)) == identity
    try:
        return id(holder.get(name)) == identity
    except Exception:
        return False


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

    def __init__(self, source: str, resolver=None, depth: int = 0, closure=(),
                 globals_map=None, builtins_map=None):
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
        # Anything this function BINDS or closes over shadows a module global or a
        # builtin of the same name, so neither may be used to explain a call to it.
        self._bound = _bound_names(function)
        self._closure = set(closure)
        # The globals and builtins THIS function would look a name up in, and the
        # identities it resolved: a cached verdict is only as good as the namespaces
        # it read, so every resolution is remembered and re-checked
        # (`_resolve`, `_identity_holds`).
        self._globals = globals_map if isinstance(globals_map, dict) else {}
        self._builtins = _builtins_namespace(globals_map, builtins_map)
        self.resolved: list[tuple[object, str, int]] = []
        # Set when the body names an action site at all - delivered, discarded or
        # yielded. A helper that does that hands over SOMETHING, even if no
        # `return` carries it.
        self.saw_action_site = False

    def _resolve(self, name: str):
        """The object this module's globals bind `name` to, or None.

        The identity is recorded so `delivered_actions` can tell whether the
        module still binds that very object: a global rebound AFTER the analysis
        would otherwise be read through a stale verdict (`JSONResponse` swapped
        for a class that runs a state action).
        """
        if self._resolver is None:
            return None
        try:
            target = self._resolver(name)
        except Exception:
            return None
        if isinstance(self._globals, dict) and self._globals:
            self.resolved.append((self._globals, name, id(target)))
        return target

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
        # Every yield this statement runs hands a value out of the handler's frame,
        # whatever shape it is written in, and it is NOT covered by the
        # discarded-expression scan below (`x = yield v` names no action site).
        for yielded in _yielded_values(statement):
            self._delivery_of(yielded)
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
        if isinstance(statement, (ast.For, ast.While, ast.AsyncFor)):
            self._walk_block(statement.body)
            self._walk_block(statement.orelse)
            return True
        if isinstance(statement, ast.Match):
            # Every case body is reachable: the reader does not evaluate patterns,
            # and a delivery hidden in one arm is still a delivery.
            for case in statement.cases:
                self._walk_block(case.body)
            return True
        if isinstance(statement, ast.Try):
            self._walk_block(statement.body)
            for handler in statement.handlers:
                self._walk_block(handler.body)
            self._walk_block(statement.orelse)
            self._walk_block(statement.finalbody)
            return True
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            self._walk_block(statement.body)
            return True
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield):
            # Already walked as a yield position above; this arm only keeps the
            # discarded-expression scan below from re-reading the same call.
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
                self.saw_action_site = True
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
                self.saw_action_site = True
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
            if isinstance(node.func, ast.Name) and self._is_value_constructor(name):
                # Builds a value; its arguments were walked above.
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

    def _is_value_constructor(self, name: str) -> bool:
        """Whether a bare name resolves to a callable that only builds a value.

        The name is resolved the same way a followed helper is - through the
        handler's OWN globals - and only then compared by identity with the
        constructors above. A handler that binds the name to something else (a
        function of its own, a subclass of a response class) resolves to a
        different object and is NOT inert. Neither does a name this function
        BINDS or CLOSES OVER: `str` may be a local `def`, a function-local
        import or a walrus target, and the reader cannot see through a binding
        it did not resolve.
        """
        if name in self._bound or name in self._closure:
            return False
        target = self._resolve(name)
        if target is None:
            # `str`, `len`, ... are usually builtins: they are not in a module's
            # globals, so the resolver cannot see them. Which builtins a function
            # actually resolves through is its own `__builtins__`, not the
            # interpreter's - and that namespace can be a dict someone mutates
            # after this read, so the identity is recorded like a global's.
            target = self._builtins.get(name)
            self.resolved.append((self._builtins, name, id(target)))
        return any(target is inert for inert in _INERT_CALLABLES)

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
        if name in self._bound or name in self._closure:
            # The name is bound by this function or closed over from an enclosing
            # one, so the module global of that name is NOT what runs: a closure
            # variable or a local rebinding can hold a function that delivers a
            # private body while the global is a harmless refusal builder.
            return False
        try:
            target = self._resolve(name)
        except Exception:
            return False
        if target is _execute or not inspect.isfunction(target):
            return False
        if getattr(target, "__wrapped__", None) is not None:
            # `functools.wraps` copies `__wrapped__`, and `inspect.getsource`
            # UNWRAPS it - so the source read here would be the inner function's
            # while the object the handler calls is the WRAPPER, whose own body
            # may run the action. Which body runs cannot be read off this object.
            return False
        try:
            source = inspect.getsource(target)
        except (OSError, TypeError):
            return False
        # The body that will run is `target.__code__`, and it can be swapped under
        # the same function object: the identity of the function says nothing about
        # the code it runs now. Record the body's identity so a cached verdict
        # cannot survive a swap.
        self.resolved.append((target, _CODE_GUARD,
                              id(getattr(target, "__code__", None))))
        # The helper's body resolves names where the HELPER lives, not where the
        # handler lives: a module that binds `JSONResponse` to its own class must
        # not be read through the handler's module, where the name may mean
        # starlette's class. Its builtins come off the function object for the
        # same reason a module that swapped `__builtins__` cannot be read off the
        # module's current global.
        helper_globals = getattr(target, "__globals__", None)
        resolver = (helper_globals.get if isinstance(helper_globals, dict)
                    else self._resolver)
        freevars = getattr(getattr(target, "__code__", None), "co_freevars", ())
        try:
            sub = _Analysis(source, resolver=resolver, depth=self._depth + 1,
                            closure=freevars, globals_map=helper_globals,
                            builtins_map=_function_builtins(target)).run()
        except Exception:
            # FAIL-CLOSED. Any failure to finish the read - a syntax error, a
            # namespace whose `str` someone replaced under the interpreter, a
            # recursion limit a hostile body hits - leaves the helper UNREAD, and
            # an unread helper is a delivery this reader cannot explain.
            return False
        # A resolution this helper made in ITS OWN globals is checked against
        # those globals on every cached read, not only the handler's.
        self.resolved.extend(sub.resolved)
        # Record that this handler's action sites were reached THROUGH an
        # intermediate call: the judged parameter is not handed to the action
        # site directly, so the dispatch identity can no longer be trusted.
        #
        # Only a helper that HANDS SOMETHING OVER can move that identity: a
        # delivered action, an action parameter, an action site it names without
        # returning it (a discarded call), a value it `yield`s - including a call
        # it yields whose own body delivers one - a helper of its own, or a
        # delivery it could not explain. A refusal builder called from an
        # `except` clause hands over none of those - it formats a message and
        # returns it - so the dispatch identity stays where it was. Counting it
        # was the second half of iss-393c02b9ed844e04: together with that
        # builder's own `JSONResponse(...)` body it made every public action on
        # `POST /api/state/query/{action}` an unverifiable declaration.
        if (sub.delivered or sub.params or sub.opaque or sub.followed
                or sub.saw_action_site):
            self.followed = True
        self.delivered |= sub.delivered
        self.params |= sub.params
        self.opaque = self.opaque or sub.opaque
        return True


@functools.lru_cache(maxsize=512)
def _read_delivery(handler) -> tuple[Delivery, tuple]:
    """Read the handler's source once, WITH the identities it resolved.

    The identities come back beside the verdict so `delivered_actions` can notice
    that a global, a builtins namespace or a followed helper's body changed after
    the read; a verdict is only valid for the namespaces it was derived from.
    """
    if getattr(handler, "__wrapped__", None) is not None:
        # `inspect.getsource` unwraps, so the source read here would be the inner
        # function's while the object the route calls is the wrapper. Unread is
        # not an approval.
        return Delivery(frozenset(), frozenset(), True, True, False), ()
    try:
        source = inspect.getsource(handler)
    except (OSError, TypeError):
        return Delivery(frozenset(), frozenset(), False, False, False), ()
    try:
        globals_map = getattr(handler, "__globals__", None)
        freevars = getattr(getattr(handler, "__code__", None), "co_freevars", ())
        analysis = _Analysis(source,
                             resolver=(globals_map.get if globals_map else None),
                             closure=freevars, globals_map=globals_map,
                             builtins_map=_function_builtins(handler)).run()
    except Exception:
        # FAIL-CLOSED. A read that cannot finish is an UNREADABLE handler, not an
        # approval: the guard above answers such a route with the mismatch
        # refusal. Every exception belongs here, including the ones a replaced
        # builtin raises inside `ast` itself (`builtins.str` swapped process-wide
        # makes `isinstance` raise) - a crash would skip the refusal entirely.
        return Delivery(frozenset(), frozenset(), False, False, False), ()
    delivery = Delivery(frozenset(analysis.delivered), frozenset(analysis.params),
                        analysis.opaque, True, analysis.followed)
    return delivery, tuple(analysis.resolved)


def delivered_actions(handler) -> Delivery:
    """The actions `handler`'s own source can deliver, or `readable=False`."""
    delivery, resolved = _read_delivery(handler)
    if any(not _identity_holds(holder, name, identity)
           for holder, name, identity in resolved):
        # Something the read leaned on changed after it: a global rebound, a
        # builtins dict mutated, or a followed helper's body swapped. The cached
        # verdict described the old state, so re-read instead of serving it.
        _read_delivery.cache_clear()
        delivery, resolved = _read_delivery(handler)
    return delivery


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

