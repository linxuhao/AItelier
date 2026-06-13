# web_api/main.py
# Web GUI FastAPI application — behind Cloudflare Access.
# Thin entry point: imports unified routers from api/ and overrides auth dependency.
#
# Modes (via AITELIER_MODE env var):
#   demo   — shared scheduler, all users can read all projects
#   normal — per-user schedulers, strict multi-tenant isolation

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from api.routers import router as tasks_router
from api.project_routers import router as projects_router
from api.settings_routers import router as settings_router
from api.meta_routers import router as meta_router
from api.auth import get_optional_user
from web_api.auth import get_current_user
from core.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    mode = os.getenv("AITELIER_MODE", "normal")
    app.state.mode = mode

    if mode == "demo":
        app.state.scheduler = start_scheduler(demo=True)
        print(f"Web API started in DEMO mode (shared scheduler).")
    else:
        from web_api.scheduler_manager import UserSchedulerManager
        app.state.scheduler_manager = UserSchedulerManager()
        # Global reaper: runs every 5 minutes to clean up idle schedulers
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        reaper = AsyncIOScheduler()

        def _reap():
            app.state.scheduler_manager.reap_inactive()

        reaper.add_job(_reap, "interval", seconds=300)
        reaper.start()
        app.state._reaper = reaper
        print(f"Web API started in NORMAL mode (per-user schedulers).")

    yield

    # Shutdown
    if mode == "demo":
        app.state.scheduler.shutdown(wait=False)
    else:
        app.state.scheduler_manager.shutdown_all()
        app.state._reaper.shutdown(wait=False)


app = FastAPI(
    title="AItelier Web API",
    description="Multi-tenant web API behind Cloudflare Access",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Wire auth: override optional dependency with real Cloudflare Access auth ──
app.dependency_overrides[get_optional_user] = get_current_user

# Startup assertion: fail fast if auth override is missing
assert get_optional_user in app.dependency_overrides, (
    "FATAL: auth dependency override missing — all endpoints would be unauthenticated!"
)

# Mount unified routers from api/
app.include_router(tasks_router)
app.include_router(projects_router)
app.include_router(settings_router)
app.include_router(meta_router)


@app.get("/health")
def health_check(request: Request):
    return {"status": "ok", "engine": "DPE SOTA v3.0", "mode": getattr(request.app.state, "mode", "normal")}
