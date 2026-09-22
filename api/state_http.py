"""State-only HTTP routes reusable without importing the workflow host."""
import inspect
from functools import partial
from typing import Annotated, Literal
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from core.state_commands import (READ_REQUESTS, WRITE_REQUESTS, describe, execute,
                                                                  is_public_read, ProjectPrivate)
from starlette.responses import JSONResponse
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from contextlib import AsyncExitStack
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from api.state_verdict import DECLARATION_ATTR, binding_for, record_judged


# Each route declares WHAT it serves, on its own endpoint object: a tuple
# `(kind, action)` where `kind` is `read`, `write` or `director` and `action` is
# the read action (or None for the action-dispatch families, whose action lives
# in the URL). This is the enumerable declaration the router guard reads, and
# the thing the invariant tests walk instead of a hardcoded door list.
_DECLARATION = DECLARATION_ATTR

# The `/schema` REST route publishes the shape of every operation, closed
# families included. The owner has not ruled on opening it, so it stays shut -
# written down as a NAME so it is a decision somebody made, not a route somebody
# forgot.
SCHEMA_DECLARED_ACTION = "get_state_schema"


def apply_project_privacy(app):
    """Map a project-privacy refusal raised at the `execute` chokepoint to a 403 on
    EVERY route of `app` — including one a route author forged by reusing the real
    guard and declaring a public action. The forged handler obtains a private body
    only by calling `execute`, which raises; this handler turns that raise into a
    403 whose text is a generic 'not available', never the body. Honest routes get
    the same 403 from `_call`. Registered on the product app (api/main) and on the
    second assembly point (api/state_only)."""

    @app.exception_handler(ProjectPrivate)
    async def _project_private(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)


