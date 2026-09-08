"""Authorized State DAG command/query endpoints; all project facts are private."""
from fastapi import Depends, Request

from api.authz import require_writer
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core import cf_access
from core.state_service import StateService

from api.state_http import create_state_router


def authenticated_actor(request) -> str:
    """Called only after transport authorization; never trusts an actor argument."""
    if request is not None:
        email = cf_access.email_from_request_headers(request.headers, getattr(request, "cookies", {}))
        if email:
            return email
    return "authorized-state-operator"


def get_service(request: Request, db=Depends(get_db_manager), ws=Depends(get_workspace_manager)):
    from api.mcp_router import _start_driver
    return StateService(db, ws, attach_driver=_start_driver, actor=authenticated_actor(request),
                        runtime_factory=lambda: (get_skillflow(), get_config_registry()))


router = create_state_router(get_service, require_writer)
