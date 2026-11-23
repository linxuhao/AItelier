"""Managed DSH drivers: existing State reader/writer authentication applies."""
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from api import authz
from api.dependencies import get_db_manager, get_skillflow
from api.state_graph_routers import get_service
from core import ai_router, media_library
from core import state_conversation_uploads as uploads
from core.datadir import aitelier_home
from core.dsh_driver import DshDrivers, completion_route, input_measured, route_input, route_modalities
from core.state_commands import ProjectPrivate, execute
from core.state_graph import CLOSED_STATUSES, StateGraphError, StateNotFound, StateWriteBusy
from core.state_driver_guide import STATE_DRIVER_GUIDE_INDEX

router = APIRouter(prefix="/api/dsh-drivers", tags=["DSH drivers"])

# How often `POST /{project_id}/completion` writes one whitespace byte while the
# model answer is outstanding, so a slow turn survives the client's transport
# timeouts (undici's `headersTimeout` and `bodyTimeout` are both 300 s by
# default; a single keepalive would only cover the first). Same 15 s the
# sibling `/work` endpoint uses. A module constant, not an inline literal, so a
# witness can shorten it instead of waiting it out.
_DSH_COMPLETION_KEEPALIVE_SEC = 15.0


def _runtime_skill() -> str:
    """The director's runtime instructions as the driver gets them: the generic
    director.md (dsh-director) followed by AItelier's state.md (plugin step 4)."""
    root = Path(__file__).resolve().parents[1] / "integrations"
    return (root / "dsh-director" / "director.md").read_text() + "\n" + (root / "dsh-driver" / "state.md").read_text()



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
    phase: str = Field(pattern="^(running|waiting|dormant|failed)$")
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

    Each entry's `segments` carry their stable `id`, which a reply cites. A
    message carries `answers` (its cited questions) only when it cites any; a
    row written before answers existed has no `answers` key, never an empty one.
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


UPLOAD_HEADER = "X-AItelier-Upload"


