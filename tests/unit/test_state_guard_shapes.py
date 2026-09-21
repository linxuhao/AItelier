"""The guard must not depend on the shape of its dependency.

Three holes, each pinned here, each RED against the base this landed on:

1. The old `_verdict` called dependencies synchronously. An `async def`
   dependency produced a coroutine nobody awaited - no error, no 403, an open
   private read (`200 | dependency executed: False`).
2. The route declaration was the ONLY reader of which action a route serves.
   Declaring a private-reading handler as a public action opened the door and
   the declaration-derived test agreed with the wrong answer: all green.
3. A route mounted on the APP under `/api/state` was covered by nothing: the
   write_gate middleware exempts the whole prefix and the router guard only
   sees its own router's routes.

The default everywhere is DENY: a verdict that cannot be applied is a 403,
never a silent pass.
"""
from __future__ import annotations

import functools
from contextlib import contextmanager

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api import authz, state_graph_routers as routes
from api.state_http import STATE_ROUTE_DECLARATION, apply_verdict, create_state_router
from api.state_route_reader import served_actions
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService


@pytest.fixture
def service(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                     actor="guard-shape-test")
    s.create_project("p", "P")
    s.driver_notes.update("p", "permanent", "NOTEBOOK SECRET-BODY", 0, "director")
    return s


@pytest.fixture
def gated(monkeypatch):
    """Anonymous visitor: the gate is armed and nothing verifies."""
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin-token")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)


def _router(service, verdict):
    return create_state_router(lambda: service, authz.require_writer, verdict)


@contextmanager
def _client_for(router, service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[routes.get_service] = lambda: service
    with TestClient(app) as client:
        yield client


# --- the five shapes, both poles -------------------------------------------------

def _sync_deny(request: Request) -> None:
    raise HTTPException(403, "sync deny")


def _sync_allow(request: Request) -> None:
    return None


async def _async_deny(request: Request) -> None:
    raise HTTPException(403, "async deny")


async def _async_allow(request: Request) -> None:
    return None


class _CallableDeny:
    def __call__(self, request: Request) -> None:
        raise HTTPException(403, "callable deny")


class _AsyncCallableAllow:
    async def __call__(self, request: Request) -> None:
        return None


def _subdep_deny(request: Request, verdict=Depends(_sync_allow)) -> None:
    raise HTTPException(403, "subdep deny")


SHAPES = [
    ("sync_def", _sync_deny, _sync_allow),
    ("async_def", _async_deny, _async_allow),
    ("callable_object", _CallableDeny(), _AsyncCallableAllow()),
    ("functools_partial", functools.partial(_sync_deny), functools.partial(_sync_allow)),
    ("dependency_with_subdependency", _subdep_deny, _async_allow),
]


@pytest.mark.parametrize("name,deny,allow", SHAPES, ids=[s[0] for s in SHAPES])
def test_the_verdict_is_applied_whatever_the_dependency_shape(
        service, gated, name, deny, allow):
    """Private read: the denying shape refuses (403), the allowing shape lets
    through (200). The verdict was REALLY applied in both directions."""
    deny_router = _router(service, deny)
    with _client_for(deny_router, service) as client:
        response = client.get("/api/state/projects/p/driver-note")
        assert response.status_code == 403, (name, response.status_code)
        assert "SECRET-BODY" not in response.text
    allow_router = _router(service, allow)
    with _client_for(allow_router, service) as client:
        response = client.get("/api/state/projects/p/driver-note")
        assert response.status_code == 200, (name, response.status_code)


def test_a_verdict_that_cannot_be_applied_is_a_403(service, gated):
    """The default is DENY, not hope: a dependency that cannot be resolved or
    called refuses instead of silently passing."""
    for unusable in (object(), 42, None):
        router = _router(service, unusable)
        with _client_for(router, service) as client:
            response = client.get("/api/state/projects/p/driver-note")
            assert response.status_code == 403, (unusable, response.status_code)


def test_the_real_reader_verdict_survives_becoming_async(service, gated):
    """THE decisive case: the repository's own require_reader, rewritten as an
    `async def` (the most ordinary FastAPI shape), still refuses an anonymous
    private read - and still allows a public one."""

    async def require_reader_async(request: Request) -> None:
        authz.require_reader(request)

    router = _router(service, require_reader_async)
    with _client_for(router, service) as client:
        private = client.get("/api/state/projects/p/driver-note")
        public = client.get("/api/state/projects")
    assert private.status_code == 403, private.status_code
    assert "SECRET-BODY" not in private.text
    assert public.status_code == 200, public.status_code


# --- the declaration needs a reader that is not the declaration ------------------

def test_a_wrong_declaration_is_refused_and_the_suite_sees_it(service, gated):
    """A private-reading handler DECLARED as the public `get_graph` used to
    answer 200 to an anonymous caller, and the declaration-derived test agreed.
    The guard now cross-checks the declaration against the endpoint's own
    source and refuses the mismatch; the consistency test below goes red."""
    router = create_state_router(lambda: service, authz.require_writer,
                                 authz.require_reader)

    def lying_note(project_id: str, service=Depends(routes.get_service)):
        return execute(service, "get_driver_note", {"project_id": project_id})

    setattr(lying_note, STATE_ROUTE_DECLARATION, ("read", "get_graph"))
    router.get("/projects/{project_id}/lying-note")(lying_note)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/lying-note")
    assert response.status_code == 403, response.status_code
    assert "SECRET-BODY" not in response.text


def test_every_declared_action_is_the_action_the_endpoint_serves():
    """The suite-level reader: for every route on the real router, the declared
    action must appear among the actions the endpoint's source executes."""
    checked = 0
    for route in routes.router.routes:
        declaration = getattr(route.endpoint, STATE_ROUTE_DECLARATION, None)
        served = served_actions(route.endpoint)
        if declaration is not None and declaration[1] is not None and served:
            assert declaration[1] in served, (route.path, declaration[1], sorted(served))
            checked += 1
    assert checked >= 15, checked  # the cross-check must not pass vacuously


# --- no route under the prefix escapes the verdict --------------------------------

def test_an_app_mounted_route_under_the_prefix_still_gets_a_verdict(
        service, gated):
    """The hole: same prefix, mounted on the APP - the middleware exempted the
    whole `/api/state/` prefix and the router guard never saw the route, so it
    had NO guard at all. Now an app-wide dependency fails it closed."""
    app = FastAPI(dependencies=[Depends(routes.state_prefix_verdict)])
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: service

    @app.get("/api/state/leak/{project_id}")
    def leak(project_id: str):
        return {"notebook": "NOTEBOOK SECRET-BODY"}

    @app.get("/elsewhere")
    def elsewhere():
        return {"ok": True}

    with TestClient(app) as client:
        leak_response = client.get("/api/state/leak/p")
        owned = client.get("/api/state/projects/p")
        other = client.get("/elsewhere")
    assert leak_response.status_code == 403, leak_response.status_code
    assert "SECRET-BODY" not in leak_response.text
    assert owned.status_code == 200, owned.status_code
    assert other.status_code == 200, other.status_code
