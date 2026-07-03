# api/settings_routers.py
# REST endpoints for scheduler settings.

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from core.db_manager import DBManager
from api.dependencies import get_db_manager
from api.auth import CurrentUser, get_optional_user

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class SchedulerSettingsResponse(BaseModel):
    scheduler_type: str  # "interval" or "cron"
    scheduler_interval: Optional[int] = None
    scheduler_cron: Optional[str] = None


class SchedulerUpdateRequest(BaseModel):
    scheduler_type: str
    scheduler_interval: Optional[int] = None
    scheduler_cron: Optional[str] = None


@router.get("/scheduler", response_model=SchedulerSettingsResponse)
def get_scheduler_settings(
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """Get current scheduler configuration."""
    settings = db.get_scheduler_settings()
    scheduler_type = settings.get("scheduler_type", "interval")
    interval = settings.get("scheduler_interval")
    cron = settings.get("scheduler_cron")

    # Defaults for interval mode
    if scheduler_type == "interval" and not interval:
        interval = "60"

    return SchedulerSettingsResponse(
        scheduler_type=scheduler_type,
        scheduler_interval=int(interval) if interval else None,
        scheduler_cron=cron if cron else None,
    )


@router.post("/scheduler", response_model=SchedulerSettingsResponse)
def update_scheduler_settings(
    request: SchedulerUpdateRequest,
    http_request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Update scheduler configuration and hot-reload the running scheduler."""
    if request.scheduler_type not in ("interval", "cron"):
        raise HTTPException(400, "scheduler_type must be 'interval' or 'cron'")

    if request.scheduler_type == "interval":
        if not request.scheduler_interval or request.scheduler_interval < 5:
            raise HTTPException(400, "scheduler_interval must be >= 5 seconds")
        db.set_scheduler_setting("scheduler_type", "interval")
        db.set_scheduler_setting("scheduler_interval", str(request.scheduler_interval))
    else:
        if not request.scheduler_cron:
            raise HTTPException(400, "scheduler_cron required for cron type")
        parts = request.scheduler_cron.strip().split()
        if len(parts) != 5:
            raise HTTPException(400, "cron expression must have 5 fields: minute hour day month weekday")
        db.set_scheduler_setting("scheduler_type", "cron")
        db.set_scheduler_setting("scheduler_cron", request.scheduler_cron.strip())

    # Hot-reload: reschedule without server restart
    try:
        from core.scheduler import reschedule_scheduler
        scheduler = getattr(http_request.app.state, "scheduler", None)
        if scheduler:
            reschedule_scheduler(scheduler)
    except Exception:
        pass  # Settings are saved; scheduler will pick them up on next restart

    return SchedulerSettingsResponse(
        scheduler_type=request.scheduler_type,
        scheduler_interval=request.scheduler_interval,
        scheduler_cron=request.scheduler_cron,
    )


# ── User language preference ──────────────────────────────────────────


class UserLanguageResponse(BaseModel):
    lang: str | None = None


class UserLanguageRequest(BaseModel):
    lang: str


@router.get("/user/language", response_model=UserLanguageResponse)
def get_user_language(
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Get the current user's language preference."""
    if not user:
        return UserLanguageResponse(lang=None)
    lang = db.get_user_lang(user.email)
    return UserLanguageResponse(lang=lang)


@router.post("/user/language", response_model=UserLanguageResponse)
def set_user_language(
    body: UserLanguageRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Set the current user's language preference.

    Open to ALL authenticated users (readers included) — this is a
    per-user display preference, not a project mutation.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    lang = body.lang.strip()[:10]
    db.set_user_lang(user.email, lang)
    return UserLanguageResponse(lang=lang)
