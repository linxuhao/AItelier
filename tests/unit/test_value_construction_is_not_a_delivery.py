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
``yield`` carries - directly or from a nested helper - and a response class the
module rebinds AFTER the reader looked at it. Each must still be refused.
"""
from __future__ import annotations

import builtins
import functools
import linecache

import pytest
from fastapi import Depends
from starlette.responses import JSONResponse

from api.state_graph_routers import get_service
from api.state_graph_routers import router as state_router
from api.state_verdict import binding_for
from core.state_commands import execute
from core.state_graph import StateWriteBusy
from tests.support.state_author_surface import GEN_PID, PRIVATE_ACTION

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


@pytest.mark.parametrize("build", [_dispatch_with_empty_helper,
                                   _dispatch_with_value_builder_inline,
                                   _dispatch_with_refusal_builder])
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
                                   _response_class_rebound_after_the_read])
def test_a_delivery_the_reader_cannot_explain_is_still_refused(build):
    binding = binding_for(build(), "get_graph", TEMPLATE)
    assert binding.ok is False, (binding,)
