"""A call that BUILDS A VALUE is not a delivery, and an empty helper moves nothing.

The defect (iss-393c02b9ed844e04): `api/state_http.query` answers a retryable
busy lock through `state_write_busy_response`, whose whole body is
``return JSONResponse({"detail": str(exc), ...}, status_code=503)``. The reader
followed that helper, marked the dispatch handler ``opaque`` (a bare
``JSONResponse(...)`` and a bare ``str(...)`` it could not resolve) and
``followed``, so ``binding_for`` refused EVERY public action on
``POST /api/state/query/{action}`` with 403 "state route declaration does not
match its handler" — while the same action over its own GET route answered 200.
One ``except`` clause that delivers nothing, and every anonymous public read
through the dispatch door went dark.

Two independent over-refusals met in that clause, and each has its own witness
below (so a fix for one cannot hide behind the other):

* a followed helper that hands over nothing at all — no delivered action, no
  action parameter, no action site even in a discarded or yielded position —
  cannot move the dispatch identity;
* a call that only BUILDS A VALUE (``str``, ``len``, an eager response class)
  cannot run a State action in a body of its own, and its ARGUMENTS are walked
  like any other expression, so an action handed to it is still seen.

"By identity, never by name" is the whole safety argument, so the negatives pin
the ways a NAME can lie: a local ``def``, a module global, a closure variable, a
function-local import, a ``match`` capture, a subclass of a response class,
``eval``, a private action passed INTO a response constructor, a helper whose own
module binds ``JSONResponse`` to something else, a module that replaces its own
``__builtins__``, a helper reached through ``functools.wraps``, a private action a
``yield`` carries - directly or from a nested helper, through ``yield from``, or
as the value of an assignment - a response class the module rebinds AFTER the
reader looked at it, a builtins dict mutated after the read, and a followed
helper whose ``__code__`` was swapped under the same function object.

The shapes below are a second, measured family: each was observed EXECUTING the
private action while the reader answered ``ok=True`` (iss-a555f839083d405d and
iss-0d84268ff0af47ee), and each is refused now for the reason it leaked. They are
witnessed at the DOOR, not only at the binding: the product's own dispatch route
(``POST /api/state/query/{action}``) and the router's own guard, with the row's
handler installed on it, refused with the guard's own message, while a handler
that delivers the judged action directly answers 200 on the same route.

* ``N8``  a callee behind an ATTRIBUTE: ``helpers.leak(service)``
* ``N9``  a callee behind a SUBSCRIPT: ``[_leak][0](service)``
* ``N12`` a value constructor (``str``) handed an object whose ``__str__`` reads
  the mailbox
* ``N12b`` that same object handed out BARE, with no constructor at all
* ``C1``  ``functools.partial(LeakyResponse)(service)``
* ``E9``  the HANDLER's own ``__code__`` swapped after the first read
* ``E10`` local-variable flow: ``x = _leak(service); return x``
* ``E11`` an ``execute`` ALIAS rebound after the first read

What is still accepted, and why that is a claim rather than a shrug (each is
recorded where the rule lives, never left as a silent pass):

* a call in a CONDITION. Its truth value picks a branch and is never handed out,
  so a private body cannot leave through it - which is what keeps the product's
  own ``return await result if inspect.isawaitable(result) else result``
  readable (``_delivering_nodes``).
* ``anyio.to_thread.run_sync(<the action call>)``: a callable that only RUNS the
  callable it is handed, whitelisted by OBJECT IDENTITY and transparent only when
  what it is handed is a recognized action call, which is the product's own
  thread offload of the dispatch (``_DISPATCH_WRAPPERS``). Handed anything else it
  is refused, and the row below pins that.
* a process that monkeypatches a third-party class's own method
  (``starlette.responses.JSONResponse.__init__``) after the read keeps the class
  identity the reader whitelisted, so the cached verdict survives. That is a
  measured boundary, not a claimed safety property.
"""
from __future__ import annotations

import builtins
import functools
import linecache

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from api.state_graph_routers import get_service
from api.state_graph_routers import router as state_router
from api.state_verdict import DECLARATION_ATTR, binding_for
from core.state_commands import READ_REQUESTS, execute, is_public_read
from core.state_database import StateDatabase
from core.state_graph import StateWriteBusy
from core.state_service import StateService
from tests.support.state_author_surface import (GEN_PID, LEAK_MARK, PRIVATE_ACTION,
                                                seed_private_mail)

