"""State-only HTTP routes reusable without importing the workflow host."""
import inspect
import json
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Annotated, Literal
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_parameterless_sub_dependant, solve_dependencies
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.routing import Match
from api.state_route_reader import DISPATCH, LITERAL, NO_ACTION, UNREADABLE, read_route
from core.state_commands import (READ_REQUESTS, WRITE_REQUESTS, describe, execute,
                                 is_public_read)
from core.state_graph import StateConflict, StateGraphError, StateNotFound

try:  # FastAPI computes this for every real route; reuse it rather than guess.
    from fastapi.dependencies.utils import _should_embed_body_fields
except ImportError:  # pragma: no cover - pinned FastAPI ships it
    _should_embed_body_fields = None

try:  # FastAPI >= 0.141 keeps an included router lazy in `app.routes`.
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - pinned FastAPI ships it
    iter_route_contexts = None

# The exit stacks FastAPI >= 0.141 solves dependencies against. Inside a route
# its own middleware has already put them in the scope; the prefix middleware
# runs outside that middleware, so it puts them there itself.
_EXIT_STACK_SCOPE_KEYS = ("fastapi_inner_astack", "fastapi_function_astack",
                          "fastapi_astack")


@asynccontextmanager
async def _exit_stacks(scope):
    async with AsyncExitStack() as stack:
        installed = []
        for key in _EXIT_STACK_SCOPE_KEYS:
            if not isinstance(scope.get(key), AsyncExitStack):
                scope[key] = await stack.enter_async_context(AsyncExitStack())
                installed.append(key)
        try:
            yield stack
        finally:
            for key in installed:
                scope.pop(key, None)


def route_contexts(app):
    """Every route of `app`, with the dependencies its inclusion added.

    `app.routes` no longer holds one entry per route: FastAPI keeps an included
    router as a single lazy entry, and the app-wide dependencies live on the
    INCLUSION rather than on the route. Reading `app.routes` directly therefore
    reports neither the routes nor the verdict they carry - which is how a
    coverage claim can be green over an app it never looked inside.
    """
    if iter_route_contexts is None:  # pragma: no cover - pinned FastAPI ships it
        return list(app.routes)
    return list(iter_route_contexts(app.routes))


# Each route declares WHAT it serves, on its own endpoint object: a tuple
# `(kind, action)` where `kind` is `read`, `write`, `director` or `schema` and
# `action` is the literal read action (or None for the action-dispatch
# families, whose action lives in the URL). This is the enumerable declaration
# the router guard reads, and the thing the invariant tests walk instead of a
# hardcoded door list.
STATE_ROUTE_DECLARATION = "_state_route"
_DECLARATION = STATE_ROUTE_DECLARATION  # legacy name used by the invariant tests

# The kinds the router guard has a branch for. A declaration naming anything
# else is REFUSED rather than sent to the branch that happens to be last: an
# unknown kind used to reach the literal-read arm, where a kind nobody had
# written a rule for was judged by whatever action it named - a public one
# opened the door. The default here is DENY, like everywhere else in this file.
DECLARATION_KINDS = ("read", "write", "director", "schema")

# The URL parameter the action-dispatch families take their action from. The
# guard reads `request.path_params[_DISPATCH_PARAMETER]`, so the independent
# reader must find the endpoint executing THAT parameter and no other: the two
# halves of the cross-check name the same thing or the route is refused.
_DISPATCH_PARAMETER = "action"

# The `/schema` REST route publishes the shape of every operation, closed
# families included. The owner has not ruled on opening it, so it stays shut -
# written down as a NAME so it is a decision somebody made, not a route somebody
# forgot. It executes no state action, which is why it declares kind `schema`
# rather than a read of an action nobody could cross-check.
SCHEMA_DECLARED_ACTION = "get_state_schema"