@router.post("/{project_id}/conversation/uploads", dependencies=[Depends(authz.require_writer)])
async def conversation_upload(project_id: str, request: Request,
                              name: str = Query(min_length=1, max_length=255),
                              service=Depends(get_service)):
    """Step 24.2b (design-steward 7.8.12a "上传"): one file for the conversation, as the raw
    request body (no multipart), staged until a message carries it and the hosts store it.

    Who: the write gate and `require_writer` as for every write (a steward token is refused
    by the gate: this is not one of its runtime endpoints), then the project-user check
    `send_user_conversation_message` makes (`user_identity_required` for a driver token).
    Cross-site: the custom `X-AItelier-Upload: 1` header forces a browser preflight, which
    fails (no CORS middleware), and a present `Sec-Fetch-Site` other than `same-origin` is 403.
    The kind and mime come from the first bytes (415 `type`), never from `Content-Type` or
    the name; the kind's cap is enforced while the body arrives (413 `too_large`, nothing
    left in staging); 20 uploads per project per hour counted from the table (429
    `rate_limited`, `Retry-After`); staging total (507 `staging_full`). Answers
    {upload_id, kind, mime, size, name}; the file is `<staging>/<upload_id>`, 0600."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        raise HTTPException(403, "cross_site")
    if request.headers.get(UPLOAD_HEADER) != "1":
        raise HTTPException(400, "upload_header_required")
    if not uploads.valid_name(name):
        raise HTTPException(422, "invalid_name")
    if not service.driver_inbox._project_user():
        raise HTTPException(403, "user_identity_required")
    directory = media_library.staging_dir()
    upload_id = uploads.new_upload_id()
    try:
        base = await asyncio.to_thread(uploads.admit, service.store, project_id, directory)
        entry, size, partial = await uploads.receive(request.stream(), directory, upload_id, base)
        return await asyncio.to_thread(uploads.commit, service.store, project_id=project_id,
                                       user=service.actor, upload_id=upload_id, entry=entry,
                                       size=size, name=name, partial=partial, directory=directory)
    except uploads.UploadRefused as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(exc.status, exc.code, headers=headers) from exc
    except ProjectPrivate as exc:
        raise HTTPException(403, str(exc)) from exc
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{project_id}/uploads/{upload_id}", dependencies=[Depends(authz.require_reader)])
async def staged_upload(project_id: str, upload_id: str, service=Depends(get_service)):
    """Step 24.2b (design-steward 7.8.12a "宿主入库"): a host takes the staged bytes of one
    dashboard upload with its driver token, streamed, before storing them in its own library
    and acknowledging (`ack_conversation_upload`).

    Only a driver that is a delivery target of the message carrying this project's upload gets
    the bytes; anyone else - and an unknown id, another project's upload or one no message
    carries - gets the same 404. A target whose file was already removed (every target
    acknowledged, or nobody came for 7 days) gets 410 `gone`."""
    from fastapi.responses import StreamingResponse
    try:
        handle, mime, size = await asyncio.to_thread(
            uploads.open_for_target, service.store, project_id, upload_id,
            getattr(service, "driver_id", None), media_library.staging_dir())
    except LookupError as exc:
        raise HTTPException(404, uploads.NOT_FOUND) from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, "gone") from exc

    def chunks():
        with handle:
            while data := handle.read(1024 * 1024):
                yield data

    return StreamingResponse(chunks(), media_type=mime, headers={
        "Content-Length": str(size), "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/{project_id}/conversation/media/{media_id}", dependencies=[Depends(authz.require_reader)])
async def conversation_media(project_id: str, media_id: str, service=Depends(get_service)):
    """Step 24.2b (design-steward 7.8.12a "显示"): the bytes of one attachment of this project's
    conversation, for the panel's thumbnails, voice player and file downloads.

    Who: `require_reader` and the conversation read's own verdict, so an anonymous request and
    one carrying `X-AItelier-Read-As` are 403 even on a public project; a driver token is 403 on
    every project (hosts read their own libraries), and on the steward's project only the owner
    gets the bytes. Which: `u_...` (a
    dashboard upload: staging, then the library after the first ack), `m_...` named by this
    conversation (core.state_conversation_uploads.open_for_display); anything else is 404, a
    file that was cleaned is 410 `gone`. Headers as workspace_raw: the type the magic bytes
    decided, nosniff, `default-src 'none'` (security_headers only setdefaults), private
    caching, inline for images and voice and attachment otherwise."""
    from fastapi.responses import StreamingResponse
    try:
        handle, mime, kind, size = await asyncio.to_thread(
            uploads.open_for_display, service, project_id, media_id, media_library.staging_dir())
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, uploads.UNKNOWN_MEDIA) from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, "gone") from exc

    def chunks():
        with handle:
            while data := handle.read(1024 * 1024):
                yield data

    return StreamingResponse(chunks(), media_type=mime, headers={
        "Content-Length": str(size), "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Cache-Control": "private",
        "Content-Disposition": "inline" if kind in ("image", "voice") else "attachment"})


@router.post("/{project_id}/start", dependencies=[Depends(authz.require_writer)])
async def start(project_id: str, body: Start, request: Request, runtime=Depends(manager)):
    try:
        return await runtime.start(project_id, provider=body.provider, model=body.model,
                                   actor=authz.request_actor(request))
    except StateWriteBusy:
        # A CONTROL-PLANE stamp (`enabled`) that could not take SQLite's write
        # lock is retryable, not a ValueError describing a broken request. It is
        # re-raised so the app-wide default answers 503 + `Retry-After`; the
        # clause below would otherwise relabel it 422 and the caller would read a
        # "retry in a second" condition as "your request is invalid". It cannot
        # be a silent 200 either: the row was not written, so nothing started.
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{project_id}/stop", dependencies=[Depends(authz.require_writer)])
async def stop(project_id: str, runtime=Depends(manager)):
    try:
        return await runtime.stop(project_id)
    except StateWriteBusy:
        # Same refusal, same answer as `start`: the stop was NOT recorded
        # (`enabled=0` was refused), so it must not answer 200 and must not be
        # relabelled as an invalid request.
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def owned(project_id, request, runtime):
    identity = authz.request_identity(request)
    row = runtime.get(project_id)
    if row is None or identity is None or identity.driver_id != row["driver_id"]:
        raise HTTPException(403, "this runtime belongs to another driver")
    return row


async def owned_off_loop(project_id, request, runtime):
    """`owned` for an ASYNC handler: it is sqlite, so it runs on a worker thread.

    `request_identity` resolves the presented driver token through
    `DriverRegistry.lookup_token` - a SELECT plus a throttled `last_seen_at`
    UPDATE that COMMITs - and `runtime.get` reads the driver row. On the loop
    thread those are three connections opened and closed per request, each close
    a WAL checkpoint plus fdatasync (one sampled episode of this path was 2.81 s,
    `iss-ecea53516eb543c4`). The verdict, the row and the 403 are unchanged; only
    the thread changes.
    """
    return await asyncio.to_thread(owned, project_id, request, runtime)


@router.get("/{project_id}/context", dependencies=[Depends(authz.require_reader)])
def context(project_id: str, request: Request, runtime=Depends(manager), service=Depends(get_service)):
    row = owned(project_id, request, runtime)
    graph = execute(service, "project_overview", {"project_id": project_id})
    project_note = execute(service, "get_driver_note", {"project_id": project_id})
    private_note = execute(service, "get_driver_note", {"driver_id": row["driver_id"]})
    # VERIFIED nodes are closed history nobody acts on: they stay in live State
    # and the snapshot only counts them. Everything else is listed with its status.
    open_nodes = [n for n in graph["nodes"] if n["status"] != "VERIFIED"]
    counts = {"VERIFIED": 0, **graph.get("counts", {})}
    verified = counts["VERIFIED"]
    return {"project_id": project_id, "driver_id": row["driver_id"], "event_seq": graph["event_seq"],
            "nodes": [{"name": n["node_key"], "status": n["status"]} for n in open_nodes],
            # Live issue work items (closed ones are history in live State).
            "issue_work": [{"name": w["node_key"], "issue_id": w["issue_id"], "status": w["status"]}
                           for w in graph.get("issue_work", [])],
            "node_counts": counts,
            "nodes_omitted": {"VERIFIED": verified,
                              "statement": f"{verified} VERIFIED node(s) omitted from this snapshot; read them from live State (project_overview)."},
            "project_note": project_note, "private_note": private_note,
            "driver_skill": STATE_DRIVER_GUIDE_INDEX,
            "runtime_skill": _runtime_skill()}


@router.get("/{project_id}/model-sessions", dependencies=[Depends(authz.require_reader)])
def model_sessions(project_id: str, request: Request, runtime=Depends(manager)):
    """Which concrete model answered this driver's sessions, and what it asked for.

    Driver-scoped exactly like `context`: the credential must belong to THIS
    project's driver, so a valid token for another driver is refused rather than
    turned into a read of somebody else's sessions, and the query itself is keyed
    on the project so no foreign row can be served. `served_by` is NULL when the
    gateway could not name a concrete model - never the route alias.
    """
    owned(project_id, request, runtime)
    return {"project_id": project_id, "sessions": runtime.session_models(project_id)}


@router.get("/{project_id}/route-input", dependencies=[Depends(authz.require_reader)])
def route_input_read(project_id: str, request: Request, runtime=Depends(manager)):
    """Step 24.2b (design-steward 7.8.12a "路线能力"): what this driver's root
    session and its child sessions may each be sent, `{root, child}`, by the
    route `/completion` uses for them. Driver-scoped like `model-sessions`."""
    return route_input(owned(project_id, request, runtime))


@router.post("/{project_id}/activity", dependencies=[Depends(authz.require_writer)])
async def activity(project_id: str, body: Activity, request: Request, runtime=Depends(manager)):
    row = await owned_off_loop(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")
    import json
    # The stamp is a sqlite write, so it waits for a write lock - on this thread
    # that wait IS the event loop, and every other request in the process waits
    # behind it. Handing the write to a worker thread keeps the loop free while
    # the database is busy; the call is still the same keyword call, and the SSE
    # push below still happens only after the row is committed.
    await asyncio.to_thread(runtime.update, project_id, phase=body.phase,
                            subagents_json=json.dumps(body.subagents), last_error=body.error)
    from api.sse_manager import stream_manager
    await stream_manager.push_log("dsh:" + row["driver_id"], json.dumps({
        "type": "driver_activity", "phase": body.phase, "subagents": body.subagents,
        "entry": body.event.model_dump() if body.event else None}))
    return {"ok": True}


def _renewable_attempts(service, project_id: str, driver_id: str):
    """This driver's active attempts on nodes that can still accept a report."""
    # A CLOSED node can never accept a report, so renewing its attempt would push
    # its lease expiry - and with it the design §4.4 reclaim deadline (expiry +
    # 900 s grace) - forward forever: the attempt never becomes reclaimable and a
    # non-admin director can neither abandon nor take it over. Hence the join on
    # state_nodes; the filters on the attempt itself are unchanged.
    closed = ",".join("?" * len(CLOSED_STATUSES))
    with service.db.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT a.attempt_id,a.owner_fence AS fence FROM state_attempts a "
            f"JOIN state_nodes n ON n.project_id=a.project_id AND n.node_key=a.node_key "
            f"WHERE a.project_id=? AND a.owner_driver_id=? "
            f"AND a.status IN ('reserved','launching','running','paused','unknown') "
            f"AND n.status NOT IN ({closed}) LIMIT 100",
            (project_id, driver_id, *sorted(CLOSED_STATUSES)))]