def create_state_router(service_dependency, access_dependency, read_dependency=None):
    """Build the `/api/state` router.

    A route's authorization class is DERIVED, not remembered. Every route
    declares the action it serves (`_declare`), ONE guard is attached to the
    ROUTER, and the guard asks `core.state_commands.read_visibility` - the single
    table - whether that action is public.

    The shape this replaces answered 13 of its 20 GET routes to an anonymous
    visitor because nobody had attached a dependency to them: public by
    OMISSION. Here there is nothing to attach and nothing to forget. A route
    with no declaration, a route reading an action nobody classified, and a read
    action added later all fall to the same verdict - private - because
    `read_visibility` fails CLOSED and so does the guard.

    `read_dependency` is the identity verdict for a private read and MUST accept
    the Request - every authorization dependency in this repository does. It
    defaults to `access_dependency`, so an embedder that passes only the writer
    verdict gets that verdict on every private read: it can never open one by
    omission, and which reads are public stays the table's decision alone.
    """
    read_verdict = read_dependency or access_dependency
    router = APIRouter(prefix="/api/state", tags=["state-graph"])

    async def _apply_verdict(request, dependency, ran: list) -> AsyncExitStack:
        """Apply an authorization dependency to a request BY RUNNING IT THROUGH
        FASTAPI'S OWN DEPENDENCY MACHINERY, not by calling it here.

        The guard used to do `dependency(request)` itself. That silently broke for
        every dependency shape that is not a plain synchronous function - an
        `async def` coroutine, a `yield` (or `async yield`) dependency, a callable
        object or a class - because calling such a D returns a coroutine or
        generator whose body never executes, so a refusal written inside the
        dependency never ran and the request leaked. The remedy is NOT to branch
        on `isawaitable`/`isgenerator` of the return value either: that is just
        another hand-maintained shape list, and the third one bought for the same
        exemption. Instead FastAPI resolves D exactly as it would for a route's
        `Depends(D)`, so release and refusal match a normal route for EVERY shape,
        present or future, with no shape table to keep current.

        An override is honored: the guard stands in for the declared dependency,
        so `dependency_overrides[require_writer]` (used by this repository's tests
        and embeds to swap a verdict) keeps working - a swapped verdict would
        otherwise be silently ignored here and nowhere else.

        `ran` is the guard's ledger of "a dependency actually EXECUTED": it is
        appended the moment `solve_dependencies` is entered, so a refusal raised
        inside the dependency still counts as a ruling, while a branch that never
        reaches this function (a declaration the reader refuses on its own)
        records NO judgment and the route reports uncovered.
        """
        overrides = getattr(getattr(request, "app", None), "dependency_overrides", None) or {}
        dependency = overrides.get(dependency, dependency)

        def _parent(v=Depends(dependency)):
            return v

        # D becomes a real SUB-dependency of a trivial parent: that is the only
        # way FastAPI executes it (solve_dependencies runs a dependant's
        # sub-dependencies, not its own `call`).
        dependant = get_dependant(path=request.url.path, call=_parent, scope="function")
        ran.append(True)
        # The stack is created here and returned OPEN: the guard is a yield
        # dependency, so it closes this AFTER the handler has run — the same
        # order in which a plain `Depends(D)` route closes D's teardown.
        stack = AsyncExitStack()
        solved = await solve_dependencies(
                request=request, dependant=dependant, body=None,
                background_tasks=None, response=None,
                dependency_overrides_provider=request.app,
                dependency_cache={}, async_exit_stack=stack,
                embed_body_fields=False)
        # A released verdict leaves no refusal. A refused one either propagated as
        # an exception above (handled exactly like a route dependency) or is
        # recorded on the solved result; reproduce it so the guard denies exactly
        # as a real `Depends(D)` route would.
        errors = getattr(solved, "errors", None)
        if errors:
            raise errors[0] if isinstance(errors, (list, tuple)) else errors
        response = getattr(solved, "response", None)
        status = getattr(solved, "status_code", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if status is not None and 400 <= int(status) < 600:
            headers = getattr(solved, "headers", None)
            if headers is None and response is not None:
                headers = getattr(response, "headers", None)
            raise HTTPException(status_code=int(status),
                                headers=dict(headers) if headers else None)
        return stack

    def _call(service, action, arguments, *, write=False):
        try:
            return execute(service, action, arguments, allow_write=write)
        except ProjectPrivate as exc:
            # An anonymous caller reached a project nobody opened (or one that
            # does not exist) via `execute` — the guard, the action table and the
            # route declaration are all irrelevant to this refusal, which is why
            # a route author cannot bypass it by forging a declaration.
            raise HTTPException(403, str(exc)) from exc
        except StateNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StateGraphError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def _guard_read_action(action, route, request: Request, ran: list, add_stack):
        """Refuse a NON-public read before the request body is parsed, and refuse -
        unconditionally - a PUBLIC declaration its own handler source contradicts.

        `None` (the action could not be resolved) and every name the table does
        not list - including one added later - are private reads. A declaration
        that IS public is then checked against the handler's source: a route that
        declares a public read and DELIVERS a private one is refused here, before
        any early approval.

        The mismatch refusal does NOT route through `read_verdict`. An embedder may
        pass a no-op verdict (api/state_only does, because its bearer middleware
        already authenticated every request), and routing the mismatch through that
        no-op would let a forged declaration answer 200 there. A declaration that
        contradicts its handler is not a visibility question, so it is answered by
        the guard itself, on every app that builds this router.

        Returns `public-clearance` when the reader approved a public declaration -
        a decision made WITHOUT running any authorization dependency, which the
        coverage metric therefore reports as cleared, not judged.
        """
        if action is None or not is_public_read(action):
            add_stack(await _apply_verdict(request, read_verdict, ran))
            return None
        binding = binding_for(route.endpoint, action, route.path)
        if not binding.ok:
            raise HTTPException(403, "state route declaration does not match its handler")
        return "public-clearance"

    async def _guard_director_action(route, request: Request, ran: list, add_stack) -> None:
        """This path carries both classes. `send_director_message` and the two
        transitions CHANGE state, so they take the WRITE verdict and its wording;
        only `list_director_messages` is a read, and it is private."""
        if request.path_params.get("action") == "list_director_messages":
            await _guard_read_action("list_director_messages", route, request, ran, add_stack)
        else:
            add_stack(await _apply_verdict(request, access_dependency, ran))

    async def _router_guard(request: Request):
        """The one guard, on the ROUTER, so no route can be added without it.

        It reads the declaration off the matched endpoint at REQUEST time, which
        is what makes a reclassified action move every door at once: nothing is
        frozen at build time except the declaration of which action a route
        serves. An endpoint that declares nothing is refused - the default here
        is DENY, exactly as it is in the read table.

        There is no stand-down: no attribute, dependency identity or path shape
        lets a route decline this judgment. Every branch resolves to a verdict
        applied through FastAPI's own machinery and then RECORDED, so coverage
        measures the ruling, not the arrival. The verdict is idempotent - a pure
        function of the request's credential - so judging a request twice yields
        the status and body of judging it once.

        The guard is a YIELD dependency on purpose. FastAPI runs the second half
        of a yield dependency AFTER the handler, exactly as it closes the
        teardown of a plain `Depends(D)` route, so the verdict stack opened here
        closes at the same point a real route's would - a verdict dependency
        with a post-`yield` half observes setup -> handler -> teardown, the same
        order `Depends(D)` gives it. Closing the stack inside the guard (the
        earlier shape) ran teardown BEFORE the handler, order-reversed.
        """
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        app = request.app
        declaration = getattr(getattr(route, "endpoint", None), DECLARATION_ATTR, None)
        ruling = "private-verdict"
        # A ruling is RECORDED only when a decision was actually made: either an
        # authorization dependency executed (append to `ran`), or the reader
        # approved a public declaration (public-clearance). Arriving here records
        # nothing - the arrival-count was the metric that hid the leak.
        ran: list = []
        stacks: list = []
        decision = None
        try:
            if declaration is None:
                stacks.append(await _apply_verdict(request, read_verdict, ran))
            else:
                kind, action = declaration
                if kind == "write":
                    ruling = "write-verdict"
                    stacks.append(await _apply_verdict(request, access_dependency, ran))
                elif kind == "director":
                    ruling = "director-verdict"
                    await _guard_director_action(route, request, ran, stacks.append)
                elif action is None:
                    # Action-dispatch family: the action lives in the URL and is
                    # judged here; the declaration binds only because the handler
                    # hands `execute` the very path parameter this guard judged AND
                    # returns no private body (checked inside `binding_for`).
                    ruling = "dispatch-verdict"
                    decision = await _guard_read_action(
                        request.path_params.get("action"), route, request, ran,
                        stacks.append)
                else:
                    ruling = "read-verdict"
                    decision = await _guard_read_action(action, route, request, ran,
                                                        stacks.append)
        except BaseException:
            for stack in stacks:
                await stack.aclose()
            if ran:
                record_judged(app, path, ruling, dep_backed=True)
            else:
                record_judged(app, path, "declaration-refused", dep_backed=False)
            raise
        if ran:
            # A dependency executed: the branch ruling is backed by a real
            # authorization decision, approval or refusal.
            record_judged(app, path, ruling, dep_backed=True)
        elif decision:
            # The reader approved a public declaration: a decision, but one no
            # dependency made. Recorded as public-clearance, dep_backed False -
            # coverage reports it as cleared, never as judged.
            record_judged(app, path, decision, dep_backed=False)
        yield
        for stack in stacks:
            await stack.aclose()

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

    _declare(router.get("/schema")(schema), "read", SCHEMA_DECLARED_ACTION)

    async def director_message(action: str, request: Request,
                               service=Depends(service_dependency)):
        """Closed v2 REST adapter; router authorization runs before body parsing."""
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

    # The declaration names the action this handler SERVES (`project_catalog`, the
    # paged catalog), not a sibling public read it resembles: `list_projects` is a
    # different action with different arguments, and a declaration that named it
    # would not bind the action this route delivers.
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

    def open_project(project_id: str, service=Depends(service_dependency)):
        return _call(service, "open_project", {"project_id": project_id}, write=True)

    _declare(router.post("/projects/{project_id}/open")(open_project), "write")

    def close_project(project_id: str, service=Depends(service_dependency)):
        return _call(service, "close_project", {"project_id": project_id}, write=True)

    _declare(router.post("/projects/{project_id}/close")(close_project), "write")

    return router
