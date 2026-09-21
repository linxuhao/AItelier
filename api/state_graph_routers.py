"""State DAG HTTP endpoints. Reads of the graph are public; the notebooks are not."""
from fastapi import Depends, Request

from api.authz import require_reader, require_writer
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core import cf_access
from core.state_service import StateService

from api import state_http
from api.state_http import STATE_ROUTE_DECLARATION, create_state_router


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


# Two verdicts, one identity check: `require_writer` for writes and for any read
# that stayed private, `require_reader` for a private read so the refusal talks
# about reading. Which reads are public is NOT decided per route here — the route
# declares the action it serves and one router-wide guard asks
# `core.state_commands.read_visibility`, which fails CLOSED, so a read added later
# is private until someone opens it.
router = create_state_router(get_service, require_writer, require_reader)


async def state_prefix_verdict(request: Request) -> None:
    """A verdict for EVERY route under `/api/state`, whoever owns it.

    The state router's guard covers the routes that router owns. A route with
    the same prefix mounted on the APP itself belongs to a different router
    and used to be covered by nothing: the write_gate middleware exempts the
    whole prefix, and the router guard never saw the route. This dependency is
    attached app-wide, so every request under the prefix reaches it: a route
    the state router owns carries the declaration and is left to that guard;
    anything else under the prefix is treated as a private read and takes the
    reader verdict. The default is a verdict, not an exemption.
    """
    path = request.url.path
    if path != "/api/state" and not path.startswith("/api/state/"):
        return
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    if getattr(endpoint, STATE_ROUTE_DECLARATION, None) is not None:
        return
    await state_http.apply_verdict(require_reader, request)
