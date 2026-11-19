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

* a followed helper that hands over neither an action nor a parameter cannot
  move the dispatch identity — there is nothing to hand over;
* a call that only BUILDS A VALUE (``str``, ``len``, a framework response
  class) cannot run a State action in a body of its own, and its ARGUMENTS are
  walked like any other expression, so an action handed to it is still seen.

The negatives are what keeps that narrowing from being a hole: a locally
defined builder class, a subclass of a response class, ``eval``, a shadowed
``str``, and a private action passed INTO a response constructor are all still
refused.
"""
from __future__ import annotations

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


def _compiled(name: str, source: str, extra: dict | None = None):
    """Build a handler whose source ``inspect.getsource`` can read."""
    ns = _namespace()
    ns.update(extra or {})
    filename = f"<value_construction/{name}>"
    linecache.cache[filename] = (len(source), None,
                                 source.splitlines(keepends=True), filename)
    exec(compile(source, filename, "exec"), ns)
    return ns[name]


def _busy_body(inner: str) -> dict:
    return {"JSONResponse": JSONResponse, "str": str, "StateWriteBusy": StateWriteBusy}


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


def test_the_product_dispatch_route_binds_every_public_action():
    """The real endpoint, not a generated stand-in: this is the regression."""
    endpoint = next(route.endpoint for route in state_router.routes
                    if getattr(route, "path", "") == TEMPLATE)
    for action in ("get_graph", "get_attempt", "list_director_messages"):
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


def _shadowed_builtin():
    return _compiled(
        "h_shadowed_str",
        ("def h_shadowed_str(action: str, arguments: dict, service=Depends(get_service)):\n"
         "    def str(value):\n"
         f"        return execute(service, {PRIVATE_ACTION!r},\n"
         f"                       {{'project_id': {GEN_PID!r}}})\n"
         "    return _call(service, action, {'x': str(action)})\n"))


@pytest.mark.parametrize("build", [_local_builder_class, _response_subclass,
                                   _eval_in_a_delivered_position,
                                   _private_action_inside_a_response_constructor,
                                   _shadowed_builtin])
def test_a_delivery_the_reader_cannot_explain_is_still_refused(build):
    binding = binding_for(build(), "get_graph", TEMPLATE)
    assert binding.ok is False, (binding,)
