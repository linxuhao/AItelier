"""State-only HTTP routes reusable without importing the workflow host."""
import inspect
from functools import partial
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
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


    @router.get("/projects")
    def projects(repo_path: str | None = None, after: str = "", limit: int = 100, service=Depends(service_dependency)):
        return _call(service, "project_catalog", {"repo_path": repo_path, "after": after, "limit": limit})


    @router.get("/projects/{project_id}")
    def graph(project_id: str, service=Depends(service_dependency)):
        return _call(service, "get_graph", {"project_id": project_id})


    @router.get("/projects/{project_id}/frontier")
    def frontier(project_id: str, limit: int = 30, service=Depends(service_dependency)):
        return _call(service, "frontier", {"project_id": project_id, "limit": limit})


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
