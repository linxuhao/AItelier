"""State DAG HTTP endpoints. Reads of the graph are public; the notebooks are not."""
from fastapi import Depends, Request

from api.authz import may_read_private, require_reader, require_writer
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
    # WHO this request is is recorded ONCE, from the raw credential and not from
    # any route declaration, and travels into the service so `execute` can consult
    # it. A route author who forges a guard cannot reopen a private project: the
    # project decision is taken before routing and independent of the guard.
    return StateService(db, ws, attach_driver=_start_driver, actor=authenticated_actor(request),
                        runtime_factory=lambda: (get_skillflow(), get_config_registry()),
                        project_read_trusted=may_read_private(request))


# Two verdicts, one identity check: `require_writer` for writes and for any read
# that stayed private, `require_reader` for a private read so the refusal talks
# about reading. Which reads are public is NOT decided per route here — the route
# declares the action it serves and one router-wide guard asks
# `core.state_commands.read_visibility`, which fails CLOSED, so a read added later
# is private until someone opens it.
router = create_state_router(get_service, require_writer, require_reader)