TEMPLATE = "/api/state/query/{action}"


def _call(service, action, arguments=None, write=False):
    raise AssertionError("the binding reader never runs a handler")


def _namespace() -> dict:
    """The globals a route module has: the action callable, the refusal types
    and the response class it builds its bodies with."""

    class _Sneaky:
        """A LOCAL builder: its ``__init__`` runs a State action."""

        def __init__(self, service):
            self.body = execute(service, PRIVATE_ACTION, {"project_id": GEN_PID})

    class _LeakyResponse(JSONResponse):
        """A SUBCLASS of a response class, with a State action in ``__init__``."""

        def __init__(self, service):
            super().__init__(execute(service, PRIVATE_ACTION,
                                     {"project_id": GEN_PID}))

    return {"Depends": Depends, "get_service": get_service, "execute": execute,
            "JSONResponse": JSONResponse, "StateWriteBusy": StateWriteBusy,
            "_call": _call, "_Sneaky": _Sneaky, "_LeakyResponse": _LeakyResponse}


def _register(name: str, source: str) -> str:
    filename = f"<value_construction/{name}>"
    linecache.cache[filename] = (len(source), None,
                                 source.splitlines(keepends=True), filename)
    return filename


def _compiled(name: str, source: str, extra: dict | None = None):
    """Build a handler whose source ``inspect.getsource`` can read."""
    ns = _namespace()
    ns.update(extra or {})
    exec(compile(source, _register(name, source), "exec"), ns)
    return ns[name]


# ── the two over-refusals, each isolated ─────────────────────────────────────
def _dispatch_with_empty_helper():
    """A refusal helper that hands over NOTHING: a dict literal, no call."""
    return _compiled(
        "h_empty_helper",
        ("def _busy(exc):\n"
         "    return {'detail': 'busy'}\n"
         "def h_empty_helper(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy as exc:\n"
         "        return _busy(exc)\n"))


def _dispatch_with_value_builder_inline():
    """The same clause, with the value construction written IN the handler.

    No helper is followed here, so this arm isolates the value-constructor rule.
    """
    return _compiled(
        "h_inline_builder",
        ("def h_inline_builder(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy as exc:\n"
         "        return JSONResponse({'detail': str(exc)}, status_code=503)\n"))


def _dispatch_with_refusal_builder():
    """The product's own shape: both halves in one clause."""
    return _compiled(
        "h_refusal_builder",
        ("def _busy_response(exc):\n"
         "    return JSONResponse({'detail': str(exc), 'code': exc.code},\n"
         "                        status_code=503)\n"
         "def h_refusal_builder(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy as exc:\n"
         "        return _busy_response(exc)\n"))


def _dispatch_with_a_streaming_tail():
    """A generator that yields INERT values still binds the dispatch."""
    return _compiled(
        "h_stream_inert",
        ("def _chunks(service):\n"
         "    yield 'ok'\n"
         "def h_stream_inert(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return list(_chunks(service))\n"))


@pytest.mark.parametrize("build", [_dispatch_with_empty_helper,
                                   _dispatch_with_value_builder_inline,
                                   _dispatch_with_refusal_builder,
                                   _dispatch_with_a_streaming_tail])
def test_a_refusal_clause_that_builds_a_value_binds_the_dispatch(build):
    binding = binding_for(build(), "get_graph", TEMPLATE)
    assert binding.ok is True, binding
    assert binding.delivered == frozenset(), binding


def test_the_product_dispatch_route_binds_the_actions_it_serves():
    """The real endpoint, not a generated stand-in: this is the regression.

    Two of the actions below are public reads and one (``PRIVATE_ACTION``) is
    not. All three must bind: the handler hands the path parameter itself to the
    action call, so the route's declaration is that parameter's identity and
    does not depend on which action the caller asked for. The private one is
    listed because it is the body the negatives above use - not because the route
    declares it publicly.
    """
    endpoint = next(route.endpoint for route in state_router.routes
                    if getattr(route, "path", "") == TEMPLATE)
    for action in ("get_graph", "get_attempt", PRIVATE_ACTION):
        binding = binding_for(endpoint, action, TEMPLATE)
        assert binding.ok is True, (action, binding)


