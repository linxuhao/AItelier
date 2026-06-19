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
from api.run_routers import router as run_router
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
                _adb = _sql.connect(_os.path.expanduser("~/.AItelier/aitelier.db"))
                row = _adb.execute(
                    "SELECT name FROM projects WHERE project_id = ?",
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
            _sdb = _sql.connect(_os.path.expanduser("~/.AItelier/skillflow.db"))
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

    def _resolve_run_info(data: dict, rid: str):
        """Ensure project_id from run if not in payload (thread-safe)."""
        pid = data.get("project_id")
        if pid:
            return
        if rid not in _pid_cache:
            try:
                import sqlite3 as _sql
                _sdb = _sql.connect(_os.path.expanduser("~/.AItelier/skillflow.db"))
                row = _sdb.execute(
                    "SELECT project_id FROM skillflow_runs WHERE id = ?",
                    (rid,),
                ).fetchone()
                _sdb.close()
                _pid_cache[rid] = row[0] if row else ""
            except Exception:
                _pid_cache[rid] = ""
        pid = _pid_cache.get(rid, "")
        if pid:
            data["project_id"] = pid

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
app.include_router(run_router)


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
