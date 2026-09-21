"""A declaration the reader cannot support is refused, and the table is walked whole.

Two things are pinned here.

The first is that an unreadable route REFUSES. `api/state_route_reader` says
"Empty is not an approval" and the caller used to skip the whole cross-check
when the reading was empty, so a route the reader could not read was the one
route nothing checked. The reading is now three-valued plus a failure, and the
failure denies.

The second is that the cross-check is walked over the WHOLE route table. The
previous version asserted `checked >= 15` and passed at 16 - with every write
route and every action-dispatch read outside the count, because those were the
ones the reader returned nothing for. The count is now the number of routes on
the router, read off the router.

What the guard does with a dependency SHAPE is not asserted in this file;
`tests/unit/test_verdict_matches_depends.py` compares that against FastAPI
directly, for dependencies nobody here enumerated.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api import authz, state_graph_routers as routes
from api.state_http import (SCHEMA_DECLARED_ACTION, STATE_ROUTE_DECLARATION,
                            create_state_router, declaration_agrees_with_source)
from api.state_route_reader import DISPATCH, LITERAL, NO_ACTION, UNREADABLE, read_route
from core.state_commands import execute, is_public_read
from core.state_database import StateDatabase
from core.state_service import StateService

SECRET = "NOTEBOOK SECRET-BODY"


@pytest.fixture
def service(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                     actor="guard-shape-test")
    s.create_project("p", "P")
    s.driver_notes.update("p", "permanent", SECRET, 0, "director")
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


def _allows(request: Request) -> None:
    return None


# --- the whole table, one row per route -------------------------------------

def _verdict_for(kind, action):
    if kind == "write":
        return "writer verdict"
    if kind == "director":
        return "writer verdict, except list_director_messages (private read)"
    if kind == "schema":
        return "private read" if not is_public_read(SCHEMA_DECLARED_ACTION) else "public"
    if action is None:
        return "read verdict derived per URL action from read_visibility"
    return "public" if is_public_read(action) else "private read"


def _table():
    rows = []
    for route in routes.router.routes:
        declaration = getattr(route.endpoint, STATE_ROUTE_DECLARATION, None)
        reading = read_route(route.endpoint)
        rows.append({
            "path": route.path,
            "methods": sorted(route.methods or []),
            "declaration": declaration,
            "reading": reading.kind,
            "read": sorted(reading.actions) if reading.actions else reading.parameter,
            "verdict": None if declaration is None else _verdict_for(*declaration),
            "agrees": (declaration is not None
                       and declaration_agrees_with_source(declaration, route.endpoint)),
        })
    return rows


def test_every_route_on_the_router_is_declared_read_and_cross_checked(capsys):
    """Route -> declaration -> what the independent reader read -> verdict, for
    EVERY route on the real router. The count is the router's own, so a route
    added later is in the table the moment it exists."""
    table = _table()
    with capsys.disabled():
        print()
        print(f"{'ROUTE':58} {'DECLARATION':40} {'READING':10} {'READ':34} VERDICT")
        for row in table:
            print(f"{row['path']:58} {str(row['declaration']):40} "
                  f"{row['reading']:10} {str(row['read']):34} {row['verdict']}")
        print(f"{len(table)} routes, "
              f"{sum(1 for r in table if r['reading'] == UNREADABLE)} unreadable, "
              f"{sum(1 for r in table if r['agrees'])} cross-checked")

    assert len(table) == len(routes.router.routes)
    undeclared = [row["path"] for row in table if row["declaration"] is None]
    assert undeclared == [], undeclared
    unreadable = [row["path"] for row in table if row["reading"] == UNREADABLE]
    assert unreadable == [], unreadable
    disagreeing = [(row["path"], row["declaration"], row["reading"], row["read"])
                   for row in table if not row["agrees"]]
    assert disagreeing == [], disagreeing


def test_the_action_dispatch_routes_are_read_not_skipped():
    """`/query/{action}`, `/commands/{action}` and `/director-messages/{action}`
    take their action from the URL. Under the old regex reader they were four of
    the twenty routes the reader returned NOTHING for, and the guard skipped the
    cross-check for exactly those. They are now read as a dispatch on the very
    parameter the guard resolves, so the two halves name the same thing."""
    dispatching = {row["path"]: row for row in _table() if row["declaration"][1] is None
                   and row["declaration"][0] != "schema"}
    assert set(dispatching) == {"/api/state/query/{action}",
                                "/api/state/commands/{action}",
                                "/api/state/director-messages/{action}"}, sorted(dispatching)
    for path, row in dispatching.items():
        assert row["reading"] == DISPATCH, (path, row)
        assert row["read"] == "action", (path, row)


def test_the_schema_route_is_read_as_executing_nothing():
    """`/schema` publishes the operation shapes and executes no state action.
    It says so (`kind == "schema"`), the reader confirms it, and it is judged as
    a private read of a name the owner wrote down."""
    row = next(r for r in _table() if r["path"] == "/api/state/schema")
    assert row["declaration"] == ("schema", SCHEMA_DECLARED_ACTION), row
    assert row["reading"] == NO_ACTION, row
    assert not is_public_read(SCHEMA_DECLARED_ACTION)


def test_a_literal_route_is_read_as_the_action_it_executes():
    row = next(r for r in _table() if r["path"] == "/api/state/projects/{project_id}")
    assert row["reading"] == LITERAL, row
    assert row["read"] == ["get_graph"], row


# --- an unreadable declaration refuses; a readable one serves ----------------

def _readable_note(project_id: str, service=Depends(routes.get_service)):
    return execute(service, "get_driver_note", {"project_id": project_id})


def _unreadable_note():
    """The same handler, compiled from a source file that does not exist.

    `inspect.getsource` cannot fetch it, so the independent reader has nothing
    to say about it - the case the old caller skipped the cross-check for.
    """
    source = ("def endpoint(project_id: str, service=Depends(get_service)):\n"
              "    return execute(service, 'get_driver_note', {'project_id': project_id})\n")
    namespace = {"Depends": Depends, "get_service": routes.get_service, "execute": execute}
    exec(compile(source, "<a file that does not exist>", "exec"), namespace)
    return namespace["endpoint"]


@pytest.mark.parametrize("readable", [True, False], ids=["readable", "unreadable"])
def test_a_route_the_reader_cannot_read_is_refused(service, gated, readable):
    """Both poles, one allowing verdict, one declaration, one handler body. The
    only difference is whether the reader can read the endpoint - and that
    alone decides whether the notebook goes out."""
    endpoint = _readable_note if readable else _unreadable_note()
    setattr(endpoint, STATE_ROUTE_DECLARATION, ("read", "get_driver_note"))
    router = _router(service, _allows)
    router.get("/projects/{project_id}/note-probe")(endpoint)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/note-probe")
    if readable:
        assert response.status_code == 200, response.status_code
        assert SECRET in response.text
    else:
        assert response.status_code == 403, (response.status_code, response.text[:200])
        assert SECRET not in response.text


def test_a_declaration_naming_an_action_the_endpoint_does_not_serve_is_refused(service, gated):
    """A private-reading handler DECLARED as the public `get_graph` used to
    answer 200 to an anonymous caller, and the declaration-derived test agreed
    with it."""
    def lying_note(project_id: str, service=Depends(routes.get_service)):
        return execute(service, "get_driver_note", {"project_id": project_id})

    setattr(lying_note, STATE_ROUTE_DECLARATION, ("read", "get_graph"))
    router = _router(service, authz.require_reader)
    router.get("/projects/{project_id}/lying-note")(lying_note)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/lying-note")
    assert response.status_code == 403, response.status_code
    assert SECRET not in response.text


def test_a_dispatch_declaration_the_reader_cannot_confirm_is_refused(service, gated):
    """Declaring the dispatch family is not enough either: the endpoint has to
    be read executing the URL parameter the guard resolves. One that executes
    something else - here a name of its own choosing - is refused."""
    def crooked_dispatch(action: str, service=Depends(routes.get_service)):
        chosen = "get_driver_note"
        return execute(service, chosen, {"project_id": "p"})

    setattr(crooked_dispatch, STATE_ROUTE_DECLARATION, ("read", None))
    router = _router(service, _allows)
    router.get("/crooked/{action}")(crooked_dispatch)
    with _client_for(router, service) as client:
        response = client.get("/api/state/crooked/get_graph")
    assert response.status_code == 403, (response.status_code, response.text[:200])
    assert SECRET not in response.text


def test_an_endpoint_that_declares_nothing_is_refused(service, gated):
    def undeclared(project_id: str, service=Depends(routes.get_service)):
        return execute(service, "get_driver_note", {"project_id": project_id})

    router = _router(service, authz.require_reader)
    router.get("/projects/{project_id}/undeclared")(undeclared)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/undeclared")
    assert response.status_code == 403, response.status_code
    assert SECRET not in response.text


# --- what the previous rounds bought, which must not regress ----------------

def test_the_real_reader_verdict_survives_becoming_async(service, gated):
    """The decisive case of r1: the repository's own `require_reader`, rewritten
    as an `async def`, still refuses an anonymous private read and still allows
    a public one."""

    async def require_reader_async(request: Request) -> None:
        authz.require_reader(request)

    router = _router(service, require_reader_async)
    with _client_for(router, service) as client:
        private = client.get("/api/state/projects/p/driver-note")
        public = client.get("/api/state/projects")
    assert private.status_code == 403, private.status_code
    assert SECRET not in private.text
    assert public.status_code == 200, public.status_code


def test_the_real_reader_verdict_survives_becoming_a_yield_dependency(service, gated):
    """The decisive case of r2: a `yield` dependency returned a generator that
    was discarded, so its body never ran - `200 | leaked: True` where
    `Depends(...)` refused."""

    def require_reader_yield(request: Request):
        authz.require_reader(request)
        yield None

    router = _router(service, require_reader_yield)
    with _client_for(router, service) as client:
        private = client.get("/api/state/projects/p/driver-note")
        public = client.get("/api/state/projects")
    assert private.status_code == 403, private.status_code
    assert SECRET not in private.text
    assert public.status_code == 200, public.status_code


def test_a_verdict_that_cannot_be_applied_is_a_403(service, gated):
    """The default is DENY, not hope."""
    for unusable in (object(), 42, None):
        router = _router(service, unusable)
        with _client_for(router, service) as client:
            response = client.get("/api/state/projects/p/driver-note")
            assert response.status_code == 403, (unusable, response.status_code)


def test_a_wrongly_declared_public_action_cannot_be_reclassified_by_the_route(service, gated):
    """The read table decides, not the route: declaring `/schema` a public
    action does not open it, because the declaration is cross-checked and the
    visibility comes from `read_visibility` either way."""
    router = _router(service, authz.require_reader)
    schema_route = next(r for r in router.routes if r.path.endswith("/schema"))
    setattr(schema_route.endpoint, STATE_ROUTE_DECLARATION, ("read", "frontier"))
    try:
        with _client_for(router, service) as client:
            response = client.get("/api/state/schema")
    finally:
        setattr(schema_route.endpoint, STATE_ROUTE_DECLARATION,
                ("schema", SCHEMA_DECLARED_ACTION))
    assert response.status_code == 403, (response.status_code, response.text[:200])


def test_a_refused_verdict_never_reaches_the_handler(service, gated):
    """The refusal is a refusal, not a filtered response."""
    ran = []

    def counting(project_id: str, service=Depends(routes.get_service)):
        ran.append(project_id)
        return execute(service, "get_driver_note", {"project_id": project_id})

    setattr(counting, STATE_ROUTE_DECLARATION, ("read", "get_driver_note"))
    router = _router(service, authz.require_reader)
    router.get("/projects/{project_id}/counting")(counting)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/counting")
    assert response.status_code == 403, response.status_code
    assert ran == [], ran


def test_raising_something_that_is_not_an_http_exception_still_refuses(service, gated):
    def explodes(request: Request) -> None:
        raise RuntimeError("the verdict blew up")

    router = _router(service, explodes)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/driver-note")
    assert response.status_code == 403, response.status_code


def test_an_http_exception_from_the_verdict_keeps_its_own_status(service, gated):
    def teapot(request: Request) -> None:
        raise HTTPException(418, "no tea for readers")

    router = _router(service, teapot)
    with _client_for(router, service) as client:
        response = client.get("/api/state/projects/p/driver-note")
    assert response.status_code == 418, response.status_code