# ── the fail-closed rule survives: none of these may become a pass ──────────
def _local_builder_class():
    return _compiled(
        "h_local_builder",
        ("def h_local_builder(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return _Sneaky(service)\n"))


def _response_subclass():
    return _compiled(
        "h_response_subclass",
        ("def h_response_subclass(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return _LeakyResponse(service)\n"))


def _eval_in_a_delivered_position():
    return _compiled(
        "h_eval",
        ("def h_eval(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         f"        return eval(\"execute(service, {PRIVATE_ACTION!r}, {{}})\")\n"))


def _private_action_inside_a_response_constructor():
    return _compiled(
        "h_wrapped_private",
        ("def h_wrapped_private(service=Depends(get_service)):\n"
         f"    return JSONResponse(execute(service, {PRIVATE_ACTION!r},\n"
         f"                                {{'project_id': {GEN_PID!r}}}))\n"))


def _shadowed_builtin_in_a_local_def():
    """`str` is a nested `def` here: the reader's `_locals` branch decides, not identity."""
    return _compiled(
        "h_shadowed_str_local",
        ("def h_shadowed_str_local(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    def str(value):\n"
         f"        return execute(service, {PRIVATE_ACTION!r},\n"
         f"                       {{'project_id': {GEN_PID!r}}})\n"
         "    return _call(service, action, {'x': str(action)})\n"))


def _shadowed_builtin_in_a_module_global():
    """`str` bound to a State action runner in the module: identity must reject it."""
    leaky = lambda value: execute(value, PRIVATE_ACTION, {"project_id": GEN_PID})
    return _compiled(
        "h_shadowed_str_global",
        ("def h_shadowed_str_global(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return str(service)\n"),
        {"str": leaky})


def _shadowed_builtin_in_a_closure():
    """`str` is a CLOSURE variable of the handler - the product's own layout."""
    ns = _namespace()
    ns["_leaky"] = lambda value: execute(value, PRIVATE_ACTION,
                                         {"project_id": GEN_PID})
    source = ("def _make(leaky):\n"
              "    def str(value):\n"
              "        return leaky(value)\n"
              "    def h_closure(action: str, arguments: dict, service=Depends(get_service)):\n"
              "        try:\n"
              "            return _call(service, action, arguments)\n"
              "        except StateWriteBusy:\n"
              "            return str(service)\n"
              "    return h_closure\n")
    exec(compile(source, _register("h_closure", source), "exec"), ns)
    return ns["_make"](ns["_leaky"])


def _shadowed_builtin_by_a_local_import():
    """`str` is a FUNCTION-LOCAL import of the action callable."""
    return _compiled(
        "h_shadowed_str_import",
        ("def h_shadowed_str_import(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    from core.state_commands import execute as str\n"
         "    return {'graph': _call(service, action, {}),\n"
         f"            'mail': str(service, {PRIVATE_ACTION!r},\n"
         f"                        {{'project_id': {GEN_PID!r}}})}}\n"))


def _helper_whose_own_module_shadows_the_response_class():
    """The helper lives where `JSONResponse` is a leaky subclass, not starlette's."""
    handler_ns = _namespace()
    helper_ns = dict(handler_ns)
    helper_ns["JSONResponse"] = handler_ns["_LeakyResponse"]
    helper_source = ("def _busy_response(exc):\n"
                     "    return JSONResponse({'detail': 'busy'})\n")
    exec(compile(helper_source, _register("helper_module", helper_source), "exec"),
         helper_ns)
    handler_ns["_busy_response"] = helper_ns["_busy_response"]
    source = ("def h_cross_module(action: str, arguments: dict, service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy:\n"
              "        return _busy_response(service)\n")
    exec(compile(source, _register("h_cross_module", source), "exec"), handler_ns)
    return handler_ns["h_cross_module"]


def _private_action_a_yield_carries():
    """The private body leaves through a generator the handler returns."""
    return _compiled(
        "h_yielded_tail",
        ("def _tail(service):\n"
         f"    yield execute(service, {PRIVATE_ACTION!r}, {{'project_id': {GEN_PID!r}}})\n"
         "def h_yielded_tail(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    return [_call(service, action, arguments), _tail(service)]\n"))



def _helper_name_bound_by_a_closure():
    """The helper NAME is a closure variable; a harmless global has the same name.

    The global the reader would resolve hands over nothing, but the object the
    handler actually calls is the enclosing parameter.
    """
    leaky = lambda service: execute(service, PRIVATE_ACTION, {"project_id": GEN_PID})
    ns = _namespace()
    ns["_leaky"] = leaky
    source = ("def _busy(exc):\n"
              "    return {'detail': 'busy'}\n"
              "def _make(_busy):\n"
              "    def h_helper_closure(action: str, arguments: dict,\n"
              "                         service=Depends(get_service)):\n"
              "        try:\n"
              "            return _call(service, action, arguments)\n"
              "        except StateWriteBusy:\n"
              "            return _busy(service)\n"
              "    return h_helper_closure\n")
    exec(compile(source, _register("h_helper_closure", source), "exec"), ns)
    return ns["_make"](leaky)


def _helper_name_rebound_locally():
    """The helper name is rebound by an assignment in the handler itself."""
    return _compiled(
        "h_helper_rebound",
        ("def _busy(exc):\n"
         "    return {'detail': 'busy'}\n"
         "def h_helper_rebound(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    _busy = _leaky\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return _busy(service)\n"),
        {"_leaky": lambda service: execute(service, PRIVATE_ACTION,
                                           {"project_id": GEN_PID})})


def _response_class_captured_by_a_match_pattern():
    """`case JSONResponse:` CAPTURES the name; it is not a comparison."""
    return _compiled(
        "h_match_capture",
        ("def h_match_capture(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    match action:\n"
         "        case 'busy':\n"
         "            pass\n"
         "        case JSONResponse:\n"
         "            pass\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return JSONResponse(service)\n"))


def _builtin_captured_by_a_match_sequence_pattern():
    """The capture sits inside a sequence pattern: `case [str]:` binds `str`."""
    return _compiled(
        "h_match_sequence_capture",
        ("def h_match_sequence_capture(action: str, arguments: dict,\n"
         "                             service=Depends(get_service)):\n"
         "    match [action]:\n"
         "        case [str]:\n"
         "            pass\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return str(service)\n"))


def _module_rebinds_its_own_builtins():
    """The module replaces `__builtins__` so `str` is a State action runner."""
    ns = _namespace()
    leaky = lambda service: execute(service, PRIVATE_ACTION, {"project_id": GEN_PID})
    ns["__builtins__"] = dict(vars(builtins), str=leaky)
    source = ("def h_builtins_override(action: str, arguments: dict,\n"
              "                          service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy:\n"
              "        return str(service)\n")
    exec(compile(source, _register("h_builtins_override", source), "exec"), ns)
    return ns["h_builtins_override"]


def _helper_reached_through_functools_wraps():
    """`@functools.wraps` makes the WRAPPER run while its source reads as the inner def."""
    return _compiled(
        "h_wraps_helper",
        ("def _deco(fn):\n"
         "    @functools.wraps(fn)\n"
         "    def wrapper(*a, **k):\n"
         f"        return execute(a[0], {PRIVATE_ACTION!r},\n"
         f"                       {{'project_id': {GEN_PID!r}}})\n"
         "    return wrapper\n"
         "@_deco\n"
         "def _busy(exc):\n"
         "    return {'detail': 'busy'}\n"
         "def h_wraps_helper(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return _busy(service)\n"),
        {"functools": functools})


def _private_action_a_nested_generator_yields():
    """The generator yields a CALL; only the nested helper spells the action."""
    return _compiled(
        "h_nested_yield",
        ("def _leaf(service):\n"
         f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {GEN_PID!r}}})\n"
         "def _tail(service):\n"
         "    yield _leaf(service)\n"
         "def h_nested_yield(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return list(_tail(service))\n"))


