# FIXED: api/admin_routers.py
# Admin-only REST endpoints (protected by require_writer).


from fastapi import APIRouter, Depends, HTTPException, Request

from api.authz import require_writer
from api.dependencies import get_db_manager
from core.db_manager import DBManager
# TEST_MARKER


router = APIRouter(prefix="/api/admin", tags=["Admin"])


@router.get("/logged-users")
def get_logged_users(
    limit: int = 50,
    db: DBManager = Depends(get_db_manager),
    _=Depends(require_writer),
):
    """Return logged users with tracking info. Writers only."""
    return db.list_logged_users(limit=limit)
# MARKER_A


@router.delete("/logged-users/{email:path}")
def delete_logged_user(
    email: str,
    db: DBManager = Depends(get_db_manager),
    _=Depends(require_writer),
):
    """Delete a tracked user by email. Writers only."""
    deleted = db.delete_user(email)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"User not found: {email}")
    return {"ok": True, "email": email}


@router.post("/deployment-runtime-observation", dependencies=[Depends(require_writer)])
def deployment_runtime_observation(request: Request):
    """Original runtime facts for the existing off-tunnel host CLI authority.

    This owner audit can record observed-lost accounting. It never starts a
    runtime, clears ownership, or grants deployment authorization.
    """
    import sys
    from api import authz
    # Off-tunnel admin authority: the env admin token (driver identity off) or an
    # active is_admin LAN driver such as owner-cli (on). See api/authz.py.
    if not authz.local_admin_authority(request):
        raise HTTPException(403, "Original runtime observation requires the local CLI admin authority")
    dependencies = sys.modules.get("api.dependencies")
    sf = getattr(dependencies, "_skillflow_instance", None)
    db = getattr(dependencies, "db_instance", None)
    if sf is None or db is None:
        raise HTTPException(503, "Already initialized live SkillFlow and DB are required")
    from core import deployment_quiescence as dq
    try:
        return dq.runtime_observation(skillflow=sf, db=db)
    except Exception as exc:
        raise HTTPException(503, f"Live runtime observation unavailable: {type(exc).__name__}") from exc
