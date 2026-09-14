"""State-only HTTP routes reusable without importing the workflow host."""
import inspect
from functools import partial
from typing import Annotated, Literal
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, describe, execute
from core.state_graph import StateConflict, StateGraphError, StateNotFound


def create_state_router(service_dependency, access_dependency):
    router = APIRouter(prefix="/api/state", tags=["state-graph"], dependencies=[Depends(access_dependency)])
    def _call(service, action, arguments, *, write=False):
        try:
            return execute(service, action, arguments, allow_write=write)
        except StateNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StateGraphError as exc:
            raise HTTPException(422, str(exc)) from exc


    @router.get("/schema")
    def schema():
        return describe()


    @router.post("/director-messages/{action}")
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


    @router.get("/projects")
    def projects(repo_path: str | None = None, after: str = "", limit: int = 100, service=Depends(service_dependency)):
        return _call(service, "project_catalog", {"repo_path": repo_path, "after": after, "limit": limit})


    @router.get("/projects/{project_id}")
    def graph(project_id: str, service=Depends(service_dependency)):
        return _call(service, "get_graph", {"project_id": project_id})


    @router.get("/projects/{project_id}/frontier")
    def frontier(project_id: str, limit: int = 30, service=Depends(service_dependency)):
        return _call(service, "frontier", {"project_id": project_id, "limit": limit})


    @router.get("/projects/{project_id}/driver-note")
    def driver_note(project_id: str, service=Depends(service_dependency)):
        return _call(service, "get_driver_note", {"project_id": project_id})


    @router.get("/projects/{project_id}/driver-note/history")
    def driver_note_history(project_id: str, after_revision: int = 0, limit: int = 100,
                            service=Depends(service_dependency)):
        return _call(service, "driver_note_history", {
            "project_id": project_id, "after_revision": after_revision, "limit": limit})


    @router.get(
        "/projects/{project_id}/driver-note/history/search",
        summary="Search driver note history",
        description=("Returns bounded redacted excerpts from the section changed by each matching "
                     "revision, ordered by revision ascending. Continue stable pagination with "
                     "next_after_revision and the same filters."))
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


    @router.get("/attempts/{attempt_id}")
    def attempt(attempt_id: str, service=Depends(service_dependency)):
        return _call(service, "get_attempt", {"attempt_id": attempt_id})


    @router.post("/query/{action}")
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


    @router.post("/commands/{action}")
    def command(action: str, arguments: dict, service=Depends(service_dependency)):
        if action not in WRITE_REQUESTS:
            raise HTTPException(422, "unknown state command")
        return _call(service, action, arguments, write=True)


    @router.get("/projects/{project_id}/overview")
    def overview(project_id: str, service=Depends(service_dependency)):
        return _call(service, "project_overview", {"project_id": project_id})


    @router.get("/projects/{project_id}/run-summary")
    def run_summary(project_id: str, service=Depends(service_dependency)):
        return _call(service, "project_run_summary", {"project_id": project_id})


    @router.get("/projects/{project_id}/nodes/{node_key}")
    def node(project_id: str, node_key: str, service=Depends(service_dependency)):
        return _call(service, "get_node", {"project_id": project_id, "node_key": node_key})


    @router.get("/projects/{project_id}/attempts")
    def attempts(project_id: str, after: int = 0, limit: int = 30, service=Depends(service_dependency)):
        return _call(service, "project_attempts", {"project_id": project_id, "after": after, "limit": limit})


    @router.get("/projects/{project_id}/references")
    def references(project_id: str, node_key: str | None = None, after: str = "", limit: int = 100,
                   service=Depends(service_dependency)):
        return _call(service, "references", {"project_id": project_id, "node_key": node_key, "after": after, "limit": limit})


    @router.get("/attempts/{attempt_id}/detail")
    def attempt_detail(attempt_id: str, service=Depends(service_dependency)):
        return _call(service, "attempt_detail", {"attempt_id": attempt_id})


    @router.get("/runs/{run_id}/owners")
    def run_owners(run_id: str, service=Depends(service_dependency)):
        return _call(service, "run_owners", {"run_id": run_id})

    return router
