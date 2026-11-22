"""State DAG HTTP endpoints. Reads of the graph and the driver guide are public, and so are the driver's working notes for a project that has been opened; the director mailbox and the event plumbing are not."""
from fastapi import Depends, Request

from api.authz import (identity_resolved_once, is_via_cloudflare, may_read_private, request_actor,
                       request_identity, require_reader, require_writer)
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core.state_commands import GROUP_TURN_ACTIONS
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


def authenticated_is_admin(request) -> bool:
    """Owner email or an is_admin driver, judged exactly like `require_admin`."""
    identity = request_identity(request) if request is not None else None
    return bool(identity is not None and identity.is_admin
                and (identity.kind == "owner" or not is_via_cloudflare(request)))


READ_AS_HEADER = "X-AItelier-Read-As"


def get_service(request: Request, db=Depends(get_db_manager), ws=Depends(get_workspace_manager)):
    from api.mcp_router import _start_driver
    # WHO this request is is recorded ONCE, from the raw credential and not from
    # any route declaration, and travels into the service so `execute` can consult
    # it. A route author who forges a guard cannot reopen a private project: the
    # project decision is taken before routing and independent of the guard.
    # Step 23 (design-steward 7.7.6): `X-AItelier-Read-As` (any value) makes THIS request
    # an anonymous reader, whoever its credential is - the steward's tools send it in a
    # group turn. It is judged per request (this dependency builds a new service every
    # time) and can only narrow: without a credential, in test mode or with the gate off
    # the request is still anonymous, and `execute` refuses every write
    # (public_view_read_only). The exceptions are the family memory pair and the family lists'
    # read and two writes (step 25 C4), which a trusted credential reaches with the header
    # only (core.state_groups.group_turn judges the recorded turn audience and the turn the
    # request names); that service serves nothing else (core.state_commands.execute).
    public_view = request.headers.get(READ_AS_HEADER) is not None
    # The identity (and the one driver-token lookup and `_touch` behind it) is
    # resolved once for every question below (api.authz.identity_resolved_once).
    with identity_resolved_once():
        trusted = may_read_private(request)
        if public_view:
            trusted = trusted and request.path_params.get("action") in GROUP_TURN_ACTIONS
        actor = authenticated_actor(request)
        driver_id = authenticated_driver_id(request)
        is_admin = authenticated_is_admin(request) and not public_view
    service = StateService(db, ws, attach_driver=_start_driver, actor=actor,
                           runtime_factory=lambda: (get_skillflow(), get_config_registry()),
                           project_read_trusted=trusted,
                           driver_id=driver_id,
                           is_admin=is_admin,
                           public_view=public_view)
    service.steward.driver_runner = _driver_runner(request)
    return service


def _driver_runner(request):
    """Start / stop a driver for an owner's chat approval (core.state_channels) with
    the entries the approve route uses (api.steward_routers.approve). A State command
    runs in a worker thread; the driver manager lives on the app's event loop, so the
    call is handed back to that loop (anyio.from_thread)."""
    def run(kind, project_id, actor):
        from functools import partial

        from anyio.from_thread import run as on_loop

        from api.dsh_driver_routers import manager
        runtime = manager(request)
        if kind == "start_driver":
            return on_loop(partial(runtime.start, project_id, provider="aitelier", model=None, actor=actor))
        return on_loop(runtime.stop, project_id)
    return run


# Two verdicts, one identity check: `require_writer` for writes and for any read
# that stayed private, `require_reader` for a private read so the refusal talks
# about reading. Which reads are public is NOT decided per route here — the route
# declares the action it serves and one router-wide guard asks
# `core.state_commands.read_visibility`, which fails CLOSED, so a read added later
# is private until someone opens it.
router = create_state_router(get_service, require_writer, require_reader)
