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
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from api.routers import router as tasks_router
from api.project_routers import router as projects_router
from api.settings_routers import router as settings_router
from api.meta_routers import router as meta_router
from api.agent_routers import router as agent_router
from api.sse_manager import stream_manager
from core.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动/关闭生命周期：初始化 skillflow NotificationBus → SSE 订阅 + 后台调度器"""
    import asyncio
    import json as _json
    from api.dependencies import get_skillflow

    loop = asyncio.get_running_loop()

    # Initialize skillflow (lazy singleton, registers DPE pipeline)
    sf = get_skillflow()
    app.state.skillflow = sf
    # Wire the main event loop so notifications from worker threads
    # (e.g. PipelineEngine in thread-pool executor) bridge to SSE.
    sf.notifications.set_event_loop(loop)

    # ── NotificationBus → SSE bridge (single event path) ──────────
    # Cache run_id → project_id lookups to avoid per-event DB queries.
    _pid_cache: dict[str, str] = {}
    _MAX_PID_CACHE = 2000

    async def _on_skillflow_event(notification):
        """Forward skillflow NotificationBus events to SSE."""
        payload = notification.payload
        data = {**payload, "type": notification.event_type}
        # Ensure project_id is present (resolve from run_id if needed)
        if not data.get("project_id"):
            rid = notification.run_id
            if rid:
                if rid not in _pid_cache:
                    if len(_pid_cache) >= _MAX_PID_CACHE:
                        _pid_cache.clear()
                    try:
                        row = sf._conn.execute(
                            "SELECT project_id FROM skillflow_runs WHERE id = ?",
                            (rid,),
                        ).fetchone()
                        _pid_cache[rid] = row["project_id"] if row else ""
                    except Exception:
                        _pid_cache[rid] = ""
                if _pid_cache.get(rid):
                    data["project_id"] = _pid_cache[rid]
        # Include metadata from notification if not already in payload
        if notification.step_id and "step_id" not in data:
            data["step_id"] = notification.step_id
        if notification.run_id and "run_id" not in data:
            data["run_id"] = notification.run_id
        payload_str = _json.dumps(data)
        await stream_manager.push_log("__global__", payload_str)
        await stream_manager.push_log("0", payload_str)

    sf.notifications.subscribe(_on_skillflow_event)

    # Recover any claimed steps left by a previous (crashed/killed) process.
    # Server is singleton — any claim at startup is definitively stale.
    from core.scheduler import recover_claims_on_startup
    recover_claims_on_startup()

    app.state.scheduler = start_scheduler()
    print("DPE APScheduler started. skillflow NotificationBus → SSE bridge active.")
    yield
    # Shutdown
    if hasattr(app.state, "scheduler") and app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(
    title="AItelier DPE Engine API",
    description="Deterministic Pipeline Engine (DPE) 控制面",
    version="1.0.0",
    lifespan=lifespan,
)

# 挂载路由
app.include_router(tasks_router)
app.include_router(projects_router)
app.include_router(settings_router)
app.include_router(meta_router)
app.include_router(agent_router)


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    """Reject requests from non-localhost clients."""
    if getattr(request.app.state, "_test_mode", False):
        return await call_next(request)
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="External access denied")
    return await call_next(request)


@app.get("/health")
def health_check():
    """系统探针"""
    return {"status": "ok", "engine": "DPE SOTA v3.0"}


@app.get("/api/events/stream")
async def stream_global_events():
    """
    Global SSE endpoint for CLI dashboard.
    Broadcasts all pipeline events (project + task) for real-time status updates.
    """
    return StreamingResponse(
        stream_manager.event_generator("__global__"),
        media_type="text/event-stream",
    )
