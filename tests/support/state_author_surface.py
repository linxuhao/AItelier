"""Enumerate the author-controllable surface of a ``/api/state`` route.

This module OBSERVES the author-controllable surface of the route-layer guard:
it generates, from the dimensions a route author actually controls, route
shapes and records whether the guard applied a judgment to each. It measures
the guard's REACH, not confidentiality - an approval here is not a grant, since
what a caller may read is decided where the private read EXECUTES
(`core.state_privacy.refuse_private_read`).

This is TEST SCAFFOLDING and lives under ``tests/`` on purpose: the running
application imports nothing here, and it must never grow into an API-package
module a product route could reach for.

`compile_handler` builds each shape as a real Python function whose source
`inspect.getsource` can read (the lines are registered in `linecache` under a
virtual file name), so the binding runs on it exactly as it does on a handler
written into a file. The one shape whose source is deliberately UNREADABLE is
the refuse pole: an author-writable route this reader cannot classify must be
refused, never approved.

There is NO hand-written table of "what each body delivers". The expected
model is DERIVED, per shape, from the generated source text itself by a
second, independent scan (see `_model_delivery`): the two computations share
the generated handler, not a hand-kept dictionary keyed by body name.
"""
from __future__ import annotations

import functools
import itertools
import linecache
import re
from typing import NamedTuple


# The project every generated handler reads, opened by the driver so a private
# delivery is actually servable - which is what makes a leak detectable.
GEN_PID = "author-surface-open"

# A distinctive string placed in that project's private director mail. The
# universal safety invariant is that NO response, for ANY generated shape, may
# carry this text through the prefix guard: if it does, the verdict failed.
LEAK_MARK = "AUTHOR-SURFACE-LEAK-9b1c"


def _private_mailbox_action() -> str:
    """The PRIVATE action every generated handler delivers.

    An opened project's working notes are public (owner ruling 2026-09-22,
    note://aitelier/546f3b521eca), so a note read can no longer be the private
    delivery. The director mailbox stays writer-only; its visibility is READ
    from the one classification table here, so a change to that table fails
    this module instead of quietly turning the private pole public.
    """
    from core.state_commands import read_visibility
    action = "list_director_messages"
    assert read_visibility(action) == "private", (action, read_visibility(action))
    return action


PRIVATE_ACTION = _private_mailbox_action()


def seed_private_mail(service, project_id: str, secret: str,
                      sender: str = "private-mail-sender") -> None:
    """Deliver ``secret`` (subject and body) to ``project_id``'s director
    mailbox through a TRUSTED ``service``, creating the sender if needed."""
    known = {p["project_id"] for p in service.store.list_projects(public_only=False)}
    if sender not in known:
        service.create_project(sender, sender)
    result = service.director_messages.send_director_message(
        sender, "director", f"private-mail-{project_id}", secret, secret,
        target_project_id=project_id)
    assert "error" not in result, result


class Param:
    """Name of the path parameter the handler dispatches on."""
    ACTION = "action"
    NOTE = "note"
    THING = "thing"


class Body:
    """What the handler's source delivers, in the shape `state_verdict` reads."""
    PUBLIC_ONLY = "public_only"
    PRIVATE_ONLY = "private_only"
    DISPATCH_PARAM = "dispatch_param"
    DISPATCH_AND_PRIVATE = "dispatch_and_private"   # the closed carrier
    MULTI_PUBLIC = "multi_public"
    OPAQUE_ACTION = "opaque_action"
    STATIC = "static"
    UNKNOWN_SOURCE = "unknown_source"               # the refuse-pole shape


class Decl:
    """The declaration the author writes on the endpoint: (kind, action)."""
    PUBLIC_GET_GRAPH = ("read", "get_graph")
    PRIVATE_MAIL = ("read", PRIVATE_ACTION)
    DISPATCH_FAMILY = ("read", None)


class Shape(NamedTuple):
    name: str
    param: str
    template: str
    method: str
    body: str
    declaration: tuple
    stand_down_attr: bool
    label: str
    intent: str