def _response_class_rebound_after_the_read():
    """A global rebound AFTER the reader looked at it must not keep a cached pass."""
    ns = _namespace()
    source = ("def h_rebound_class(action: str, arguments: dict,\n"
              "                       service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy:\n"
              "        return JSONResponse({'detail': 'busy'})\n")
    exec(compile(source, _register("h_rebound_class", source), "exec"), ns)
    handler = ns["h_rebound_class"]
    assert binding_for(handler, "get_graph", TEMPLATE).ok is True
    ns["JSONResponse"] = ns["_LeakyResponse"]
    return handler



def _private_action_a_yield_from_carries():
    """`yield from` a helper that RETURNS the private action.

    The statement is a `YieldFrom`, not an `Expr(Yield)`, so a reader that only
    treats a bare yield as a delivery position misses it - while a caller that
    iterates the generator gets the private body.
    """
    return _compiled(
        "h_yield_from",
        ("def _tail(service):\n"
         f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {GEN_PID!r}}})\n"
         "def _gen(service):\n"
         "    yield from _tail(service)\n"
         "def h_yield_from(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        return list(_gen(service))\n"))


def _private_action_a_yield_assignment_carries():
    """`got = yield helper(service)`: the yield is the VALUE of an assignment."""
    return _compiled(
        "h_yield_assignment",
        ("def _tail(service):\n"
         f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {GEN_PID!r}}})\n"
         "def h_yield_assignment(action: str, arguments: dict,\n"
         "                       service=Depends(get_service)):\n"
         "    try:\n"
         "        return _call(service, action, arguments)\n"
         "    except StateWriteBusy:\n"
         "        _sent = yield _tail(service)\n"
         "        return [_sent]\n"))