# Names the real verdict objects carry so a dump, a trace or a test can say
# which object it is looking at. They are DESCRIPTIVE and nothing in this module
# reads them to decide anything: a route that sets all three - on its endpoint,
# on a dependency, on a sub-dependency, on another router's dependency or on a
# callable class - is judged exactly like a route that sets none.
# `tests/unit/test_no_route_author_can_stand_the_verdict_down.py` derives this
# list out of this file and attacks every name in it in every one of those
# positions, so a name added here is attacked without anybody editing the test.
STATE_VERDICT_MARK = "_is_state_verdict"
STATE_ROUTER_GUARD_MARK = "_is_state_router_guard"
STATE_PREFIX_VERDICT_MARK = "_is_state_prefix_verdict"

# The verdict objects THIS PROCESS built. The question the prefix verdict has to
# answer before it stands down - "is this route already judged by a state
# router's own guard?" - is answered by looking for one of these objects in the
# dependency tree FastAPI built for the route, compared with `is`. A route's
# author can write any attribute they like and copy every attribute the real
# guard carries, its `__name__` included; the object itself is what is
# compared. Reaching the real object is no way round either: hanging it on a
# route of one's own means the real guard judges that route, and it refuses a
# route that declares nothing.
#
# Weak, so a router a test built stops being an answer as soon as the test
# drops it. A guard that is not in here is not recognised, and an unrecognised
# route is JUDGED rather than waved through: forgetting to register fails
# closed, which is the direction this whole module leans.
_ROUTER_GUARDS: weakref.WeakSet = weakref.WeakSet()
_PREFIX_VERDICTS: weakref.WeakSet = weakref.WeakSet()

STATE_PREFIX = "/api/state"


def is_state_prefix(path: str) -> bool:
    """Whether `path` is under the state prefix.

    `/api/state` and everything below it; `/api/statistics` is a different
    prefix and is not this module's business.
    """
    return path == STATE_PREFIX or path.startswith(STATE_PREFIX + "/")


def _dependant_calls(dependant):
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _dependant_calls(sub)


def _tree_holds(route, registry) -> bool:
    """Whether one of `registry`'s objects is in the tree FastAPI built."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    known = list(registry)
    return any(any(call is one for one in known)
               for call in _dependant_calls(dependant))


def route_carries_state_verdict(route) -> bool:
    """Whether FastAPI will run a state verdict this process built for this route.

    Read off `route.dependant` - the tree FastAPI built and will solve - and
    matched against the verdict objects themselves, so a route that merely looks
    like a state route, that sets the declaration attribute on its endpoint, or
    that hangs a dependency carrying every attribute a real guard carries, is
    not mistaken for a route that carries a verdict. A `Mount`, a bare Starlette
    `Route` and anything else with no dependant answer False.
    """
    return route_carries_router_guard(route) or route_carries_prefix_verdict(route)


def route_carries_router_guard(route) -> bool:
    """Whether a guard object a state router BUILT is in this route's tree."""
    return _tree_holds(route, _ROUTER_GUARDS)


def prefix_verdict_stands_down_for(route) -> bool:
    """The stand-down decision itself, as the request path makes it.

    Exported so a test can ask the question the running request asks and
    cross-check the answer against what actually happens to a request for that
    route. From r1 to r4 the two were never compared: coverage reported a
    verdict present while the verdict stood itself down and the route served
    the notebook to an anonymous caller.
    """
    return route_carries_router_guard(route)


def route_carries_prefix_verdict(route) -> bool:
    """Whether the APP-WIDE prefix verdict is part of this route's tree.

    Distinct from `route_carries_state_verdict`, and the distinction is the
    whole point: the state router's own routes carry that router's guard, so
    asking only "does this route carry SOME state verdict" answers yes for them
    whether or not the app installed the prefix verdict at all. The prefix
    verdict is what covers a route the state router does NOT own, and a route
    that does not carry it is a route the app is not ready to have one added
    next to.
    """
    return _tree_holds(route, _PREFIX_VERDICTS)


def _body_params(dependant) -> list:
    found = list(dependant.body_params)
    for sub in dependant.dependencies:
        found += _body_params(sub)
    return found


