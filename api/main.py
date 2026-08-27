# api/main.py
# [修复说明] 在现有的 FastAPI 实例中补充 Scheduler 的生命周期挂载。
# [变更] on_event("startup") → lifespan context manager (FastAPI 推荐方式)。

import os as _os
from pathlib import Path as _Path
_env_file = _Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                _key = _key.strip().removeprefix("export ")
                _val = _val.strip().strip("\"'")
                if _key not in _os.environ:
                    _os.environ[_key] = _val

from contextlib import asynccontextmanager
from pathlib import Path as _Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from core import cf_access
from api import _read_cache
from api import authz
from starlette.routing import Route as _StarletteRoute
from api.mcp_router import MCPEndpoint
from api.routers import router as tasks_router
from api.project_routers import router as projects_router
from api.settings_routers import router as settings_router
from api.meta_routers import router as meta_router
from api.agent_routers import router as agent_router
from api.run_routers import router as run_router
from api.config_routers import router as config_router
from api.admin_routers import router as admin_router
from api.repo_routers import router as repo_router
from api.model_routers import router as model_router
from api.sse_manager import stream_manager
from core.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动/关闭生命周期：初始化 skillflow NotificationBus → SSE 订阅 + 后台调度器"""
    import asyncio
    import json as _json
    from api.dependencies import get_skillflow

    # Test-composed apps get NO backend: no instance lock, no claim recovery,
    # no scheduler. recover_claims_on_startup() assumes it is the ONLY backend
    # — from a TestClient it would steal a live backend's in-flight claims and
    # teardown would cancel them mid-run (the orphaned-claim storm). Data-dir
    # isolation in conftest is the primary guard; this makes the skip explicit
    # and keeps test startups fast.
    if getattr(app.state, "_test_mode", False):
        # The MCP session manager still runs: it starts no backend, and a test app
        # that mounts the endpoint but cannot answer a single call would make the
        # suite green on an endpoint nobody could use.
        async with _mcp_endpoint.open().session_manager.run():
            try:
                yield
            finally:
                _mcp_endpoint.close()
        return

    loop = asyncio.get_running_loop()

    # Single-instance gate (MUST be first — before the destructive claim
    # recovery below, which assumes this is the only backend). Exactly one
    # AItelier backend may run per data directory; the host and the Docker
    # container share this lock. A second backend refuses to start with an
    # explicit message rather than silently shadowing the real one.
    from core.scheduler import acquire_instance_lock, _instance_lock_path
    if not acquire_instance_lock():
        import sys as _sys
        _lock = _instance_lock_path()
        print(
            "\n" + "=" * 74 + "\n"
            "  AItelier backend REFUSING TO START — another instance is running.\n"
            f"  Single-instance lock already held: {_lock}\n"
            "  Only ONE backend may run per data directory (host and the\n"
            "  Docker container share this lock). Stop the other instance — e.g. the\n"
            "  running `aitelier` container or a stray `uvicorn api.main` — then\n"
            "  retry. (If the real backend is down, that is what this is telling\n"
            "  you: nothing is shadowing it.)\n"
            + "=" * 74 + "\n",
            file=_sys.stderr, flush=True,
        )
        raise RuntimeError(
            f"AItelier single-instance lock held by another process: {_lock}"
        )

    # Initialize skillflow (lazy singleton, registers DPE pipeline)
    sf = get_skillflow()
    app.state.skillflow = sf
    # Wire the main event loop so notifications from worker threads
    # (e.g. PipelineEngine in thread-pool executor) bridge to SSE.
    sf.notifications.set_event_loop(loop)

    # ── NotificationBus → SSE bridge (single event path) ──────────
    _pid_cache: dict[str, str] = {}        # run_id → project_id
    _pname_cache: dict[str, str] = {}      # project_id → project name
    _task_cache: dict[str, str] = {}       # run_id → current task name
    _MAX_PID_CACHE = 2000

    _TASK_LOOP_STEPS = frozenset({
        "t_plan", "t_plan_review", "t_impl", "t_impl_review",
        "t_verify", "t_verify_review",
    })

    def _resolve_project_info(data: dict, rid: str):
        """Ensure project_id and project name are in the event data."""
        _resolve_run_info(data, rid)
        pid = data.get("project_id", "")
        if pid and pid not in _pname_cache:
            try:
                import sqlite3 as _sql
                from api.dependencies import DB_PATH as _DB_PATH
                _adb = _sql.connect(_DB_PATH)
                row = _adb.execute(
                    "SELECT name FROM runs WHERE project_id = ?",
                    (pid,),
                ).fetchone()
                _adb.close()
                _pname_cache[pid] = row[0] if row else pid
            except Exception:
                _pname_cache[pid] = pid
        if pid:
            data["_project_name"] = _pname_cache.get(pid, pid)

    def _resolve_task_context(data: dict, rid: str, step_id: str):
        """If this is a task-loop step, inject the current task name."""
        if step_id not in _TASK_LOOP_STEPS:
            return
        # Always query — loop state changes every task.  The old cache on
        # current_index never invalidated, causing notifications to show a
        # stale task name (e.g. "backend_setup" forever).
        try:
            import sqlite3 as _sql
            from api.dependencies import SKILLFLOW_DB_PATH as _SF_DB_PATH
            _sdb = _sql.connect(_SF_DB_PATH)
            row = _sdb.execute(
                "SELECT current_item FROM skillflow_loop_state WHERE run_id = ?",
                (rid,),
            ).fetchone()
            _sdb.close()
            if row and row[0]:
                task = row[0]  # current_item — the authoritative field (v2)
                _task_cache[rid] = task  # still cache for the hot path
        except Exception:
            pass
        task = _task_cache.get(rid, "")
        if task:
            data["_task_id"] = task

    _graph_cache: dict[str, str] = {}      # run_id → graph_name (config)

    def _resolve_run_info(data: dict, rid: str):
        """Ensure project_id + graph_name from the run (thread-safe)."""
        if rid and rid not in _pid_cache:
            try:
                import sqlite3 as _sql
                from api.dependencies import SKILLFLOW_DB_PATH as _SF_DB_PATH
                _sdb = _sql.connect(_SF_DB_PATH)
                row = _sdb.execute(
                    "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
                    (rid,),
                ).fetchone()
                _sdb.close()
                _pid_cache[rid] = row[0] if row else ""
                _graph_cache[rid] = row[1] if row else ""
            except Exception:
                _pid_cache[rid] = ""
                _graph_cache[rid] = ""
        if not data.get("project_id"):
            pid = _pid_cache.get(rid, "")
            if pid:
                data["project_id"] = pid
        # Carry the config identity so clients can route/render any config.
        if not data.get("graph_name"):
            graph = _graph_cache.get(rid, "")
            if graph:
                data["graph_name"] = graph

    async def _on_skillflow_event(notification):
        """Forward skillflow NotificationBus events to SSE."""
        payload = notification.payload
        step_id = notification.step_id or payload.get("step_id", "")
        run_id = notification.run_id or payload.get("run_id", "")
        data = {
            **payload,
            "type": notification.event_type,
            "_ts": notification.timestamp,
            "_step_id": step_id,
            "_run_id": run_id,
        }
        _resolve_project_info(data, run_id)
        _resolve_task_context(data, run_id, step_id)
        if notification.step_id and "step_id" not in data:
            data["step_id"] = notification.step_id
        if notification.run_id and "run_id" not in data:
            data["run_id"] = notification.run_id
        payload_str = _json.dumps(data)
        await stream_manager.push_log("__global__", payload_str)
        # There was a second push to channel "0" here. Nothing subscribes to
        # it — the only consumers are "__global__" and /api/tasks/{id}/stream,
        # which the SPA never opens — so push_log took its no-consumer branch
        # every time and appended to a buffer that is never drained. Measured
        # on the live run: ~245 events/10min at ~1.8KB each, so roughly 60MB a
        # day of unreclaimable heap for as long as the process lives.

    sf.notifications.subscribe(_on_skillflow_event)

    # Recover any claimed steps left by a previous (crashed/killed) process.
    # Server is singleton — any claim at startup is definitively stale.
    from core.scheduler import recover_claims_on_startup
    recover_claims_on_startup()

    app.state.scheduler = start_scheduler()
    print("DPE APScheduler started. skillflow NotificationBus → SSE bridge active.")
    # The MCP sub-app is MOUNTED, and a mounted app's own lifespan is never run by
    # the parent — its only job is `session_manager.run()`, so without this every
    # POST /mcp fails on an uninitialised task group. Mounting alone looks like it
    # worked (routes resolve, the config is right) right up until the first call.
    async with _mcp_endpoint.open().session_manager.run():
        try:
            yield
        finally:
            _mcp_endpoint.close()
    # Shutdown
    if hasattr(app.state, "scheduler") and app.state.scheduler:
        app.state.scheduler.shutdown(wait=True)


_mcp_endpoint = MCPEndpoint()

app = FastAPI(
    title="AItelier Engine API",
    description="Skillflow config-run orchestration control plane",
    version="1.0.0",
    lifespan=lifespan,
)

# 挂载路由
app.include_router(tasks_router)
app.include_router(projects_router)
app.include_router(settings_router)
app.include_router(meta_router)
app.include_router(agent_router)
app.include_router(run_router)
app.include_router(config_router)
app.include_router(admin_router)
app.include_router(repo_router)
app.include_router(model_router)

# When running in Docker (and fronted by Cloudflare Access), requests arrive
# from the Docker bridge gateway / the tunnel — never 127.0.0.1 — so the
# localhost guard is disabled via AITELIER_ALLOW_EXTERNAL=1. Auth is then
# expected to be enforced at the edge (e.g. Cloudflare Access).
_ALLOW_EXTERNAL = _os.getenv("AITELIER_ALLOW_EXTERNAL", "").lower() in ("1", "true", "yes")


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    """Reject requests from non-localhost clients (unless external access is allowed)."""
    if _ALLOW_EXTERNAL or getattr(request.app.state, "_test_mode", False):
        return await call_next(request)
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="External access denied")
    return await call_next(request)


# ── Write-gate: reads open (Cloudflare Access guards them at the edge),
#    mutating requests require an allowlisted Cloudflare identity or the admin
#    token (used by the host CLI, which reaches the origin without a JWT). Only
#    enforced when Cloudflare verification is configured. Writer determination
#    lives in api/authz so the GET-endpoint guard (require_writer) can't diverge
#    from this middleware. ───────────────────────────────────────────────────
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# Security headers. The SPA renders content authored by agents that read the
# open web, and the page is served to anonymous strangers, so a CSP is the
# compensating control for every `{@html}` site — the ones that exist and the
# ones somebody adds next.
#
# Verified against the real build before choosing the directives: no inline
# <script>, no eval / new Function (Svelte 5 runes compile without them), CSS is
# extracted to a file, but inline `style=` ATTRIBUTES are used (progress bars)
# and Pico ships data: SVG backgrounds — hence 'unsafe-inline' for style-src and
# data: for img-src, and nothing else loosened. All three <form>s are
# onsubmit+preventDefault, so `form-action 'none'` costs nothing and
# independently neuters a phishing form written into a markdown file.
#
# Set as a RESPONSE HEADER, not an index.html meta tag: `frame-ancestors` is
# ignored in a meta tag, a meta tag would not cover /assets, and index.html is a
# build input baked into the image, so it would drift from what is served.
_SEC_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self'; "
        "object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SEC_HEADERS.items():
        # setdefault, NOT assignment: the raw-image endpoint sets a STRICTER
        # policy of its own (`default-src 'none'`), and overwriting it here
        # would LOOSEN the one response that was already thought about.
        response.headers.setdefault(k, v)
    return response


@app.middleware("http")
async def invalidate_read_cache(request: Request, call_next):
    """Drop the few-second read cache after anything that could have changed.

    `api/_read_cache` exists so that N dashboard tabs cost one computation
    instead of N. The cost of any cache is staleness, and the one place
    staleness is actually surprising is right after you did something: create a
    project and it is reasonable to expect the list to contain it, not to
    contain it in up to five seconds.

    Deliberately NOT hooked into `write_gate` — that middleware returns early
    when the gate is disabled or in test mode, so invalidation would silently
    not happen in exactly the configurations where nobody is watching for it.
    It found the bug that way too: an integration test passed alone and failed
    in the suite, because one test's writes were invisible to the next test's
    read.
    """
    response = await call_next(request)
    if request.method not in _SAFE_METHODS:
        _read_cache.clear()
    return response


@app.middleware("http")
async def write_gate(request: Request, call_next):
    """Require write authorization for mutating requests."""
    if getattr(request.app.state, "_test_mode", False) or not authz.gate_enabled():
        return await call_next(request)
    if request.method in _SAFE_METHODS or request.url.path == "/health":
        return await call_next(request)
    # MCP posts every call — `list_pipelines` and `edit_pipeline` alike — to this
    # one path, so the METHOD cannot classify it and this middleware has only two
    # settings, both wrong (gate it and reads break; exempt it and writes open).
    # The verdict moves to the tool: api/mcp_router._authorize re-applies exactly
    # this check for every tool declared `write`. Do not remove one half.
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        return await call_next(request)
    code = authz.write_denial_reason(request)
    if not code:
        return await call_next(request)
    return JSONResponse(authz.denial_body(code), status_code=403)


@app.get("/health")
def health_check():
    """系统探针"""
    return {"status": "ok", "engine": "DPE SOTA v3.0"}


@app.get("/api/me")
def whoami(request: Request):
    """Current identity + write permission (for the web UI to reflect state).

    Also reports where to sign in and whether a credential was REJECTED, because
    the two failures a reader hits look identical from the UI otherwise: "I have
    no credential" and "I have one the origin refuses" both render as a
    read-only session. The second is the one that actually happens — an Access
    application re-created with a fresh AUD while ``AITELIER_CF_AUD`` still names
    the old one authenticates the browser and is then rejected here, silently.
    """
    token = (request.headers.get("Cf-Access-Jwt-Assertion")
             or request.cookies.get("CF_Authorization", ""))
    email = cf_access.email_from_request_headers(request.headers, request.cookies)
    if email:
        from api.dependencies import db_instance
        db_instance.upsert_user(email)
    return {
        "email": email,
        "can_write": authz.request_can_write(request),
        "gate_enabled": authz.gate_enabled(),
        # Empty unless the deployment declares one — a sign-in button pointing at
        # an Access application that does not exist is a button to nowhere.
        "signin_url": _os.getenv("AITELIER_SIGNIN_URL", "").strip(),
        # Carried a credential, got nobody: bad audience, wrong issuer, expired,
        # or an org that no longer signs it. Never says which — that is for the
        # operator's logs, not for an unauthenticated caller.
        "auth_error": "credential_rejected" if (token and not email) else None,
    }


@app.get("/signin")
def signin(request: Request):
    """Where the Cloudflare Access round trip lands, and bounces home from.

    The public site has no Access application in front of it — reads are open
    to anyone. Sign-in works by protecting THIS path alone with its own
    application: Cloudflare authenticates the visitor here, sets its
    `CF_Authorization` cookie for the host, and forwards the request to us.
    Everything else on the origin stays public.

    So this endpoint does almost nothing on purpose. It records the identity
    (the same upsert `/api/me` does) and sends the browser to the app, which
    re-reads `/api/me` on boot and renders as a writer. It redirects even when
    verification FAILS — landing on a read-only UI that says "Sign in again"
    beats a bare error page, and `/api/me` reports the refusal.

    Unprotected, this is a plain redirect to `/` and grants nothing: authority
    comes from the verified JWT, never from having reached this path.
    """
    email = cf_access.email_from_request_headers(request.headers, request.cookies)
    if email:
        from api.dependencies import db_instance
        db_instance.upsert_user(email)
    # 303: the browser must GET the destination, whatever method got here.
    return RedirectResponse("/", status_code=303)


@app.get("/api/events/stream")
async def stream_global_events(request: Request):
    """
    Global SSE endpoint for CLI dashboard.
    Broadcasts all pipeline events (project + task) for real-time status updates.

    Identity is resolved ONCE at connect (verified Cloudflare Access JWT →
    email, else anonymous) and recorded next to the connection's queue, which
    is what /api/connections reports. The stream itself is open either way.
    """
    who = None
    try:
        from core import cf_access
        who = cf_access.email_from_request_headers(request.headers, request.cookies)
    except Exception:
        pass
    return StreamingResponse(
        stream_manager.event_generator("__global__", who=who),
        media_type="text/event-stream",
    )


@app.get("/api/connections")
def list_connections(request: Request):
    """Who is watching right now — the live SSE connection table.

    Split visibility: the COUNTS are public (they are also broadcast to every
    tab as `presence` events), but the per-connection detail carries verified
    emails, and "is the operator at the dashboard right now" is exactly the
    working-hours intelligence the reflog leak taught us not to hand out. So
    the detail list is included only for a caller the write gate would let
    write.
    """
    from api import authz
    snap = stream_manager.connection_snapshot()
    auth = sum(1 for m in snap if m.get("who"))
    body = {"total": len(snap), "authenticated": auth,
            "anonymous": len(snap) - auth}
    if authz.request_can_write(request):
        body["viewers"] = snap
    return body


# ── MCP endpoint ──
# Registered BEFORE the SPA (a mount at "/" swallows everything after it) and after
# the middlewares, so `write_gate` still sees the request — which is exactly the
# problem: MCP posts every call, read or write, to this one path, so the method
# tells the gate nothing. The path is exempted from the method gate above and
# `api/mcp_router._authorize` re-applies the same verdict PER TOOL instead. Those
# two halves are one decision; changing either alone opens or breaks the endpoint.
#
# Route, not mount. `app.mount("/mcp", …)` compiles to a regex that matches
# "/mcp/…" but NOT the bare "/mcp" — so `POST /mcp`, the URL every client config
# actually contains, fell through to the SPA's catch-all StaticFiles mount, which
# allows only GET/HEAD and answered 405. The endpoint looked mounted, `/mcp/`
# worked, and the documented URL did not. Both spellings are registered so neither
# depends on a redirect (a 307 on a POST is a coin flip across HTTP clients).
app.router.routes.append(_StarletteRoute("/mcp", endpoint=_mcp_endpoint))
app.router.routes.append(_StarletteRoute("/mcp/", endpoint=_mcp_endpoint))


# ── Serve compiled SPA ──
# Registered LAST: Starlette matches in registration order, and a mount at
# "/" swallows every path registered after it (it 404'd /health when it sat
# above the route definitions).
# AITELIER_WEB_DIST: in Docker the bundle is baked OUTSIDE /app (the .:/app
# live-source mount would shadow web/dist, which doesn't exist on the host).
_WEB_DIST = _Path(
    _os.getenv("AITELIER_WEB_DIST")
    or _Path(__file__).resolve().parent.parent / "web" / "dist"
)
if _WEB_DIST.is_dir():
    from fastapi.responses import FileResponse

    @app.get("/")
    async def spa_index():
        """Serve the SPA entry with no-cache so a deploy is picked up at
        once. StaticFiles sends no Cache-Control, so browsers heuristically
        cached index.html and kept loading STALE hashed bundles after a
        deploy (the vite asset names are content-hashed and safe to cache;
        the html that references them is not)."""
        return FileResponse(
            _WEB_DIST / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="spa")