def _builtin_captured_at_creation():
    """The function keeps the `__builtins__` it was created under, not the module's.

    CPython copies the frame's builtins into the function object, so a module that
    restores `__builtins__` afterwards still runs `str` from the leaky namespace.
    """
    ns = _namespace()
    leaky = lambda service: execute(service, PRIVATE_ACTION, {"project_id": GEN_PID})
    ns["__builtins__"] = dict(vars(builtins), str=leaky)
    source = ("def h_captured_builtins(action: str, arguments: dict,\n"
              "                            service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy:\n"
              "        return str(service)\n")
    exec(compile(source, _register("h_captured_builtins", source), "exec"), ns)
    ns["__builtins__"] = builtins
    return ns["h_captured_builtins"]


def _builtins_namespace_mutated_after_the_read():
    """The SAME builtins dict gains a leaky `str` after the reader looked at it."""
    ns = _namespace()
    ns["__builtins__"] = dict(vars(builtins))
    source = ("def h_mutated_builtins(action: str, arguments: dict,\n"
              "                           service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy:\n"
              "        return str(service)\n")
    exec(compile(source, _register("h_mutated_builtins", source), "exec"), ns)
    handler = ns["h_mutated_builtins"]
    assert binding_for(handler, "get_graph", TEMPLATE).ok is True
    ns["__builtins__"]["str"] = lambda service: execute(service, PRIVATE_ACTION,
                                                        {"project_id": GEN_PID})
    return handler


def _helper_body_swapped_after_the_read():
    """The followed helper's `__code__` is swapped; the function object is the same."""
    ns = _namespace()
    source = ("def _busy(exc):\n"
              "    return {'detail': 'busy'}\n"
              "def h_swapped_body(action: str, arguments: dict,\n"
              "                   service=Depends(get_service)):\n"
              "    try:\n"
              "        return _call(service, action, arguments)\n"
              "    except StateWriteBusy as exc:\n"
              "        return _busy(exc)\n")
    exec(compile(source, _register("h_swapped_body", source), "exec"), ns)
    handler = ns["h_swapped_body"]
    assert binding_for(handler, "get_graph", TEMPLATE).ok is True
    ns["_busy"].__code__ = _leaky_busy.__code__
    return handler


def _leaky_busy(exc):
    """The replacement body: it runs the private action instead of formatting."""
    return execute(None, PRIVATE_ACTION, {"project_id": GEN_PID})



