"""Authorized State DAG command/query endpoints; all project facts are private."""
from fastapi import APIRouter, Depends, HTTPException, Request

from api.authz import require_writer
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core import cf_access
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, describe, execute
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_service import StateService

router = APIRouter(prefix="/api/state", tags=["state-graph"], dependencies=[Depends(require_writer)])


def authenticated_actor(request) -> str:
    """Called only after transport authorization; never trusts an actor argument."""
    if request is not None:
        email = cf_access.email_from_request_headers(request.headers, getattr(request, "cookies", {}))
        if email:
            return email
    return "authorized-state-operator"


def get_service(request: Request, db=Depends(get_db_manager), ws=Depends(get_workspace_manager)):
    from api.mcp_router import _start_driver
    return StateService(db, ws, attach_driver=_start_driver, actor=authenticated_actor(request),
                        runtime_factory=lambda: (get_skillflow(), get_config_registry()))


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
def projects(service=Depends(get_service)):
    return {"projects": service.store.list_projects()}


@router.get("/projects/{project_id}")
def graph(project_id: str, service=Depends(get_service)):
    return _call(service, "get_graph", {"project_id": project_id})


@router.get("/projects/{project_id}/frontier")
def frontier(project_id: str, limit: int = 30, service=Depends(get_service)):
    return _call(service, "frontier", {"project_id": project_id, "limit": limit})


@router.get("/attempts/{attempt_id}")
def attempt(attempt_id: str, service=Depends(get_service)):
    return _call(service, "get_attempt", {"attempt_id": attempt_id})


@router.post("/query/{action}")
def query(action: str, arguments: dict, service=Depends(get_service)):
    if action not in READ_REQUESTS:
        raise HTTPException(422, "unknown or mutating query")
    return _call(service, action, arguments)


@router.post("/commands/{action}")
def command(action: str, arguments: dict, service=Depends(get_service)):
    if action not in WRITE_REQUESTS:
        raise HTTPException(422, "unknown state command")
    return _call(service, action, arguments, write=True)
