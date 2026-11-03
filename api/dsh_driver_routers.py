"""Managed DSH drivers: existing State reader/writer authentication applies."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api import authz
from api.dependencies import get_db_manager
from api.state_graph_routers import get_service
from core.datadir import aitelier_home
from core.dsh_driver import DshDrivers
from core.state_commands import execute
from core.state_driver_guide import STATE_DRIVER_GUIDE_INDEX

router = APIRouter(prefix="/api/dsh-drivers", tags=["DSH drivers"])


def manager(request: Request) -> DshDrivers:
    value = getattr(request.app.state, "dsh_drivers", None)
    if value is None:
        value = DshDrivers(get_db_manager(), authz.driver_registry(), aitelier_home() / "dsh-drivers")
        request.app.state.dsh_drivers = value
    return value


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(default="deepseek-official", min_length=1, max_length=100)
    model: str = Field(default="deepseek-v4.1-flash", min_length=1, max_length=200)


class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str = Field(pattern="^(running|waiting|failed)$")
    subagents: list[str] = Field(default_factory=list, max_length=1000)
    error: str = Field(default="", max_length=2000)


@router.get("", dependencies=[Depends(authz.require_reader)])
def status(runtime=Depends(manager)):
    return runtime.status()


@router.post("/{project_id}/start", dependencies=[Depends(authz.require_writer)])
async def start(project_id: str, body: Start, request: Request, runtime=Depends(manager)):
    try:
        return await runtime.start(project_id, provider=body.provider, model=body.model,
                                   actor=authz.request_actor(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{project_id}/stop", dependencies=[Depends(authz.require_writer)])
async def stop(project_id: str, runtime=Depends(manager)):
    try:
        return await runtime.stop(project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def owned(project_id, request, runtime):
    identity = authz.request_identity(request)
    row = runtime.get(project_id)
    if row is None or identity is None or identity.driver_id != row["driver_id"]:
        raise HTTPException(403, "this runtime belongs to another driver")
    return row


@router.get("/{project_id}/context", dependencies=[Depends(authz.require_reader)])
def context(project_id: str, request: Request, runtime=Depends(manager), service=Depends(get_service)):
    row = owned(project_id, request, runtime)
    graph = execute(service, "project_overview", {"project_id": project_id})
    project_note = execute(service, "get_driver_note", {"project_id": project_id})
    private_note = execute(service, "get_driver_note", {"driver_id": row["driver_id"]})
    return {"project_id": project_id, "driver_id": row["driver_id"], "event_seq": graph["event_seq"],
            "nodes": [{"name": n["node_key"], "status": n["status"]} for n in graph["nodes"]],
            "project_note": project_note, "private_note": private_note,
            "driver_skill": STATE_DRIVER_GUIDE_INDEX,
            "runtime_skill": (Path(__file__).resolve().parents[1] / "integrations" / "dsh-driver" / "driver.md").read_text()}


@router.post("/{project_id}/activity", dependencies=[Depends(authz.require_writer)])
def activity(project_id: str, body: Activity, request: Request, runtime=Depends(manager)):
    row = owned(project_id, request, runtime)
    if not row["enabled"] or project_id not in runtime.processes:
        raise HTTPException(409, "driver is stopped")
    import json
    runtime.update(project_id, phase=body.phase, subagents_json=json.dumps(body.subagents), last_error=body.error)
    return {"ok": True}


@router.post("/{project_id}/heartbeat", dependencies=[Depends(authz.require_writer)])
def heartbeat(project_id: str, request: Request, runtime=Depends(manager), service=Depends(get_service)):
    row = owned(project_id, request, runtime)
    if not row["enabled"] or project_id not in runtime.processes:
        raise HTTPException(409, "driver is stopped")
    claims = execute(service, "list_claims", {"project_id": project_id, "driver_id": row["driver_id"], "limit": 100})["claims"]
    with service.db.get_connection() as conn:
        attempts = [dict(r) for r in conn.execute("SELECT attempt_id,owner_fence AS fence FROM state_attempts "
            "WHERE project_id=? AND owner_driver_id=? AND status IN ('reserved','launching','running','paused','unknown') LIMIT 100",
            (project_id, row["driver_id"]))]
    items = [("claims", {"claim_id": c["claim_id"], "fence": c["fence"]}) for c in claims]
    items += [("attempts", a) for a in attempts]
    results = []
    for offset in range(0, len(items), 100):
        batch = items[offset:offset + 100]
        results.append(execute(service, "heartbeat", {"project_id": project_id,
            "claims": [v for k, v in batch if k == "claims"],
            "attempts": [v for k, v in batch if k == "attempts"]}, allow_write=True))
    return {"results": results}