@pytest.mark.parametrize("build", [_local_builder_class, _response_subclass,
                                   _eval_in_a_delivered_position,
                                   _private_action_inside_a_response_constructor,
                                   _shadowed_builtin_in_a_local_def,
                                   _shadowed_builtin_in_a_module_global,
                                   _shadowed_builtin_in_a_closure,
                                   _shadowed_builtin_by_a_local_import,
                                   _helper_whose_own_module_shadows_the_response_class,
                                   _private_action_a_yield_carries,
                                   _helper_name_bound_by_a_closure,
                                   _helper_name_rebound_locally,
                                   _response_class_captured_by_a_match_pattern,
                                   _builtin_captured_by_a_match_sequence_pattern,
                                   _module_rebinds_its_own_builtins,
                                   _helper_reached_through_functools_wraps,
                                   _private_action_a_nested_generator_yields,
                                   _response_class_rebound_after_the_read,
                                   _private_action_a_yield_from_carries,
                                   _private_action_a_yield_assignment_carries,
                                   _builtin_captured_at_creation,
                                   _builtins_namespace_mutated_after_the_read,
                                   _helper_body_swapped_after_the_read])
def test_a_delivery_the_reader_cannot_explain_is_still_refused(build):
    binding = binding_for(build(), "get_graph", TEMPLATE)
    assert binding.ok is False, (binding,)


# ── the eight measured delivery shapes, at the door ──────────────────────────
# Everything above decides a shape by CALLING the reader. These witnesses make
# the request instead: the product's dispatch route and the router's own guard,
# with the row's handler installed on that route. The control in every row is a
# handler that delivers the action the guard judged DIRECTLY, so a green row
# cannot be a route that answers 403 for some unrelated reason.
DOOR_REFUSAL = "state route declaration does not match its handler"
# The one visibility table decides which actions are public - derived here, and
# again for the servable one, so no list of action names is hand-written.
PUBLIC_ACTIONS = sorted(action for action in READ_REQUESTS if is_public_read(action))


def _busy_call(service, action, arguments=None, write=False):
    """The dispatch never gets the lock, so the row's leaky branch is what RUNS.

    The house `_namespace` `_call` asserts instead, on purpose: the reader must
    never run a handler. These witnesses do run one, so their dispatch refuses the
    lock and the handler takes the branch each shape hides behind.
    """
    raise StateWriteBusy("the write lock was busy")


def _door_compiled(name: str, source: str, extra: dict | None = None) -> dict:
    """Compile witness sources for the door, with a dispatch that is always busy."""
    namespace = _namespace()
    namespace["_call"] = _busy_call
    namespace.update(extra or {})
    exec(compile(source, _register(name, source), "exec"), namespace)
    return namespace


def _door_service(tmp_path, name: str) -> StateService:
    """A service whose private mailbox is seeded and whose reads are not refused
    again at execution: what the door decides is the only thing left in the way."""
    service = StateService(StateDatabase(str(tmp_path / f"door-{name}.sqlite")),
                           actor="door-seeder", project_read_trusted=True)
    service.create_project(GEN_PID, GEN_PID)
    seed_private_mail(service, GEN_PID, LEAK_MARK)
    service.open_project(GEN_PID)
    return service


def _served_public_action(service) -> str:
    """A PUBLIC read the product actually serves for the fixture project.

    Derived from the same table the guard consults, not hand-written: the control
    handler has to deliver the action the route was asked for, so the action must
    be one the product can answer with this project's arguments.
    """
    for action in PUBLIC_ACTIONS:
        try:
            execute(service, action, {"project_id": GEN_PID})
        except Exception:                     # not answerable with these arguments
            continue
        return action
    raise AssertionError("the product serves none of its public reads here")


def _door(handler, service) -> FastAPI:
    """The product's dispatch route and its own guard, with `handler` installed.

    The handler carries the declaration the product's dispatch family carries
    (`("read", None)`: the action lives in the URL), is added under the same
    template and moved in front of the product's own copy of the route, and the
    router's own guard is attached to it - so the request is judged exactly as the
    product's route is judged.
    """
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    setattr(handler, DECLARATION_ATTR, ("read", None))
    app.router.add_api_route(
        TEMPLATE, handler, methods=["POST"],
        dependencies=[Depends(state_router.dependencies[0].dependency)])
    app.router.routes.insert(0, app.router.routes.pop())
    return app


def _post(app, action: str):
    with TestClient(app) as client:
        return client.post(f"/api/state/query/{action}",
                           json={"project_id": GEN_PID})


_CONTROL_SOURCE = (
    "def control(action: str, arguments: dict, service=Depends(get_service)):\n"
    "    return execute(service, action, arguments)\n")


