# web_api/auth.py
# Cloudflare Access authentication for the web API.
# No localhost fallback — web API is always behind Cloudflare.

import time

from fastapi import Depends, Header, HTTPException, Request
from api.auth import CurrentUser
from api.dependencies import get_db_manager
from core.db_manager import DBManager


def get_current_user(
    request: Request,
    cf_access_user_email: str | None = Header(None, alias="Cf-Access-User-Email"),
    x_user_lang: str | None = Header(None, alias="X-User-Lang"),
    db: DBManager = Depends(get_db_manager),
) -> CurrentUser:
    """
    Resolve the current user from Cloudflare Access header.

    - Cf-Access-User-Email header present → authenticated web user.
    - Missing → 401 Unauthorized.
    - X-User-Lang header: on first visit (no stored lang), auto-sets it
      from the header. On subsequent visits the stored lang takes precedence
      (user must call the settings endpoint to change it).

    In normal mode, also activates the user's per-user scheduler.
    """
    if not cf_access_user_email:
        raise HTTPException(status_code=401, detail="Authentication required")

    email = cf_access_user_email.strip().lower()

    # Upsert user row
    now_epoch = int(time.time())
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO users (email, display_name, source, last_seen_at)
               VALUES (?, ?, 'cloudflare', ?)
               ON CONFLICT(email) DO UPDATE SET last_seen_at = ?""",
            (email, email.split("@")[0], now_epoch, now_epoch),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    # Resolve language: stored lang takes precedence; on first visit,
    # auto-set from the browser's X-User-Lang header.
    user_lang = row["lang"] if row and row["lang"] else None
    if not user_lang and x_user_lang:
        user_lang = x_user_lang.strip()[:10]  # sanity cap
        db.set_user_lang(email, user_lang)

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
        lang=user_lang,
    )
