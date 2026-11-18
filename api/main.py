# api/main.py
# [修复说明] 在现有的 FastAPI 实例中补充 Scheduler 的生命周期挂载。
# [变更] on_event("startup") → lifespan context manager (FastAPI 推荐方式)。

# Deliberately NO import-time `.env` load here (removed 2026-11-08). This module
# is imported by every test and by the container, and the old read ran BEFORE
# `from core import cf_access` — which reads AITELIER_CF_TEAM_DOMAIN /
# AITELIER_CF_AUD once — so an untracked `<repo root>/.env` decided the write
# gate of any process that merely imported the app (iss-f909af237ae944af).
# The parser lives in core/dotenv_boot.py and is called EXPLICITLY by the boot
# entries that need it (cli/server.py, cli/client.py). The server container gets
# its configuration from compose `env_file`, so nothing here needs the file.

import asyncio as _asyncio
import hashlib as _hashlib
import os as _os
import threading as _threading
import time as _time

from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path as _Path
from datetime import datetime as _datetime, timezone as _timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from core import cf_access
# Re-exported: the covered file list lived in this module before the rule moved
# to core/source_identity.py, and readers still ask api.main which files the
# digest covers (the noqa below is for that re-export). The capture itself is
# the shared implementation.
from core.source_identity import (SOURCE_IDENTITY_MODULES,  # noqa: F401
                                  capture as _capture_source_identity)