def _assert_control_serves(service, action: str) -> None:
    """The control pole: a handler that delivers the judged action directly."""
    control = _compiled("control", _CONTROL_SOURCE)
    served = _post(_door(control, service), action)
    assert served.status_code == 200, (served.status_code, served.text[:300])
    assert LEAK_MARK not in served.text


def _assert_the_shape_leaks_when_run(handler, service) -> None:
    """The shape really runs the private action when it is RUN.

    Measured, not assumed: the door refuses it before it runs on this tree, and on
    a tree whose reader accepts it the same handler answers 200 with this text.
    Without this half a witness could rot into "some handler is refused".
    """
    out = handler(action="get_graph", arguments={"project_id": GEN_PID},
                  service=service)
    text = str(out) + repr(getattr(out, "body", b""))
    assert LEAK_MARK in text, text[:300]


def _assert_door_refuses(app, action: str) -> None:
    refused = _post(app, action)
    assert refused.status_code == 403, (refused.status_code, refused.text[:300])
    assert refused.json()["detail"] == DOOR_REFUSAL, refused.text[:300]
    assert LEAK_MARK not in refused.text


_N8_SOURCE = f'''
class _Helpers:
    def leak(self, service):
        return execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}})

_helpers = _Helpers()

def h_n8(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return _helpers.leak(service)
'''


