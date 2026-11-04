"""Managed DSH drivers: existing State reader/writer authentication applies."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from api import authz
from api.dependencies import get_db_manager, get_skillflow
from api.state_graph_routers import get_service
from core.datadir import aitelier_home
from core.dsh_driver import DshDrivers
from core.state_commands import ProjectPrivate, execute
from core.state_graph import StateGraphError, StateNotFound
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
    provider: str = Field(default="aitelier", min_length=1, max_length=100)
    model: str = Field(default="driver", min_length=1, max_length=200)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=0)
    type: str = Field(pattern="^(user/message|assistant/message|tool/call|tool/result|compaction/end|agent/error)$")
    name: str | None = Field(default=None, max_length=200)
    timestamp: float | None = None
    text: str = Field(max_length=16000)
    truncated: bool = False


class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str = Field(pattern="^(running|waiting|failed)$")
    subagents: list[str] = Field(default_factory=list, max_length=1000)
    error: str = Field(default="", max_length=2000)
    event: HistoryEntry | None = None


@router.get("", dependencies=[Depends(authz.require_reader)])
def status(runtime=Depends(manager)):
    return runtime.status()


@router.get("/{project_id}/history", dependencies=[Depends(authz.require_reader)])
def history(project_id: str, before_seq: int = Query(default=9007199254740991, ge=0, le=9007199254740991),
            limit: int = Query(default=50, ge=1, le=100), runtime=Depends(manager)):
    try:
        return runtime.history(project_id, before_seq=before_seq, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/messages", dependencies=[Depends(authz.require_reader)])
def messages(project_id: str, limit: int = Query(default=50, ge=1, le=200),
             service=Depends(get_service)):
    """The durable messages this project's driver sent its user, newest first.

    It reads persisted State rows through the same authenticated reader verdict
    as the neighbouring driver routes: no run trace, no session log, and no
    sign that the sending driver is online - the sender may be stopped or gone.
    """
    try:
        return execute(service, "list_user_messages", {"project_id": project_id, "limit": limit})
    except ProjectPrivate as exc:
        raise HTTPException(403, str(exc)) from exc
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/unread", dependencies=[Depends(authz.require_reader)])
def unread(project_id: str | None = Query(default=None, max_length=200),
           service=Depends(get_service)):
    """The unread badge: how many driver messages the user has not seen yet.

    Counted from persisted rows against the persisted read marker
    (`mark_user_conversation_read`), so it survives a refresh and a different
    browser, and it never consults the sending driver's presence.
    """
    try:
        return execute(service, "user_conversation_unread", {"project_id": project_id})
    except ProjectPrivate as exc:
        raise HTTPException(403, str(exc)) from exc
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/conversation", dependencies=[Depends(authz.require_reader)])
def conversation(project_id: str, limit: int = Query(default=30, ge=1, le=200),
                 cursor: str | None = Query(default=None, max_length=1000),
                 service=Depends(get_service)):
    """ONE conversation: what the driver told the user and what the user told it.

    Both directions, one chronological order (newest first), each entry carrying
    its sender. `next_cursor` is a keyset position, so `?cursor=` walks older
    pages without duplicating or skipping a message while new ones arrive;
    `next_cursor: null` is the end of history. Same authenticated reader verdict
    as the neighbouring driver routes, and persisted rows only - nothing here
    consults the driver's presence or runtime, so an offline driver's messages
    still read.
    """
    try:
        return execute(service, "list_user_conversation",
                       {"project_id": project_id, "limit": limit, "cursor": cursor})
    except ProjectPrivate as exc:
        raise HTTPException(403, str(exc)) from exc
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


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
async def activity(project_id: str, body: Activity, request: Request, runtime=Depends(manager)):
    row = owned(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")
    import json
    runtime.update(project_id, phase=body.phase, subagents_json=json.dumps(body.subagents), last_error=body.error)
    from api.sse_manager import stream_manager
    await stream_manager.push_log("dsh:" + row["driver_id"], json.dumps({
        "type": "driver_activity", "phase": body.phase, "subagents": body.subagents,
        "entry": body.event.model_dump() if body.event else None}))
    return {"ok": True}


@router.post("/{project_id}/heartbeat", dependencies=[Depends(authz.require_writer)])
def heartbeat(project_id: str, request: Request, runtime=Depends(manager), service=Depends(get_service)):
    row = owned(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
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


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)
    messages: list[dict]
    tools: list[dict] | None = None
    max_tokens: int = Field(default=32768, ge=1, le=262144)
    reasoning_effort: str = Field(default="high", pattern="^(off|low|high|max)$")


class ProjectWork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    attempt_id: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)
    action: str = Field(pattern="^(open|read|write|edit|bash|deliver|push)$")
    arguments: dict = Field(default_factory=dict)


@router.post("/{project_id}/work", dependencies=[Depends(authz.require_writer)])
async def project_work(project_id: str, body: ProjectWork, request: Request,
                       runtime=Depends(manager), service=Depends(get_service), sf=Depends(get_skillflow)):
    """Root and native children share the same authenticated coding capability."""
    import asyncio
    import json
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool
    from core.dsh_work import project_work as execute_work
    row = owned(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")

    async def stream():
        # Flush headers and keep long isolated tests alive through Node fetch;
        # whitespace is ordinary JSON, not an agent event or another model turn.
        task = asyncio.create_task(run_in_threadpool(execute_work, runtime.db, service,
            project_id=project_id, driver_id=row["driver_id"], session_id=body.session_id,
            attempt_id=body.attempt_id, fence=body.fence, action=body.action,
            arguments=body.arguments, sf=sf))
        yield " "
        while not task.done():
            await asyncio.wait({task}, timeout=15)
            if not task.done():
                yield " "
        try:
            yield json.dumps(task.result())
        except Exception as exc:
            yield json.dumps({"error": str(exc), "status": "unavailable"})
    return StreamingResponse(stream(), media_type="application/json")


@router.post("/{project_id}/completion", dependencies=[Depends(authz.require_writer)])
async def completion(project_id: str, body: Completion, request: Request, runtime=Depends(manager)):
    """The managed driver uses the same router, credentials and failover as pipelines."""
    import asyncio
    from core.ai_router import AIGateway
    from starlette.concurrency import run_in_threadpool
    row = owned(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")
    sessions = runtime.gateways.setdefault(project_id, {})
    if body.session_id not in sessions:
        # Native children (including resumed old children) have their own session.
        # Bind once per child: rotate across workers, keep tool turns sticky.
        model = row["model"] if body.session_id == row["session_id"] else "worker"
        gateway = AIGateway(model, enable_thinking=True, thinking_effort=body.reasoning_effort,
                            max_output_tokens=body.max_tokens)
        sessions[body.session_id] = (gateway, asyncio.Lock())
    gateway, lock = sessions[body.session_id]
    async with lock:
        gateway.max_output_tokens = body.max_tokens
        gateway.thinking_effort = body.reasoning_effort
        gateway.enable_thinking = body.reasoning_effort != "off"
        turn = await run_in_threadpool(gateway.generate_native, body.messages, tools=body.tools)
        return {"text": turn.text, "tool_calls": turn.tool_calls, "reasoning_content": turn.reasoning_content,
                "truncated": turn.truncated, "usage": gateway.last_usage, "served_by": gateway.active_model}