_BODIES = (
    Body.PUBLIC_ONLY,
    Body.PRIVATE_ONLY,
    Body.DISPATCH_PARAM,
    Body.DISPATCH_AND_PRIVATE,
    Body.MULTI_PUBLIC,
    Body.OPAQUE_ACTION,
    Body.STATIC,
)

_PARAMS = (Param.ACTION, Param.NOTE, Param.THING)
_METHODS = ("GET", "POST")
_STAND_DOWN = (False, True)


def _template_for(param: str) -> str:
    if param == Param.THING:
        return "/api/state/gen/fixed"
    return "/api/state/gen/{" + param + "}"


def _decl_for(body: str) -> tuple:
    """A declaration an author would plausibly write for this body.

    Honesty is NOT assumed: a private-delivering body is declared PUBLIC here,
    which is the forgery the property must refuse. The dispatch family declares
    ``None`` (the shape the real ``/query/{action}`` route declares).
    """
    if body in (Body.DISPATCH_PARAM, Body.DISPATCH_AND_PRIVATE):
        return Decl.DISPATCH_FAMILY
    return Decl.PUBLIC_GET_GRAPH


def _intent(body: str, stand_down_attr: bool) -> str:
    base = {
        Body.PUBLIC_ONLY: "honest public read - may serve a public body",
        Body.PRIVATE_ONLY: "declares public, delivers private - must refuse",
        Body.DISPATCH_PARAM: "genuine dispatch on the judged parameter",
        Body.DISPATCH_AND_PRIVATE: "carrier: dispatch + a private return - must refuse",
        Body.MULTI_PUBLIC: "two public reads, one returned - may serve public",
        Body.OPAQUE_ACTION: "delivers a non-literal action - must refuse",
        Body.STATIC: "delivers no state action - must refuse",
    }[body]
    if stand_down_attr:
        base += " (endpoint also carries a fabricated stand-down attribute)"
    return base


def shapes() -> list:
    """Every author-writable route shape, as a generated cross product."""
    out: list[Shape] = []
    for param, method, body, stand in itertools.product(
            _PARAMS, _METHODS, _BODIES, _STAND_DOWN):
        out.append(Shape(
            name="h_" + "_".join((body, param, method, "sd" if stand else "ns")),
            param=param, template=_template_for(param), method=method, body=body,
            declaration=_decl_for(body), stand_down_attr=stand,
            label=f"{body}/{{p:{param}}} {method} sd={stand}",
            intent=_intent(body, stand),
        ))
    # The refuse-pole shape is generated but not cross-producted: its handler is
    # built at runtime so `inspect.getsource` cannot read it.
    out.append(Shape(
        name="h_unknown_source", param=Param.ACTION,
        template="/api/state/gen/{action}", method="GET",
        body=Body.UNKNOWN_SOURCE, declaration=Decl.PUBLIC_GET_GRAPH,
        stand_down_attr=False, label="unknown_source (unreadable)",
        intent="handler built at runtime, source unreadable - must refuse",
    ))
    return out


def shape_count() -> int:
    return len(shapes())


def _model_delivery(shape: Shape) -> tuple:
    """DERIVE the shape's delivery from its generated source text.

    A second scan over the SAME generated handler - not a hand-kept dictionary
    keyed by body name - yields (literal actions delivered, whether the source
    dispatches on the shape's own path parameter, whether any action slot is
    unresolvable). `expected_can_serve` is computed from THIS, so the model and
    the reader share an input (the handler) but no hand-written table.
    """
    source = _handler_source(shape.param, shape.body, shape.name)
    literals = frozenset(re.findall(r"execute\(service,\s*'([a-z_]+)'", source))
    dispatches = bool(re.search(
        r"execute\(service,\s*" + re.escape(shape.param) + r"\b", source))
    opaque = "get_' + 'graph'" in source
    return literals, dispatches, opaque