def test_n8_attribute_callee_is_refused_at_the_door(tmp_path):
    """N8: a callee reached through an ATTRIBUTE, delivered."""
    service = _door_service(tmp_path, "n8")
    action = _served_public_action(service)
    handler = _door_compiled("h_n8", _N8_SOURCE)["h_n8"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_N9_SOURCE = f'''
def _leak(service):
    return execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}})

def h_n9(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return [_leak][0](service)
'''


def test_n9_subscript_callee_is_refused_at_the_door(tmp_path):
    """N9: a callee reached through a SUBSCRIPT, delivered."""
    service = _door_service(tmp_path, "n9")
    action = _served_public_action(service)
    handler = _door_compiled("h_n9", _N9_SOURCE)["h_n9"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_N12_SOURCE = f'''
class _LeakyStr:
    def __str__(self):
        return str(execute(SERVICE, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}}))

_OBJ = _LeakyStr()

def h_n12(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return str(_OBJ)
'''


def test_n12_str_of_an_unexplainable_object_is_refused_at_the_door(tmp_path):
    """N12: an inert-looking `str(obj)` whose `__str__` runs the private action.

    `str` is inert as a CALLEE; the value it is handed is not covered by that, and
    a free name resolving to an arbitrary object is a value this reader cannot
    attribute.
    """
    service = _door_service(tmp_path, "n12")
    action = _served_public_action(service)
    handler = _door_compiled("h_n12", _N12_SOURCE, {"SERVICE": service})["h_n12"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_N12B_SOURCE = f'''
class _LeakyStr:
    def __str__(self):
        return str(execute(SERVICE, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}}))

_OBJ = _LeakyStr()

def h_n12b(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return _OBJ
'''


def test_n12b_bare_unexplainable_object_is_refused_at_the_door(tmp_path):
    """N12b: the same object returned BARE - the row proving N12 is not about `str`."""
    service = _door_service(tmp_path, "n12b")
    action = _served_public_action(service)
    handler = _door_compiled("h_n12b", _N12B_SOURCE, {"SERVICE": service})["h_n12b"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_C1_SOURCE = f'''
class _LeakyPartialResponse(JSONResponse):
    def __init__(self, service):
        super().__init__(
            execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}}))

def h_c1(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return functools.partial(_LeakyPartialResponse)(service)
'''


def test_c1_partial_of_a_value_constructor_is_refused_at_the_door(tmp_path):
    """C1: `functools.partial(LeakyResponse)(service)` - a callee built by a CALL."""
    service = _door_service(tmp_path, "c1")
    action = _served_public_action(service)
    handler = _door_compiled("h_c1", _C1_SOURCE, {"functools": functools})["h_c1"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_E9_SOURCE = f'''
def h_e9(action: str, arguments: dict, service=Depends(get_service)):
    return execute(service, action, arguments)

def _leaky_e9(action: str, arguments: dict, service=None):
    return execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}})
'''


def test_e9_handler_body_swapped_after_the_first_read_is_refused_at_the_door(tmp_path):
    """E9: the HANDLER's own `__code__` changes under the cached read."""
    service = _door_service(tmp_path, "e9")
    action = _served_public_action(service)
    namespace = _door_compiled("h_e9", _E9_SOURCE)
    handler = namespace["h_e9"]
    app = _door(handler, service)

    # The first read: the handler as written delivers the action the route takes
    # from its path, so the door serves it.
    first = _post(app, action)
    assert first.status_code == 200, (first.status_code, first.text[:300])

    # The body that changes is the HANDLER'S OWN - not a global, not a helper.
    handler.__code__ = namespace["_leaky_e9"].__code__
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(app, action)
    _assert_control_serves(service, action)


_E10_SOURCE = f'''
def _leak(service):
    return execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}})

def h_e10(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        x = _leak(service)
        return x
'''


def test_e10_local_variable_flow_is_refused_at_the_door(tmp_path):
    """E10: the private body reaches a `return` through a LOCAL VARIABLE."""
    service = _door_service(tmp_path, "e10")
    action = _served_public_action(service)
    handler = _door_compiled("h_e10", _E10_SOURCE)["h_e10"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(_door(handler, service), action)
    _assert_control_serves(service, action)


_E11_SOURCE = f'''
def h_e11(action: str, arguments: dict, service=Depends(get_service)):
    try:
        return _call(service, action, arguments)
    except StateWriteBusy:
        return runner(service, action, arguments)

def _leak_runner(service, action, arguments):
    return execute(service, {PRIVATE_ACTION!r}, {{"project_id": {GEN_PID!r}}})
'''


def test_e11_execute_alias_rebound_after_the_first_read_is_refused_at_the_door(tmp_path):
    """E11: the name that resolved to `execute` is rebound after the first read."""
    service = _door_service(tmp_path, "e11")
    action = _served_public_action(service)
    namespace = _door_compiled("h_e11", _E11_SOURCE, {"runner": execute})
    handler = namespace["h_e11"]
    app = _door(handler, service)

    # The first read: `runner` IS the action callable, and the call is the judged
    # path parameter, so the dispatch binds.
    first = _post(app, action)
    assert first.status_code == 200, (first.status_code, first.text[:300])

    # The same name, resolving somewhere else. Only the identity recorded during
    # the first read can notice.
    namespace["runner"] = namespace["_leak_runner"]
    _assert_the_shape_leaks_when_run(handler, service)
    _assert_door_refuses(app, action)
    _assert_control_serves(service, action)


def test_the_dispatch_wrapper_is_transparent_only_for_an_action_call():
    """The one accepted wrapper cannot become a way around the callee rule.

    `anyio.to_thread.run_sync` is whitelisted by object identity because it only
    runs the callable it is handed; that is exactly why it may be transparent ONLY
    when what it is handed is a recognized action call. Handed a function this
    reader never read, it is a delivery it cannot explain.
    """
    from anyio import to_thread

    wrapper_running_an_unread_callee = _compiled(
        "h_unread_wrapper",
        ("def h_unread_wrapper(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    return to_thread.run_sync(_busy, service)\n"),
        {"to_thread": to_thread, "_busy": _busy_call})
    binding = binding_for(wrapper_running_an_unread_callee, "get_graph", TEMPLATE)
    assert binding.ok is False, binding

    wrapper_running_the_action_call = _compiled(
        "h_action_wrapper",
        ("def h_action_wrapper(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    return to_thread.run_sync(partial(_call, service, action, arguments))\n"),
        {"to_thread": to_thread, "partial": functools.partial})
    binding = binding_for(wrapper_running_the_action_call, "get_graph", TEMPLATE)
    assert binding.ok is True, binding


def test_every_public_read_still_binds_the_dispatch_door():
    """No over-tightening: the product's OWN dispatch route still serves them all.

    The action set is derived from the one visibility table, and the endpoint from
    the product's own router, so neither the answer nor the question is a list
    somebody could keep in step by hand.
    """
    assert PUBLIC_ACTIONS, "the visibility table declares no public read"
    endpoint = next(route.endpoint for route in state_router.routes
                    if getattr(route, "path", "") == TEMPLATE)
    refused = [action for action in PUBLIC_ACTIONS
               if not binding_for(endpoint, action, TEMPLATE).ok]
    assert refused == [], refused
