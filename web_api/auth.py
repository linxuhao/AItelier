# web_api/auth.py
# Cloudflare Access authentication for the web API.
# No localhost fallback — web API is always behind Cloudflare.

from fastapi import Header, HTTPException, Request
from api.auth import CurrentUser
from api.dependencies import get_db_manager
from core.db_manager import DBManager


def get_current_user(
    request: Request,
    cf_access_user_email: str | None = Header(None, alias="Cf-Access-User-Email"),
) -> CurrentUser:
    """
    Resolve the current user from Cloudflare Access header.

    - Cf-Access-User-Email header present → authenticated web user.
    - Missing → 401 Unauthorized.

    In normal mode, also activates the user's per-user scheduler.
    """
    if not cf_access_user_email:
        raise HTTPException(status_code=401, detail="Authentication required")

    email = cf_access_user_email.strip().lower()
    db: DBManager = get_db_manager()

    # Upsert user row
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO users (email, display_name, source, last_seen_at)
               VALUES (?, ?, 'cloudflare', CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP""",
            (email, email.split("@")[0]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    # In normal mode: ensure user has a scheduler and mark activity
    mode = getattr(request.app.state, "mode", "normal")
    if mode == "normal":
        from web_api.scheduler_manager import UserSchedulerManager
        manager: UserSchedulerManager = request.app.state.scheduler_manager
        manager.touch(email)
        # Scheduler is created lazily by get_or_create, but APScheduler
        # needs a running event loop. Defer creation to the reaper/tick
        # if the user doesn't have one yet — touch() is enough to keep
        # an existing scheduler alive.

    return CurrentUser(
        email=row["email"],
        display_name=row["display_name"],
        source="cloudflare",
    )