def expected_can_serve(shape: Shape) -> bool:
    """Whether an author-writable shape is allowed to answer 200, derived here.

    A shape may serve only when: the reader can classify its source at all; every
    action it delivers is a public read; it is not dispatching on a non-literal
    (an action the reader cannot resolve fails closed); and the declared action is
    one the handler actually delivers. The genuine dispatch family may serve only
    when it hands back the very path parameter the guard judged (``action`` in a
    ``{action}`` path) and returns nothing private - the carrier that dispatches
    AND returns private mail is refused here.
    """
    if shape.declaration[0] != "read":
        return False
    if shape.body == Body.UNKNOWN_SOURCE:
        return False
    literals, dispatches, opaque = _model_delivery(shape)
    if opaque:
        return False
    if any(not _is_public(a) for a in literals):
        return False
    action = shape.declaration[1]
    if action is None:
        return dispatches and shape.param == Param.ACTION and not literals
    return action in literals


def _is_public(action: str) -> bool:
    from core.state_commands import is_public_read
    return is_public_read(action)


def carrier_shapes() -> dict:
    """The four named carriers, one generated instance each.

    Each is a shape a route author used, in an earlier round, to try to move an
    exemption rather than remove it. The generator records the guard's judgment
    of each; a shape the guard stops judging shows up here first.
    """
    index = {s.name: s for s in shapes()}
    return {
        "carrier1_endpoint_attr": index["h_private_only_action_GET_sd"],
        "carrier2_dependency_attr": index["h_private_only_note_GET_sd"],
        "carrier3_object_identity": index["h_private_only_thing_GET_sd"],
        "carrier4_param_name_template": index["h_dispatch_and_private_action_GET_ns"],
    }


def _handler_source(param: str, body: str, name: str) -> str:
    open_pid = repr(GEN_PID)
    lines = [f"def {name}({param}: str = 'get_graph', service=Depends(get_service)):"]
    if body == Body.PUBLIC_ONLY:
        lines.append(f"    return execute(service, 'get_graph', {{'project_id': {open_pid}}})")
    elif body == Body.PRIVATE_ONLY:
        lines.append(f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {open_pid}}})")
    elif body == Body.DISPATCH_PARAM:
        lines.append(f"    return execute(service, {param}, {{'project_id': {open_pid}}})")
    elif body == Body.DISPATCH_AND_PRIVATE:
        lines.append(f"    execute(service, {param}, {{'project_id': {open_pid}}})")
        lines.append(f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {open_pid}}})")
    elif body == Body.MULTI_PUBLIC:
        # Mentions/discards a second public read (binds nothing) and returns a
        # public read; the reader must see ONLY the returned action as delivered.
        lines.append(f"    execute(service, 'get_graph', {{'project_id': {open_pid}}})  # discarded, binds nothing")
        lines.append(f"    return execute(service, 'get_graph', {{'project_id': {open_pid}}})  # delivered")
    elif body == Body.OPAQUE_ACTION:
        lines.append(f"    return execute(service, 'get_' + 'graph', {{'project_id': {open_pid}}})")
    else:  # STATIC
        lines.append("    return {'static': True}")
    return "\n".join(lines) + "\n"


def _register_source(name: str, source: str) -> str:
    filename = f"<author_surface/{name}>"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    return filename


def compile_handler(shape: Shape, *, namespace: dict):
    """Build a real, source-readable handler function for `shape`.

    `namespace` supplies the globals the handler source references: ``Depends``,
    ``get_service`` and ``execute``. The `UNKNOWN_SOURCE` shape is built from a
    string NOT registered in linecache, so its source is unreadable and the
    binding must refuse it.
    """
    if shape.body == Body.UNKNOWN_SOURCE:
        ns: dict = dict(namespace)
        src = (f"def _u({shape.param}: str = 'get_graph', service=Depends(get_service)):\n"
               f"    return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {GEN_PID!r}}})\n")
        exec(compile(src, "<unreadable_author_surface>", "exec"), ns)
        handler = ns["_u"]
    else:
        source = _handler_source(shape.param, shape.body, shape.name)
        filename = _register_source(shape.name, source)
        ns = dict(namespace)
        exec(compile(source, filename, "exec"), ns)
        handler = ns[shape.name]
    if shape.stand_down_attr:
        handler._prefix_verdict_stands_down_for = True
        handler._verdict_done = True
    handler.__api_surface_shape__ = shape
    return handler


