# FIXED: api/admin_routers.py
# Admin-only REST endpoints (protected by require_writer).


from fastapi import APIRouter, Depends, HTTPException

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
