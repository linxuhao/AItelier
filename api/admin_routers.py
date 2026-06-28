# api/admin_routers.py
# Admin-only REST endpoints (protected by require_writer).

from fastapi import APIRouter, Depends

from api.authz import require_writer
from api.dependencies import get_db_manager
from core.db_manager import DBManager

router = APIRouter(prefix="/api/admin", tags=["Admin"])


@router.get("/logged-users")
def get_logged_users(
    limit: int = 50,
    db: DBManager = Depends(get_db_manager),
    _=Depends(require_writer),
):
    """Return logged users with tracking info. Writers only."""
    return db.list_logged_users(limit=limit)