def dependency_shapes():
    """Return name -> factory(service) producing a FastAPI dependency of each
    shape: ``def``, ``async def``, ``yield``, ``async yield``, a callable object
    and a class. These feed the guard-vs-FastAPI equivalence test (criterion 6):
    handing each dependency to the guard must release/refuse exactly as putting
    it in an ordinary ``Depends(D)`` route does.
    """
    def make_def(value):
        def dep():
            return value
        return dep

    def make_async_def(value):
        async def dep():
            return value
        return dep

    def make_yield(value):
        def dep():
            yield value
        return dep

    def make_async_yield(value):
        async def dep():
            yield value
        return dep

    def make_callable(value):
        class CallableDep:
            def __init__(self):
                self._value = value
            def __call__(self):
                return self._value
        return CallableDep()

    def make_class(value):
        class ClassDep:
            def __init__(self):
                self.value = value
        return ClassDep

    return {
        "def": make_def,
        "async_def": make_async_def,
        "yield": make_yield,
        "async_yield": make_async_yield,
        "callable": make_callable,
        "class": make_class,
    }


def hiding_handlers(namespace: dict) -> dict:
    """The four delivery-hiding handler shapes the review measured (r8), and a
    fifth that only the fail-closed ``opaque`` branch refuses.

    Each one delivers a PRIVATE action through a site the reader must work to
    see: a module-level helper, a renamed import, an ``async def`` inner
    function, and the action passed as the KEYWORD ``action=``. Every one must
    be refused; the reader derives each refusal, no shape list. All four are
    compiled into namespace copies so their ``__globals__`` are real, and the
    helper sources are registered in `linecache` exactly the way
    `compile_handler` does - the reader follows the helper or refuses.
    """
    out: dict = {}

    def build(name: str, source: str, extra: dict) -> object:
        ns = dict(namespace)
        ns.update(extra)
        filename = _register_source(name, source)
        exec(compile(source, filename, "exec"), ns)
        return ns[name]

    open_pid = repr(GEN_PID)
    # 1. module-level helper: the delivery lives OUTSIDE the handler source.
    out["module_level_helper"] = build(
        "h_module_helper",
        ("def _helper(service, action):\n"
         f"    return execute(service, action, {{'project_id': {open_pid}}})\n"
         "def h_module_helper(action: str = 'get_graph', service=Depends(get_service)):\n"
         f"    return _helper(service, {PRIVATE_ACTION!r})\n"),
        {})
    # 2. renamed import: the action-callable reaches the handler under an alias.
    out["renamed_import"] = build(
        "h_renamed_import",
        ("def h_renamed_import(action: str = 'get_graph', service=Depends(get_service)):\n"
         f"    return _runner(service, {PRIVATE_ACTION!r}, {{'project_id': {open_pid}}})\n"),
        {"_runner": namespace["execute"]})
    # 3. async inner function: the delivery is inside a coroutine the handler calls.
    out["async_inner"] = build(
        "h_async_inner",
        ("def h_async_inner(action: str = 'get_graph', service=Depends(get_service)):\n"
         "    async def inner():\n"
         f"        return execute(service, {PRIVATE_ACTION!r}, {{'project_id': {open_pid}}})\n"
         "    return inner()\n"),
        {})
    # 4. keyword action: the private action arrives as action=..., not positionally.
    out["keyword_action"] = build(
        "h_keyword",
        ("def h_keyword(action: str = 'get_graph', service=Depends(get_service)):\n"
         f"    return execute(service, action={PRIVATE_ACTION!r},\n"
         f"                   arguments={{'project_id': {open_pid}}})\n"),
        {})
    # 5. a PUBLIC literal delivered beside a callee the reader cannot resolve: a
    #    `functools.partial` is not a function, so the reader cannot follow it,
    #    and the private action it runs is invisible. Only the fail-closed
    #    `opaque` branch refuses this one - the public literal alone would bind.
    run = namespace["execute"]
    out["public_beside_unresolvable"] = build(
        "h_public_beside",
        ("def h_public_beside(action: str = 'get_graph', service=Depends(get_service)):\n"
         f"    return {{'graph': execute(service, 'get_graph', {{'project_id': {open_pid}}}),\n"
         "            'mail': _hidden(service)}\n"),
        {"_hidden": functools.partial(
            lambda service: run(service, PRIVATE_ACTION, {"project_id": GEN_PID}))})
    return out
