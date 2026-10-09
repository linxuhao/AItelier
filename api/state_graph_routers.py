"""State DAG HTTP endpoints. Reads of the graph and the driver guide are public, and so are the driver's working notes for a project that has been opened; the director mailbox and the event plumbing are not."""
from fastapi import Depends, Request

from api.authz import (may_read_private, request_actor, request_identity, require_reader,
                       require_writer)
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core.state_service import StateService

from api.state_http import create_state_router


def authenticated_actor(request) -> str:
    """Called only after transport authorization; never trusts an actor argument.

    Feature off (default): the Access email, else the shared
    `authorized-state-operator` - unchanged. Feature on
    (design/multi-driver-coop.md §3): `driver:<id>` or `owner:<email>`.
    """
    return request_actor(request)


def authenticated_driver_id(request) -> str | None:
    """The registered driver behind this request (feature on), else None."""
    identity = request_identity(request)
    return identity.driver_id if identity is not None and identity.kind == "driver" else None


def get_service(request: Request, db=Depends(get_db_manager), ws=Depends(get_workspace_manager)):
    from api.mcp_router import _start_driver
    # WHO this request is is recorded ONCE, from the raw credential and not from
    # any route declaration, and travels into the service so `execute` can consult
    # it. A route author who forges a guard cannot reopen a private project: the
    # project decision is taken before routing and independent of the guard.
    return StateService(db, ws, attach_driver=_start_driver, actor=authenticated_actor(request),
                        runtime_factory=lambda: (get_skillflow(), get_config_registry()),
                        project_read_trusted=may_read_private(request),
                        driver_id=authenticated_driver_id(request))


# Two verdicts, one identity check: `require_writer` for writes and for any read
# that stayed private, `require_reader` for a private read so the refusal talks
# about reading. Which reads are public is NOT decided per route here — the route
# declares the action it serves and one router-wide guard asks
# `core.state_commands.read_visibility`, which fails CLOSED, so a read added later
# is private until someone opens it.
router = create_state_router(get_service, require_writer, require_reader)