@router.post("/{project_id}/heartbeat", dependencies=[Depends(authz.require_writer)])
def heartbeat(project_id: str, request: Request, runtime=Depends(manager), service=Depends(get_service)):
    row = owned(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")
    claims = execute(service, "list_claims", {"project_id": project_id, "driver_id": row["driver_id"], "limit": 100})["claims"]
    attempts = _renewable_attempts(service, project_id, row["driver_id"])
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


def _carries_image(messages):
    """True when any message has an OpenAI `image_url` content part."""
    return any(isinstance(part, dict) and part.get("type") == "image_url"
               for m in messages if isinstance(m, dict) and isinstance(m.get("content"), list)
               for part in m["content"])


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
    row = await owned_off_loop(project_id, request, runtime)
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
    """The managed driver uses the same router, credentials and failover as pipelines.

    STREAMED, and for one measured reason: this used to await
    `gateway.generate_native` and only then return a dict, so no response
    header was sent until the model had answered. The resident driver calls
    this with Node's `fetch`, whose undici default `headersTimeout` is 300 s -
    a turn measured 2026-11-18 ended at exactly 300 s with `fetch failed` while
    the backend's own row showed the answer had arrived at 309 s, so a paid
    answer was discarded. The body is therefore the sibling `/work` endpoint's
    shape: one whitespace keepalive immediately (Starlette sends
    `http.response.start` before the first chunk, so the headers go out at
    once), another at every `_DSH_COMPLETION_KEEPALIVE_SEC` while the call is
    outstanding, and the SAME JSON object as the last chunk.
    """
    import asyncio
    from core.ai_router import AIGateway
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool
    row = await owned_off_loop(project_id, request, runtime)
    if not row["enabled"] or not runtime.running(project_id):
        raise HTTPException(409, "driver is stopped")
    sessions = runtime.gateways.setdefault(project_id, {})
    # The route this session ASKS for: the director's own configured model for
    # the root session, the worker route for every native child. Resolved for
    # every turn, not just the binding one, so the record always states the
    # route the call was made under.
    route = completion_route(row, body.session_id)
    # Step 24.2b backstop (7.8.12a): an image on a route that does not take
    # images is refused here, before any gateway exists, so nothing reaches a
    # provider. The adapter normally rewrites such images to a text line first.
    # A table with no `_input` measured nothing: no refusal, as before 24.2b.
    if _carries_image(body.messages) and input_measured() and "image" not in route_modalities(route):
        raise HTTPException(422, "route_no_image")
    if body.session_id not in sessions:
        # Native children (including resumed old children) have their own session.
        # Bind once per child: rotate across workers, keep tool turns sticky.
        gateway = AIGateway(route, enable_thinking=True, thinking_effort=body.reasoning_effort,
                            max_output_tokens=body.max_tokens)
        sessions[body.session_id] = (gateway, asyncio.Lock())
    gateway, lock = sessions[body.session_id]
    async def stream():
        # The lock is taken HERE, inside the body being streamed - not around
        # the `return` that builds the response. Returning a StreamingResponse
        # from inside `async with lock:` would release the lock when the handler
        # returns, i.e. BEFORE the generator runs, and two concurrent turns of
        # one session would share the gateway (its endpoint rotation, its
        # `_dsh_prefix`, its budget) at exactly the moment they must not.
        async with lock:
            gateway.max_output_tokens = body.max_tokens
            gateway.thinking_effort = body.reasoning_effort
            gateway.enable_thinking = body.reasoning_effort != "off"
            # A session's gateway lives as long as the session; without this a
            # failover onto the paid fallback would stick until a restart. But
            # moving to another endpoint throws away the provider's prompt cache:
            # the next call re-sends the whole context uncached (~130k tokens for
            # a director, measured). So an earlier endpoint is retaken only when
            # the cache is cold anyway - the conversation prefix changed (a
            # compaction, which also skips the post-failover interval; only a
            # parked endpoint is still refused) or the session sat idle for the
            # probe interval. A failover on an error still moves at once.
            prefix = hashlib.sha256(json.dumps(body.messages[:2], sort_keys=True, default=str)
                                    .encode()).hexdigest()
            if prefix != getattr(gateway, "_dsh_prefix", None):
                gateway.return_to_earlier(cold=True)
            elif time.time() - getattr(gateway, "_dsh_last_call", 0.0) >= ai_router._RETURN_PROBE_EVERY_S:
                gateway.return_to_earlier()
            gateway._dsh_prefix = prefix
            # The model call, in a worker thread, as a TASK rather than an
            # awaited coroutine: while it is outstanding the generator below
            # keeps writing a whitespace keepalive, which is what holds the
            # client's connection open (the sibling `/work` endpoint's shape).
            task = asyncio.create_task(
                run_in_threadpool(gateway.generate_native, body.messages, tools=body.tools))
            # HEADERS GO OUT NOW, with the first byte, and the connection is
            # kept alive through Node's fetch while the model thinks. Measured
            # 2026-11-18: undici's default `headersTimeout` (300 s) killed a
            # turn at exactly 300 s with `fetch failed` while the backend's own
            # row showed the model answered at 309 s - a paid answer thrown
            # away. `bodyTimeout` is ALSO 300 s, which is why the keepalive has
            # to REPEAT at a fixed interval instead of being sent once. A single
            # space is ordinary JSON whitespace, so the body still parses as
            # the one object yielded at the end.
            yield " "
            while not task.done():
                await asyncio.wait({task}, timeout=_DSH_COMPLETION_KEEPALIVE_SEC)
                if not task.done():
                    yield " "
            # Same propagation as the awaited call it replaces: an error out of
            # `generate_native` reaches the client as a failed body, not as a
            # turn with empty text.
            turn = task.result()
            gateway._dsh_last_call = time.time()
            # Persisted here, where the endpoint was chosen, and with the concrete
            # model that answered (`active_model` is the same value returned as
            # `served_by`): the child's own session file only keeps the route alias.
            # BEST-EFFORT: the model has already answered and the turn is already
            # paid for, so a bookkeeping write that fails (a locked SQLite file past
            # the busy timeout, a read-only or full disk) must not turn an answer
            # the driver is owed into a 500. `Exception`, not `BaseException`: a
            # cancellation still propagates. The failure is reported, not swallowed
            # - an unrecorded turn has to be visible to an operator.
            #
            # OFF THE EVENT LOOP, and still AWAITED. `record_model_call` ends in an
            # explicit `COMMIT`, which in WAL mode fsyncs: measured holding the loop
            # thread in uninterruptible disk sleep (wchan `jbd2_log_wait_commit`) for
            # seconds, 8 of 10 captured loop-blocking stacks and 64 of 70 sampled
            # episodes (`iss-ecea53516eb543c4`). The write runs in a worker thread,
            # so what the loop waits for is the write's DURATION, never its disk
            # sleep - and it is still the same awaited call, so the row is committed
            # before this response goes out and a failure is still reported below
            # rather than fired and forgotten. The connection that write closes is
            # handed to the closer thread by `core/off_loop_sqlite`.
            try:
                await asyncio.to_thread(runtime.record_model_call, project_id,
                                        body.session_id, route=route,
                                        served_by=gateway.active_model)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "dsh driver: could not record the answering model for project %s "
                    "session %s (%s: %s) - the turn is served unrecorded",
                    project_id, body.session_id, type(exc).__name__, exc)
            # The JSON object this endpoint has always returned: same six keys,
            # same values. Everything above still happens BEFORE it is written,
            # so the record is committed by the time the driver gets the turn.
            yield json.dumps({"text": turn.text, "tool_calls": turn.tool_calls,
                              "reasoning_content": turn.reasoning_content,
                              "truncated": turn.truncated, "usage": gateway.last_usage,
                              "served_by": gateway.active_model})
    return StreamingResponse(stream(), media_type="application/json")