async def _body_for(request, body_params):
    if not body_params:
        return None
    raw = await request.body()
    if not raw:
        return None
    content_type = request.headers.get("content-type", "")
    if "form" in content_type or "multipart" in content_type:
        return await request.form()
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def apply_verdict(dependency, request) -> None:
    """Hand `dependency` to FastAPI and let FastAPI run it.

    The verdict this guard needs is chosen at REQUEST time (the read table
    decides which door a route is), and `Depends(...)` is resolved at route
    build time, so the choice cannot be expressed as a plain route dependency.
    What CAN be done - and is done here - is to stop executing the dependency
    in this repository at all: the dependency is wrapped in `Depends(...)`,
    hung under an empty `Dependant`, and handed to `solve_dependencies`, the
    same function `fastapi.routing.APIRoute`'s own request handler calls. Sync
    `def`, `async def`, `yield` and `async yield` generators, callable objects,
    `functools.partial`, sub-dependencies and `dependency_overrides` are
    therefore not cases this module knows about: they are cases FastAPI knows
    about. Nothing here inspects the dependency's shape and nothing here calls
    it.

    The default is REFUSE: a dependency FastAPI cannot solve - including one
    whose parameters do not resolve on this request - is a 403, never a silent
    pass.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    root = Dependant(path=path)
    try:
        root.dependencies.append(
            get_parameterless_sub_dependant(depends=Depends(dependency), path=path))
    except Exception as exc:  # fail CLOSED: an unmountable verdict denies
        raise HTTPException(403, "state verdict could not be applied") from exc
    body_params = _body_params(root)
    embed = bool(_should_embed_body_fields(body_params)) if _should_embed_body_fields else True
    try:
        body = await _body_for(request, body_params)
        async with _exit_stacks(request.scope) as stack:
            solved = await solve_dependencies(
                request=request, dependant=root, body=body,
                dependency_overrides_provider=request.scope.get("app"),
                async_exit_stack=stack, embed_body_fields=embed)
    except HTTPException:
        raise
    except Exception as exc:  # fail CLOSED: an unappliable verdict denies
        raise HTTPException(403, "state verdict could not be applied") from exc
    if solved.errors:
        raise HTTPException(403, "state verdict could not be applied")


def prefix_verdict_dependency(verdict):
    """Build the app-wide dependency that judges routes under the prefix.

    Attached with `FastAPI(dependencies=[Depends(...)])`, it is part of the
    dependency tree of every route the app builds, so a route added under the
    prefix by any router - including one added after startup - is solved with
    a verdict in front of it. A route the state router owns already carries
    that router's guard, which judges it against its declaration; this one
    stands down for those and judges everything else under the prefix as a
    private read.

    "The state router owns it" means one of the guard objects
    `create_state_router` built is in this route's tree, compared with `is`.
    The version this replaces stood the verdict down for anything CLAIMING to
    have been judged, by carrying an attribute. Measured on `34f380aa`: that
    attribute on a route's endpoint, on its dependency, on a sub-dependency, on
    another router's dependency or on a callable class all served the notebook
    to an anonymous caller, and the coverage test called those routes covered.

    It cannot reach what FastAPI does not build a dependency tree for - a
    mounted sub-application, a bare Starlette route. `StatePrefixGate` is the
    layer for those, and the two are not interchangeable.
    """

    async def state_prefix_verdict(request: Request) -> None:
        if not is_state_prefix(request.url.path):
            return
        route = request.scope.get("route")
        if route is not None and prefix_verdict_stands_down_for(route):
            return
        await apply_verdict(verdict, request)

    setattr(state_prefix_verdict, STATE_VERDICT_MARK, True)
    setattr(state_prefix_verdict, STATE_PREFIX_VERDICT_MARK, True)
    _PREFIX_VERDICTS.add(state_prefix_verdict)
    return state_prefix_verdict


def _matched_route(routes, scope):
    """The route Starlette's own matcher would hand this request to."""
    partial_match = None
    for route in routes:
        try:
            match, _ = route.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            return route
        if match == Match.PARTIAL and partial_match is None:
            partial_match = route
    return partial_match