from api import _read_cache
from api import authz
from starlette.routing import Route as _StarletteRoute
from api.mcp_router import MCPEndpoint
from api.routers import router as tasks_router
from api.project_routers import router as projects_router
from api.settings_routers import router as settings_router
from api.meta_routers import router as meta_router
from api.run_routers import router as run_router
from api.run_history_routers import router as run_history_router
from api.config_routers import router as config_router
from api.admin_routers import router as admin_router
from api.driver_routers import router as driver_router
from api.dsh_driver_routers import router as dsh_driver_router
from api.steward_routers import router as steward_router
from api.repo_routers import router as repo_router
from api.model_routers import router as model_router
from api.state_graph_routers import router as state_graph_router
from api.novel_routers import router as novel_router
from api.sse_manager import stream_manager
from core.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动/关闭生命周期：初始化 skillflow NotificationBus → SSE 订阅 + 后台调度器"""
    import asyncio
    import json as _json
    from api.dependencies import get_skillflow

    # In-process witness for a loop parked in a syscall
    # (core/loop_stack_watchdog.py). Armed from AITELIER_LOOP_STACK_DUMP, whose
    # DEFAULT IS OFF: a deployment that sets nothing gets no thread and no file,
    # exactly as before. The heartbeat runs on the loop this lifespan serves, so
    # a loop that stops being turned is what leaves the dump under
    # <data volume>/logs/loop-stack-dumps/. Best-effort: startup never depends
    # on it. A deployment that opts in must also accept the GIL boundary stated
    # in that module: a block inside a C extension that holds the GIL produces
    # no dump, because the watchdog thread cannot run either.
    from core.loop_stack_watchdog import start_from_env as _arm_loop_stack_watchdog
    from core.loop_stack_watchdog import shutdown as _disarm_loop_stack_watchdog
    _loop_stack_watchdog = _arm_loop_stack_watchdog()

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
        try:
            async with _mcp_endpoint.open().session_manager.run():
                try:
                    yield
                finally:
                    _mcp_endpoint.close()
        finally:
            _disarm_loop_stack_watchdog(_loop_stack_watchdog)
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
    # Same capture for the outbox-bypassing push (llm_progress liveness ticks).
    from api.sse_manager import set_main_loop
    set_main_loop(loop)

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
        # Creation is published before its transaction commits. Prefer the
        # identities already carried by that event; a separate reader may not
        # see the row yet. Never make a transient miss a permanent cache hit.
        if rid:
            if data.get("project_id"):
                _pid_cache.setdefault(rid, data["project_id"])
            if data.get("graph_name"):
                _graph_cache.setdefault(rid, data["graph_name"])
        if rid and (rid not in _pid_cache or rid not in _graph_cache):
            try:
                import sqlite3 as _sql
                from api.dependencies import SKILLFLOW_DB_PATH as _SF_DB_PATH
                _sdb = _sql.connect(_SF_DB_PATH)
                row = _sdb.execute(
                    "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
                    (rid,),
                ).fetchone()
                _sdb.close()
                if row:
                    if row[0]:
                        _pid_cache.setdefault(rid, row[0])
                    if row[1]:
                        _graph_cache.setdefault(rid, row[1])
            except Exception:
                pass  # A later event retries; no empty identity is cached.
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

    # The first dashboard read should not be the request that pays for the
    # process's warm-up: on this tree it cost 14,249.4 ms, 4,527.5 ms of it the
    # engine's first boot and 8,205.1 ms the cache-stats trace pass (see
    # warm_first_dashboard_read and the investigation it cites). Started here,
    # while the rest of startup still runs, on a DAEMON thread that nothing
    # awaits and nothing fails on — startup time is unchanged and a test app
    # never reaches this branch.
    _threading.Thread(target=warm_first_dashboard_read,
                      name="first-dashboard-read", daemon=True).start()

    # State claim tables: run their one-time schema migration (the taken_over
    # status) now, before serving, instead of inside the first request. A
    # failure is logged and only switches claim takeover off.
    try:
        from api.dependencies import get_db_manager as _state_db
        from core.state_claims import initialize as _initialize_claims
        _initialize_claims(_state_db())
    except Exception:  # noqa: BLE001 - never block startup on this
        import logging as _logging
        _logging.getLogger(__name__).exception("State claim table initialization at startup failed")

    # Recover any claimed steps left by a previous (crashed/killed) process.
    # Server is singleton — any claim at startup is definitively stale.
    from core.scheduler import (recover_claims_on_startup,
                            recover_leases_on_startup)
    recover_claims_on_startup()
    # Leases outlive the process that took them: a crash between "the run
    # ended" and "the lease was released" leaves a checkout that nothing else
    # would ever revisit. Each is re-verified individually (terminal AND
    # drained), so a run that was mid-drain keeps its checkout.
    recover_leases_on_startup()

    app.state.scheduler = start_scheduler()
    from api.dsh_driver_routers import manager as dsh_manager
    from starlette.requests import Request as _Request
    dsh_runtime = dsh_manager(_Request({"type": "http", "app": app}))
    await dsh_runtime.resume()
    # Steward proposals left `executing` by a crash get a definite end, and
    # results no steward received yet are delivered (core/state_steward.py).
    try:
        from api.dependencies import get_db_manager as _steward_db
        from core.state_service import StateService as _StewardState
        await _asyncio.to_thread(_StewardState(_steward_db(), actor="steward-recovery",
                                               project_read_trusted=True).steward.recover, dsh_runtime)
    except Exception:  # noqa: BLE001 - never block startup on this
        import logging as _logging
        _logging.getLogger(__name__).exception("Steward proposal recovery at startup failed")
    print("DPE APScheduler started. skillflow NotificationBus → SSE bridge active.")
    # The MCP sub-app is MOUNTED, and a mounted app's own lifespan is never run by
    # the parent — its only job is `session_manager.run()`, so without this every
    # POST /mcp fails on an uninitialised task group. Mounting alone looks like it
    # worked (routes resolve, the config is right) right up until the first call.
    try:
        async with _mcp_endpoint.open().session_manager.run():
            try:
                yield
            finally:
                _mcp_endpoint.close()
    finally:
        # Stop admission and settle owned maintenance on every lifespan exit.
        try:
            await dsh_runtime.close()
            if hasattr(app.state, "scheduler") and app.state.scheduler:
                app.state.scheduler.shutdown(wait=True)
        finally:
            import sys
            from core.scheduler import settle_scheduler_maintenance
            await settle_scheduler_maintenance(exit_error=sys.exception())
            _disarm_loop_stack_watchdog(_loop_stack_watchdog)


_mcp_endpoint = MCPEndpoint()


def warm_first_dashboard_read():
    """Build the engine and the default `GET /api/runs` answer, OFF the request path.

    Measured on this tree (docs/investigations/run-cold-read-20261118): the first
    `GET /api/runs` of a process cost **14,249.4 ms**, of which **4,527.5 ms** was
    the process's first `get_skillflow()` (reached through FastAPI's dependency
    resolution for `Depends(get_config_registry)`) and 8,205.1 ms the cache-stats
    trace pass. Both are once-per-process work that a HTTP request should never
    have been the first to ask for: a cold process makes every dashboard visitor
    after a restart pay them, in the one process that also drives live runs.

    So the lifespan starts this on a DAEMON thread and does not wait for it.
    Three rules, all of them the difference between "warm the cache" and "turn
    startup into a request":

    * **It never raises.** Everything is caught and logged at WARNING: an
      unreachable database, a failing engine boot or a broken run must leave the
      backend starting normally, with the first `/api/runs` merely as expensive
      as it was before this existed.
    * **It never blocks startup.** The thread is started before the lifespan
      yields, and nothing joins it — `TestClient`-style startups return at once,
      and a test app (`_test_mode`) never reaches this code at all, because it is
      in the served backend's branch only.
    * **It warms the SAME key the request path reads** (`api/run_routers`'s
      `("runs", owner, include_all)` for an anonymous caller with the default
      window), through `api/_read_cache.cached`, so a request that arrives while
      this is still running WAITS for this one pass instead of starting a second
      (`_read_cache`'s per-key lock is what makes a miss cost one computation).

    Returns a small dict of the two spans on success, None on failure — for a
    probe to read, never for a caller to depend on.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    if getattr(app, "dependency_overrides", None):
        # A composed app (tests swap the DB/registry underneath these routes) is
        # not the production composition, and this function builds its value
        # from the SINGLETONS — warming a key that the overridden routes would
        # answer differently is worse than not warming at all.
        _log.info("first-dashboard-read prefetch skipped: this app carries "
                  "dependency overrides")
        return None
    started = _time.monotonic()
    try:
        from api.dependencies import (get_config_registry, get_db_manager,
                                      get_skillflow)
        engine_started = _time.monotonic()
        get_skillflow()
        engine_ms = round((_time.monotonic() - engine_started) * 1000.0, 1)
        from api import run_routers
        cutoff = run_routers._window_cutoff()
        _read_cache.cached(
            ("runs", None, False),
            lambda: run_routers._list_all_runs_uncached(
                None, get_db_manager(), get_config_registry(), cutoff=cutoff),
        )
    except Exception:  # noqa: BLE001 - startup must never fail because of this
        _log.warning(
            "first-dashboard-read prefetch failed; the first GET /api/runs will "
            "compute inline exactly as it did before this existed",
            exc_info=True)
        return None
    spans = {"engine_ms": engine_ms,
             "warm_ms": round((_time.monotonic() - started) * 1000.0, 1)}
    _log.info("first-dashboard-read prefetch done: engine %.1f ms, "
              "/api/runs %.1f ms total", spans["engine_ms"], spans["warm_ms"])
    return spans


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
app.include_router(run_router)
app.include_router(run_history_router)
app.include_router(config_router)
app.include_router(admin_router)
app.include_router(driver_router)
app.include_router(dsh_driver_router)
app.include_router(steward_router)
app.include_router(repo_router)
app.include_router(model_router)
app.include_router(state_graph_router)
app.include_router(novel_router)

# A project-privacy refusal raised at the `execute` chokepoint answers 403 on every
# route of the product app — including one a route author forges by reusing the real
# guard and declaring a public action, because such a handler still reaches `execute`.
from api.state_http import apply_project_privacy, apply_state_write_busy
apply_project_privacy(app)
# The same placement for the retryable write refusal: a State write that could not
# take SQLite's lock inside its budget answers 503 + Retry-After on EVERY route,
# including the ones (e.g. the driver heartbeat) that call `execute` themselves and
# never passed through `api.state_http._call`.
apply_state_write_busy(app)

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


async def _write_denial_reason(request: Request):
    """`authz.write_denial_reason`, on a worker thread - it is not free.

    With the driver feature on, the verdict resolves the presented token through
    `DriverRegistry.lookup_token`: a SELECT plus a throttled `last_seen_at`
    UPDATE whose COMMIT fsyncs the WAL, every connection opened and closed on
    whichever thread called us. Every MUTATING request goes through this gate,
    so on the loop thread that measured as 2.81 s of one request's stall
    (`iss-ecea53516eb543c4`). The verdict is identical either way - the gate
    still answers 403 with the same typed code - and the caller still AWAITS it,
    so no request is let through before the verdict exists.

    FastAPI already runs `require_writer`/`require_reader` - the SAME verdict as
    a dependency - on the threadpool by its own rules (sync `def` dependencies),
    and `list_connections` above already hands this exact call to
    `run_in_threadpool`. This middleware, being `async`, was the one place the
    same blocking verdict ran on the loop.
    """
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(authz.write_denial_reason, request)


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
        # The one exception: a steward token has no MCP surface at all, reads
        # included (plugin step 12), so it is refused here, before any tool. Only
        # an off-tunnel token can be a steward's, so browser and tunnel callers pay
        # no lookup.
        if (authz.presented_token(request) and not authz.is_via_cloudflare(request)
                and await _write_denial_reason(request) == authz.STEWARD_READ_ONLY):
            return JSONResponse(authz.denial_body(authz.STEWARD_READ_ONLY), status_code=403)
        return await call_next(request)
    # `/api/state` has the same property as `/mcp`: one path family carries BOTH
    # classes. `POST /api/state/query/{action}` serves the public reads and the
    # private ones, and the METHOD cannot tell them apart — gate it and the
    # public graph breaks for every anonymous visitor, exempt it and the
    # commands open. `GET /api/state/...` never reached this middleware at all
    # (the safe-method return above), so gating it here never even covered the
    # reads this change opens. The verdict therefore moves to the route:
    # api/state_http declares, on EACH route, the action it serves, and ONE
    # router-wide guard derives the class from core.state_commands.read_visibility
    # (which fails CLOSED). A route that declares nothing is REFUSED — there is
    # no dependency to remember and none to forget. Those two halves only make
    # sense together. Do not remove one half.    # sense together. Do not remove one half.
    #
    # The director-messaging path keeps its closed v2 envelope here: that
    # envelope IS this middleware's contract with the adapter, so it is
    # answered before the exemption, not after it.
    state_action = request.url.path.rsplit("/", 1)[-1]
    if request.url.path.startswith("/api/state/") and state_action in {
            "send_director_message", "list_director_messages",
            "acknowledge_director_message", "resolve_director_message"}:
        if await _write_denial_reason(request):
            from core.director_messaging_protocol import DirectorMessageError
            return JSONResponse(DirectorMessageError("unauthorized").as_dict(), status_code=403)
        return await call_next(request)
    if request.url.path == "/api/state" or request.url.path.startswith("/api/state/"):
        return await call_next(request)
    code = await _write_denial_reason(request)
    if not code:
        return await call_next(request)
    return JSONResponse(authz.denial_body(code), status_code=403)


# The godot harness an AUTHORED repository gate imports at run time. That gate's
# subprocess imports this copy, baked into the image at build time, ahead of the
# one in the working tree. A NORMAL gate imports neither: it takes the harness in
# its binding's `source` directory. /health reports this copy's identity so a
# stale image is visible from outside. Module-level so a test can point it at a
# temporary file.
GATE_EXPORT_HARNESS = _Path("/opt/aitelier-gate-imports/docker/godot/godot_harness.py")


def _gate_harness_identity() -> dict:
    """SHA-256 of the gate export's bytes, read at request time.

    Deliberately NOT cached: a value kept across calls can outlive a rewritten
    file, and the probe is cheap next to a request. A missing or unreadable file
    is declared (null digest plus a reason naming the path), never raised.
    """
    path = _Path(GATE_EXPORT_HARNESS)
    try:
        digest = _hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return {"path": str(path), "sha256": None,
                "reason": f"could not read {path}: {type(exc).__name__}"}
    return {"path": str(path), "sha256": digest, "reason": None}


# The `source_identity` digest is CAPTURED at import, ONCE, by core.source_identity
# - the one implementation of the rule, which deploy/loaded_source.py also
# recomputes with (the covered file list lives there too). This value is the
# witness: a merge changes the files on disk, but this value can only change by
# restarting the process, so a probe can tell "the disk says X" (gate_harness)
# from "the process is running X" (here).
_SOURCE_IDENTITY = _capture_source_identity(
    _Path(__file__).resolve().parent.parent)


def _process_age() -> dict:
    """When the serving process started and how long it has been up, read per request.

    Both figures come from the kernel's record of THIS process (its start time in
    /proc/self/stat, against the boot clock), so they describe the process that is
    answering now. They are the age of the process, not of the container, and they
    cannot count restarts: a process restarted in place starts again from zero, and
    the one it replaced is simply gone. Missing /proc is declared, never raised.
    """
    try:
        with open("/proc/self/stat", "rb") as handle:
            stat = handle.read()
    except OSError as exc:
        return {"started_at": None, "uptime_seconds": None,
                "reason": f"could not read /proc/self/stat: {type(exc).__name__}"}
    # Field 2 (comm) may contain spaces; the fields after it start past its ')'.
    # Field 22 (starttime, in clock ticks since boot) is index 19 of those.
    start_ticks = int(stat[stat.rindex(b")") + 2:].split()[19])
    uptime = (_time.clock_gettime(_time.CLOCK_BOOTTIME)
              - start_ticks / _os.sysconf("SC_CLK_TCK"))
    started = _datetime.fromtimestamp(_time.time() - uptime, _timezone.utc)
    return {"started_at": started.isoformat(timespec="milliseconds"),
            "uptime_seconds": round(uptime, 3), "reason": None}


# The threads /health's own BLOCKING reads run on: reading and sha256-ing the
# gate export, and reading /proc/self/stat. Two is plenty for two sub-millisecond
# reads; a probe that arrives while both are busy waits for one of them to
# finish, never for a State request. Built lazily under a lock so importing this
# module (every test does) starts no thread.
_HEALTH_PROBE_THREADS = 2
_HEALTH_PROBE: _ThreadPoolExecutor | None = None
_HEALTH_PROBE_LOCK = _threading.Lock()


def _health_probe_pool() -> _ThreadPoolExecutor:
    """The dedicated executor /health hands its blocking work to.

    A pool of its OWN, deliberately not `run_in_threadpool`: taking a slot of
    anyio's default limiter is exactly what used to put the probe behind State
    traffic. The executor is module-level and shared, because a per-request one
    would make every probe pay a thread creation to do 2 ms of reading.
    """
    global _HEALTH_PROBE
    with _HEALTH_PROBE_LOCK:
        if _HEALTH_PROBE is None:
            _HEALTH_PROBE = _ThreadPoolExecutor(
                max_workers=_HEALTH_PROBE_THREADS,
                thread_name_prefix="health-probe")
        return _HEALTH_PROBE


def health_check() -> dict:
    """系统探针 — the /health answer, read fresh on every call.

    `gate_harness` reports the harness copy inside the backend container running
    right now (see GATE_EXPORT_HARNESS), read fresh on every request. It is NOT
    the harness a gate imports: a normal gate imports the one in its binding's
    `source` directory, and an authored repository gate imports the export inside
    the image its tag names at gate time. An absent export is declared in that
    field; the probe still answers 200 with `status` and `engine` unchanged.

    `process` reports when the serving process started and how long it has been
    up (see _process_age), read fresh on every request. It is the age of this
    process, not of the container: a restart by Docker's restart policy resets it,
    and /health cannot count the restarts that came before.

    `source_identity` reports the source this process actually IMPORTED - the
    checkout root, a digest over the modules' files captured at import, and when
    (see core.source_identity, which owns the covered file list and the digest
    rule). It is deliberately NOT read fresh: that is what makes it a witness.
    `gate_harness` follows the disk and therefore moves at a merge; this field
    does not move until the process restarts, so a reader compares the TWO to
    tell "the bytes on disk changed" from "the loaded code changed", and compares
    it with a digest recomputed from the commit it meant to deploy.

    SYNCHRONOUS, and it BLOCKS: reading and sha256-ing the export and reading
    /proc/self/stat are short but they are still disk and syscall work. That is
    why the ROUTE (`health_probe`, below) never calls it on the event loop and
    never on a shared anyio slot - it hands it to the probe pool. This function
    keeps its name because a caller with no event loop can still ask it directly
    (tests/unit/test_task_log_stream_is_gated.py does). Nothing below is cached:
    the gate digest follows the file's CURRENT bytes and the uptime is THIS
    process's, so both fields still track their live sources. `source_identity`
    is the exception ON PURPOSE: it is the value captured at import, handed over
    as-is (a fresh dict and a fresh `files` list, so neither the captured record
    nor a later answer can be edited by a caller), because a field re-read per
    request cannot witness what was loaded.
    """
    return {"status": "ok", "engine": "DPE SOTA v3.0",
            "gate_harness": _gate_harness_identity(),
            "process": _process_age(),
            "source_identity": {**_SOURCE_IDENTITY,
                                "files": list(_SOURCE_IDENTITY["files"])}}


@app.get("/health")
async def health_probe():
    """Answer the liveness probe OFF the shared anyio worker pool.

    A plain `def` endpoint is serviced by `run_in_threadpool` on anyio's DEFAULT
    limiter - the same 40 slots that `POST /api/state/query/{action}` and its
    per-request `get_service` dependency take a slot from - so the probe queued
    behind State reads. Measured on the live backend (2026-11-16), one wave of 58
    concurrent `get_attempt` calls at concurrency 40 gave /health p95 53.1 ms and
    max 1171.1 ms while State p50 was 1539.8 ms. A liveness probe whose latency
    is a function of unrelated read traffic cannot answer the question it exists
    to answer.

    The fix is placement, not behaviour: the loop receives this request, and the
    blocking payload (`health_check`) runs on a small executor of its own - not
    on a shared slot and not on the loop (a blocked loop costs every request;
    see api/repo_routers.py for what that looked like).
    """
    return await _asyncio.get_running_loop().run_in_executor(
        _health_probe_pool(), health_check)


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

    Two ordering rules, both load-bearing on a single-core loop:
    - capacity is checked BEFORE identity: at the cap, EventSource retries
      every few seconds per tab, and each refused retry was paying a JWT
      verify for a stream that yields one comment line and closes;
    - the identity resolve runs in the threadpool: verification is CPU RSA
      plus, on an unknown `kid`, a blocking JWKS fetch with a 3s timeout —
      on the loop that stalls every open stream and the scheduler with it.
    """
    if stream_manager.at_capacity():
        # Refuse HERE, before paying for identity. Skipping only the identity
        # resolve and letting the generator decide later re-created the race
        # this ordering exists to prevent: a slot freeing between the two
        # checks admitted a signed-in operator as who=None — permanently
        # mislabeled anonymous for the stream's whole life. One refusal
        # decision, made once. (The generator keeps its own check as a belt.)
        # The generator's own refusal branch carried the only "refused" log
        # line; short-circuiting before it must not turn a bulk-open attack
        # into a silent one.
        import logging
        logging.getLogger("aitelier.sse").warning(
            "SSE connection refused at handler: at capacity")
        async def _refuse():
            yield ": at capacity\n\n"
        return StreamingResponse(_refuse(), media_type="text/event-stream")
    who, read_private = None, False
    try:
        from starlette.concurrency import run_in_threadpool
        from api.auth import creator_email
        from api.authz import may_read_private
        def subscriber_identity():
            # Presence attribution is not private-read authority: a verified
            # nonwriter still receives only the public progress projection.
            return creator_email(request), may_read_private(request)
        who, read_private = await run_in_threadpool(subscriber_identity)
    except Exception:
        pass  # Identity failure never grants private stream contents.
    return StreamingResponse(
        stream_manager.event_generator("__global__", who=who,
                                       read_private=read_private),
        media_type="text/event-stream",
    )


@app.get("/api/connections")
async def list_connections(request: Request):
    """Who is watching right now — the live SSE connection table.

    Split visibility: the COUNTS are public (they are also broadcast to every
    tab as `presence` events), but the per-connection detail carries verified
    emails, and "is the operator at the dashboard right now" is exactly the
    working-hours intelligence the reflog leak taught us not to hand out. So
    the detail list is included only for a caller the write gate would let
    write.

    `async` so the snapshot itself runs on the loop thread that owns the
    connection table; the authz check (which may verify a JWT) goes to the
    threadpool.
    """
    from api import authz
    from starlette.concurrency import run_in_threadpool
    body = dict(stream_manager.presence_counts())
    if await run_in_threadpool(authz.request_can_write, request):
        body["viewers"] = stream_manager.connection_snapshot()
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
# worked, and the URL in the docs did not. Both spellings are registered so neither
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
