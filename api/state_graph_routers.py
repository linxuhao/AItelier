"""State DAG HTTP endpoints. Reads of the graph are public; the notebooks are not."""
from fastapi import Depends, Request

from api.authz import require_reader, require_writer
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
from core import cf_access
from core.state_service import StateService

from api.state_http import (create_state_router, install_state_prefix_gate,
                            prefix_verdict_dependency)


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


# The app-wide half of the prefix verdict. A route the state router owns is left
# to that router's guard — recognised by the guard's presence in the route's own
# dependency tree, not by an attribute its endpoint could set. Everything else
# under the prefix takes the reader verdict.
state_prefix_verdict = prefix_verdict_dependency(require_reader)


def install_prefix_gate(app) -> None:
    """The middleware half, for routes a dependency cannot reach."""
    install_state_prefix_gate(app, require_reader)