def _replaying(request, receive):
    cached = getattr(request, "_body", None)
    if cached is None:
        return receive
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": cached, "more_body": False}
        return await receive()

    return replay


class StatePrefixGate:
    """Refuse a request under the state prefix that carries no verdict.

    An app-wide `Depends(...)` reaches every route FastAPI BUILDS. It does not
    reach a sub-application installed with `app.mount()`, nor a bare Starlette
    `Route` appended to the router, because neither has a dependency tree for
    FastAPI to solve. This middleware resolves the request against the app's
    own routes with Starlette's matcher, asks the matched route whether it
    carries a state verdict, and applies one itself when it does not - before
    the request reaches that route, so a mounted write never runs.
    """

    def __init__(self, app, verdict, routes):
        self.app = app
        self.verdict = verdict
        self.routes = routes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not is_state_prefix(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        route = _matched_route(self.routes(), scope)
        if route is not None and route_carries_state_verdict(route):
            await self.app(scope, receive, send)
            return
        request = StarletteRequest(scope, receive)
        try:
            await apply_verdict(self.verdict, request)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send)
            return
        await self.app(scope, _replaying(request, receive), send)


def install_state_prefix_gate(app, verdict) -> None:
    """Add the MIDDLEWARE layer of the prefix verdict to `app`.

    The other layer is `prefix_verdict_dependency`, which the app has to be
    constructed with, because it has to be there when the routes are built.
    This one covers what that one cannot reach.
    """
    app.add_middleware(StatePrefixGate, verdict=verdict,
                       routes=lambda: route_contexts(app))


def declaration_agrees_with_source(declaration, endpoint) -> bool:
    """Whether the route's declaration survives an independent reading.

    The declaration says what the route serves; `api.state_route_reader` says
    what the endpoint's own source executes. They must agree in the SAME terms:
    a declared literal action must be among the literals the endpoint executes,
    a declared dispatch family must be read executing the very URL parameter
    the guard resolves, and the schema route must be read executing nothing.
    A reading of UNREADABLE agrees with nothing - a route whose source cannot
    be read is refused, not waved through.
    """
    kind, action = declaration
    reading = read_route(endpoint)
    if reading.kind == UNREADABLE:
        return False
    if kind == "schema":
        return reading.kind == NO_ACTION
    if action is None:
        return reading.kind == DISPATCH and reading.parameter == _DISPATCH_PARAMETER
    return reading.kind == LITERAL and action in reading.actions


