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


# Each route declares WHAT it serves, on its own endpoint object: a tuple
# `(kind, action)` where `kind` is `read`, `write` or `director` and `action` is
# the read action (or None for the action-dispatch families, whose action lives
# in the URL). This is the enumerable declaration the router guard reads, and
# the thing the invariant tests walk instead of a hardcoded door list.
_DECLARATION = "_state_route"

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

    def _verdict(dependency, request):
        """Apply an authorization dependency to a request.

        `Depends`-shaped dependencies take the Request; a bare `lambda: None`
        (an embedder running with the gate off, which tests and
        `api/state_only` both use) takes nothing. Both shapes appear in this
        repository, so the call is adapted rather than assumed - the verdict is
        applied either way, and no path is left unchecked by a TypeError.

        An override is honored. The guard stands in for the declared dependency,
        so `app.dependency_overrides[require_writer]` (which this repository's
        tests and embeds use to swap a verdict) must keep working; without this
        a swapped verdict would be silently ignored here and nowhere else.
        """
        overrides = getattr(getattr(request, "app", None), "dependency_overrides", None)
        if overrides:
            replacement = overrides.get(dependency)
            if replacement is not None:
                dependency = replacement
        try:
            accepts = bool(inspect.signature(dependency).parameters)
        except (TypeError, ValueError):
            accepts = True
        return dependency(request) if accepts else dependency()

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

    def _guard_read_action(action, request: Request) -> None:
        """Refuse a NON-public read before the request body is parsed.

        `None` (the action could not be resolved) and every name the table does
        not list - including one added later - are private reads.
        """
        if action is None or not is_public_read(action):
            _verdict(read_verdict, request)

    def _guard_director_action(request: Request) -> None:
        """This path carries both classes. `send_director_message` and the two
        transitions CHANGE state, so they take the WRITE verdict and its wording;
        only `list_director_messages` is a read, and it is private."""
        if request.path_params.get("action") == "list_director_messages":
            _guard_read_action("list_director_messages", request)
        else:
            _verdict(access_dependency, request)

    def _router_guard(request: Request) -> None:
        """The one guard, on the ROUTER, so no route can be added without it.

        It reads the declaration off the matched endpoint at REQUEST time, which
        is what makes a reclassified action move every door at once: nothing is
        frozen at build time except the declaration of which action a route
        serves. An endpoint that declares nothing is refused - the default here
        is DENY, exactly as it is in the read table.
        """
        route = request.scope.get("route")
        declaration = getattr(getattr(route, "endpoint", None), _DECLARATION, None)
        if declaration is None:
            _verdict(read_verdict, request)
            return
        kind, action = declaration
        if kind == "write":
            _verdict(access_dependency, request)
        elif kind == "director":
            _guard_director_action(request)
        elif action is None:
            # Action-dispatch family: the action is in the URL, and a request
            # that names none is refused like any other unclassified read.
            _guard_read_action(request.path_params.get("action"), request)
        else:
            _guard_read_action(action, request)

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

    _declare(router.get("/projects")(projects), "read", "list_projects")

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