def create_state_router(service_dependency, access_dependency, read_dependency=None):
    """Build the `/api/state` router.

    A route's authorization class is DERIVED, not remembered. Every route
    declares the action it serves (`_declare`), ONE guard is attached to the
    ROUTER, and the guard asks `core.state_commands.read_visibility` - the single
    table - whether that action is public.

    The shape this replaces answered 13 of its 20 GET routes to an anonymous
    visitor because nobody had attached a dependency to them: public by
    OMISSION. Here every route declares what it serves, ONE guard applies the
    verdict through `apply_verdict`, and the guard cross-checks each
    declaration against `api.state_route_reader` - an independent reading of
    the endpoint's own source. A declaration the reading does not support, and
    a route whose source cannot be read at all, are both refused.

    `read_dependency` is the identity verdict for a private read and MUST accept
    the Request - every authorization dependency in this repository does. It
    defaults to `access_dependency`, so an embedder that passes only the writer
    verdict gets that verdict on every private read: it can never open one by
    omission, and which reads are public stays the table's decision alone.
    """
    read_verdict = read_dependency or access_dependency
    router = APIRouter(prefix="/api/state", tags=["state-graph"])

    def _call(service, action, arguments, *, write=False):
        try:
            return execute(service, action, arguments, allow_write=write)
        except StateNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StateGraphError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def _guard_read_action(action, request: Request) -> None:
        """Refuse a NON-public read.

        `None` (the action could not be resolved) and every name the table does
        not list - including one added later - are private reads. The refusal
        happens when this dependency runs; FastAPI may have validated the
        request body first, so this guard claims nothing about when the body
        is parsed.
        """
        if action is None or not is_public_read(action):
            await apply_verdict(read_verdict, request)

    async def _guard_director_action(request: Request) -> None:
        """This path carries both classes. `send_director_message` and the two
        transitions CHANGE state, so they take the WRITE verdict and its wording;
        only `list_director_messages` is a read, and it is private."""
        if request.path_params.get(_DISPATCH_PARAMETER) == "list_director_messages":
            await _guard_read_action("list_director_messages", request)
        else:
            await apply_verdict(access_dependency, request)

    async def _router_guard(request: Request) -> None:
        """The one guard, on the ROUTER, under which every route it owns stands.

        A route mounted under the same prefix but on a DIFFERENT router is not
        covered here. `api.state_http.prefix_verdict_dependency`, attached
        app-wide, judges those; `api.state_http.StatePrefixGate` judges what
        even an app-wide dependency cannot reach.

        The guard reads the declaration off the matched endpoint at REQUEST
        time, which is what makes a reclassified action move every door at
        once: nothing is frozen at build time except the declaration of which
        action a route serves. An endpoint that declares nothing is refused -
        the default here is DENY, exactly as it is in the read table.

        The declaration is then cross-checked against an independent reader
        (`declaration_agrees_with_source`). A declaration the endpoint's own
        source does not support is refused, and so is a route whose source the
        reader cannot read: reading nothing is not permission to serve
        anything.
        """
        route = request.scope.get("route")
        endpoint = getattr(route, "endpoint", None)
        declaration = getattr(endpoint, _DECLARATION, None)
        if declaration is None:
            await apply_verdict(read_verdict, request)
            return
        if not declaration_agrees_with_source(declaration, endpoint):
            raise HTTPException(403, "route declaration does not match the action it serves")
        kind, action = declaration
        if kind not in DECLARATION_KINDS:
            raise HTTPException(403, "route declares a kind the guard does not judge")
        if kind == "write":
            await apply_verdict(access_dependency, request)
        elif kind == "director":
            await _guard_director_action(request)
        elif kind == "schema":
            await _guard_read_action(SCHEMA_DECLARED_ACTION, request)
        elif action is None:
            # Action-dispatch family: the action is in the URL, the reader has
            # confirmed the endpoint executes that very parameter, and a
            # request that names none is refused like any other unclassified
            # read.
            await _guard_read_action(request.path_params.get(_DISPATCH_PARAMETER), request)
        else:
            await _guard_read_action(action, request)

    setattr(_router_guard, STATE_ROUTER_GUARD_MARK, True)
    setattr(_router_guard, STATE_VERDICT_MARK, True)
    # THIS object is what the prefix verdict stands down for, registered where
    # it is created.
    _ROUTER_GUARDS.add(_router_guard)

    def _declare(endpoint, kind: str, action: str | None = None):
        """Record on the endpoint function WHAT it serves.

        FastAPI's decorator returns the function (the route object lives in the
        router), and `request.scope["route"].endpoint` IS that function, so the
        declaration is stored where the guard reads it. No dependency is
        attached here on purpose: the verdict comes from `_router_guard`, armed
        once for the whole router, so a route that forgets to call `_declare` is
        refused rather than left open.
        """
        setattr(endpoint, _DECLARATION, (kind, action))
        return endpoint

    router.dependencies.append(Depends(_router_guard))


    def schema():
        return describe()

    # Kind `schema`, not a read of an action: this endpoint executes NO state
    # action, and the independent reader must read it that way or the route is
    # refused. It is judged as a private read of SCHEMA_DECLARED_ACTION.
    _declare(router.get("/schema")(schema), "schema", SCHEMA_DECLARED_ACTION)

    async def director_message(action: str, request: Request,
                               service=Depends(service_dependency)):
        """Closed v2 REST adapter; authorization is the router guard."""
        from core.director_messaging_protocol import ACTIONS, DirectorMessageError
        if action not in ACTIONS:
            return DirectorMessageError("invalid_request").as_dict()
        try:
            arguments = await request.json()
        except Exception:
            return DirectorMessageError("invalid_request").as_dict()
        if not isinstance(arguments, dict):
            return DirectorMessageError("invalid_request").as_dict()
        return execute(service, action, arguments,
                       allow_write=action != "list_director_messages")

    _declare(router.post("/director-messages/{action}")(director_message), "director")

    def projects(repo_path: str | None = None, after: str = "", limit: int = 100, service=Depends(service_dependency)):
        return _call(service, "project_catalog", {"repo_path": repo_path, "after": after, "limit": limit})

    # `project_catalog`, not `list_projects`: the cross-checkable truth. The
    # handler executes `project_catalog`; declaring `list_projects` here used
    # to be a wrong declaration nobody could see, because nothing independent
    # read the declaration back against what the endpoint executes.
    _declare(router.get("/projects")(projects), "read", "project_catalog")

    def graph(project_id: str, service=Depends(service_dependency)):
        return _call(service, "get_graph", {"project_id": project_id})

    _declare(router.get("/projects/{project_id}")(graph), "read", "get_graph")

    def frontier(project_id: str, limit: int = 30, service=Depends(service_dependency)):
        return _call(service, "frontier", {"project_id": project_id, "limit": limit})

    _declare(router.get("/projects/{project_id}/frontier")(frontier), "read", "frontier")

    def driver_note(project_id: str, service=Depends(service_dependency)):
        return _call(service, "get_driver_note", {"project_id": project_id})

    _declare(router.get("/projects/{project_id}/driver-note")(driver_note),
             "read", "get_driver_note")

    def driver_note_history(project_id: str, after_revision: int = 0, limit: int = 100,
                            service=Depends(service_dependency)):
        return _call(service, "driver_note_history", {
            "project_id": project_id, "after_revision": after_revision, "limit": limit})

    _declare(router.get("/projects/{project_id}/driver-note/history")(driver_note_history),
             "read", "driver_note_history")

    def search_driver_note_history(
            project_id: str,
            query: Annotated[str, Query(
                max_length=500,
                description="Unicode case-insensitive literal text; empty lists filtered revisions.")] = "",
            section: Annotated[Literal["permanent", "temporary"] | None, Query(
                description="Exact changed-section filter.")] = None,
            actor: Annotated[str | None, Query(
                min_length=1, max_length=320, description="Exact authenticated actor filter.")] = None,
            director_identity: Annotated[str | None, Query(
                min_length=1, max_length=320, description="Exact recorded director identity filter.")] = None,
            after_revision: Annotated[int, Query(
                ge=0, le=2**63-1,
                description="Exclusive stable cursor; results are ordered by revision ascending.")] = 0,
            min_revision: Annotated[int | None, Query(
                ge=1, le=2**63-1, description="Inclusive minimum revision.")] = None,
            max_revision: Annotated[int | None, Query(
                ge=1, le=2**63-1, description="Inclusive maximum revision.")] = None,
            created_after: Annotated[str | None, Query(
                max_length=64,
                description="Exclusive timezone-aware ISO-8601 instant lower bound.",
                json_schema_extra={"format": "date-time"})] = None,
            created_before: Annotated[str | None, Query(
                max_length=64,
                description="Exclusive timezone-aware ISO-8601 instant upper bound.",
                json_schema_extra={"format": "date-time"})] = None,
            limit: Annotated[int, Query(
                ge=1, le=100, description="Maximum entries returned per page.")] = 20,
            excerpt_chars: Annotated[int, Query(
                ge=64, le=1000, description="Maximum characters in each redacted excerpt.")] = 320,
            service=Depends(service_dependency)):
        return _call(service, "search_driver_note_history", {
            "project_id": project_id, "query": query, "section": section,
            "actor": actor, "director_identity": director_identity,
            "after_revision": after_revision, "min_revision": min_revision,
            "max_revision": max_revision, "created_after": created_after,
            "created_before": created_before, "limit": limit,
            "excerpt_chars": excerpt_chars})

    _declare(router.get(
        "/projects/{project_id}/driver-note/history/search",
        summary="Search driver note history",
        description=("Returns bounded redacted excerpts from the section changed by each matching "
                     "revision, ordered by revision ascending. Continue stable pagination with "
                     "next_after_revision and the same filters."))(search_driver_note_history),
        "read", "search_driver_note_history")

    def issues(project_id: str, status: Annotated[list[str] | None, Query()] = None,
               kind: Annotated[list[str] | None, Query()] = None, node_key: str | None = None,
               after: int = 0, limit: int = 50, service=Depends(service_dependency)):
        return _call(service, "list_issues", {"project_id": project_id, "statuses": status, "kinds": kind,
                                              "node_key": node_key, "after": after, "limit": limit})

    _declare(router.get("/projects/{project_id}/issues")(issues), "read", "list_issues")

    def issue(project_id: str, issue_id: str, service=Depends(service_dependency)):
        return _call(service, "get_issue", {"project_id": project_id, "issue_id": issue_id})

    _declare(router.get("/projects/{project_id}/issues/{issue_id}")(issue), "read", "get_issue")

    def attempt(attempt_id: str, service=Depends(service_dependency)):
        return _call(service, "get_attempt", {"attempt_id": attempt_id})

    _declare(router.get("/attempts/{attempt_id}")(attempt), "read", "get_attempt")

    async def query(action: str, arguments: dict, service=Depends(service_dependency)):
        if action not in READ_REQUESTS:
            raise HTTPException(422, "unknown or mutating query")
        try:
            result = await to_thread.run_sync(partial(_call, service, action, arguments))
            return await result if inspect.isawaitable(result) else result
        except StateNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StateGraphError as exc:
            raise HTTPException(422, str(exc)) from exc

    _declare(router.post("/query/{action}")(query), "read", None)

    def command(action: str, arguments: dict, service=Depends(service_dependency)):
        if action not in WRITE_REQUESTS:
            raise HTTPException(422, "unknown state command")
        return _call(service, action, arguments, write=True)

    _declare(router.post("/commands/{action}")(command), "write")

    def overview(project_id: str, service=Depends(service_dependency)):
        return _call(service, "project_overview", {"project_id": project_id})

    _declare(router.get("/projects/{project_id}/overview")(overview), "read", "project_overview")

    def run_summary(project_id: str, service=Depends(service_dependency)):
        return _call(service, "project_run_summary", {"project_id": project_id})

    _declare(router.get("/projects/{project_id}/run-summary")(run_summary),
             "read", "project_run_summary")

    def node(project_id: str, node_key: str, service=Depends(service_dependency)):
        return _call(service, "get_node", {"project_id": project_id, "node_key": node_key})

    _declare(router.get("/projects/{project_id}/nodes/{node_key}")(node), "read", "get_node")

    def attempts(project_id: str, after: int = 0, limit: int = 30, service=Depends(service_dependency)):
        return _call(service, "project_attempts", {"project_id": project_id, "after": after, "limit": limit})

    _declare(router.get("/projects/{project_id}/attempts")(attempts), "read", "project_attempts")

    def references(project_id: str, node_key: str | None = None, after: str = "", limit: int = 100,
                   service=Depends(service_dependency)):
        return _call(service, "references", {"project_id": project_id, "node_key": node_key, "after": after, "limit": limit})

    _declare(router.get("/projects/{project_id}/references")(references), "read", "references")

    def attempt_detail(attempt_id: str, service=Depends(service_dependency)):
        return _call(service, "attempt_detail", {"attempt_id": attempt_id})

    _declare(router.get("/attempts/{attempt_id}/detail")(attempt_detail), "read", "attempt_detail")

    def run_owners(run_id: str, service=Depends(service_dependency)):
        return _call(service, "run_owners", {"run_id": run_id})

    _declare(router.get("/runs/{run_id}/owners")(run_owners), "read", "run_owners")

    return router
