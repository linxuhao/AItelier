"""No route under `/api/state` escapes the verdict — on the apps the PRODUCT builds.

The previous round's guard was green while the deployed app was open. Deleting
`dependencies=[Depends(state_prefix_verdict)]` from `api/main.py` left the suite
at 245 passed, because every test in it built its own `FastAPI()` and measured
that. So this file never builds an app to measure. It imports
`api.main.app` and calls `api.state_only.create_app`, and asserts against those
two objects.

The assembly points are not a number written here either: the repository is
searched for them, so a third one added later fails this file instead of
quietly inheriting nothing.
"""
from __future__ import annotations

import pathlib
import re
import tempfile
from contextlib import contextmanager

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.routing import Route

from api import authz
from api import main as api_main
from api import state_only
from api.state_http import (STATE_ROUTE_DECLARATION, StatePrefixGate, is_state_prefix,
                            route_carries_prefix_verdict, route_carries_state_verdict,
                            route_contexts)

SECRET = "NOTEBOOK SECRET-BODY"
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


# --- how many places assemble an app carrying `/api/state` routes ------------

def _assembly_points() -> set[str]:
    """Every non-test module that mounts a state router onto an application."""
    found = set()
    for path in sorted(REPOSITORY_ROOT.rglob("*.py")):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        if relative.startswith(("tests/", "web/", ".git")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if re.search(r"include_router\(\s*(create_state_router|state_graph_router)", line):
                found.add(relative)
    return found


def test_the_repository_assembles_exactly_two_apps_under_the_prefix():
    """Counted from the source, not remembered. `api/state_only.py` was the
    second one r1's criteria never reached; a third would fail here."""
    assert _assembly_points() == {"api/main.py", "api/state_only.py"}, _assembly_points()


# --- the two product apps ----------------------------------------------------

STATE_ONLY_TOKEN = "state-only-token-for-tests-at-least-32-bytes-long"
STATE_ONLY_DIRECTORY = tempfile.mkdtemp(prefix="state-only-prefix-test-")


def _state_only_app():
    database = pathlib.Path(STATE_ONLY_DIRECTORY) / f"state-{len(pytest.__name__)}.sqlite"
    return state_only.create_app(str(database), STATE_ONLY_TOKEN, with_mcp=False)


def _product_apps():
    return [("api/main.py", api_main.app), ("api/state_only.py", _state_only_app())]


PRODUCT_APPS = _product_apps()


@pytest.mark.parametrize("name,app", PRODUCT_APPS, ids=[n for n, _ in PRODUCT_APPS])
def test_every_route_under_the_prefix_carries_a_verdict(name, app):
    """The property `dependencies=[Depends(state_prefix_verdict)]` buys.

    Delete that argument at either assembly point and this goes red naming the
    routes that lost their verdict - which is exactly what mutation N12 did to
    `api/main.py` while the whole suite stayed green.
    """
    under_the_prefix = [route for route in route_contexts(app)
                        if getattr(route, "path", None) and is_state_prefix(route.path)]
    assert under_the_prefix, (name, "no route under the prefix was found at all")
    naked = [route.path for route in under_the_prefix
             if not route_carries_state_verdict(route)]
    assert naked == [], (name, naked)
    # And specifically the APP-WIDE verdict, not merely the state router's own
    # guard: measuring the latter would pass whether or not the app installed
    # the former, which is the vacuous form of this assertion.
    without_the_prefix_verdict = [route.path for route in under_the_prefix
                                  if not route_carries_prefix_verdict(route)]
    assert without_the_prefix_verdict == [], (name, without_the_prefix_verdict)


@pytest.mark.parametrize("name,app", PRODUCT_APPS, ids=[n for n, _ in PRODUCT_APPS])
def test_the_prefix_gate_middleware_is_installed(name, app):
    """The property `install_state_prefix_gate` buys: the layer that reaches a
    mounted sub-application or a bare Starlette route, neither of which has a
    dependency tree for the dependency half to be part of.

    And OUTERMOST, which both assembly points say they install it as. In
    `api/state_only.py` that ordering is load-bearing in the other direction
    too: the bearer middleware sits inside it, so an unauthenticated request
    still reaches 401 rather than being answered by the verdict.
    """
    installed = [middleware.cls for middleware in app.user_middleware]
    assert StatePrefixGate in installed, (name, installed)
    assert installed[0] is StatePrefixGate, (name, installed)


# --- the three shapes that were still 200 and still leaking ------------------

@pytest.fixture
def gated(monkeypatch):
    """Anonymous visitor against the real verdict: the gate is armed, nothing
    verifies, and test mode is off."""
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    monkeypatch.setattr(api_main.app.state, "_test_mode", False, raising=False)


@contextmanager
def _temporarily(app, install):
    """Install extra routes on the PRODUCT app and take them off again.

    They go to the FRONT of the route list. `api/main.py` ends with
    `app.mount("/", StaticFiles(...))`, which matches every path, so a route
    appended after it is never reached - a request to it 404s, and a guard test
    written that way would read the 404 as a refusal and pass over a route
    that was never there.
    """
    before = len(app.router.routes)
    added = []
    try:
        install(app)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = added
        _mark_changed(app)
        yield
    finally:
        for route in added:
            if route in app.router.routes:
                app.router.routes.remove(route)
        _mark_changed(app)


def _mark_changed(app):
    marker = getattr(app.router, "_mark_routes_changed", None)
    if marker is not None:
        marker()


def _leaking_subapplication(app):
    sub = FastAPI()

    @sub.get("/notebook")
    def notebook():
        return {"notebook": SECRET}

    app.mount("/api/state/sub", sub)


def _bare_starlette_route(app):
    async def leak(request):
        return JSONResponse({"notebook": SECRET})

    app.router.routes.append(Route("/api/state/bare", leak, methods=["GET"]))


def _route_on_another_router_claiming_the_declaration(app):
    other = APIRouter()

    def impostor():
        return {"notebook": SECRET}

    # r1's prefix verdict exempted any endpoint carrying this attribute, so an
    # endpoint on a router the state guard never sees could award itself the
    # exemption. The attribute is set here on purpose.
    setattr(impostor, STATE_ROUTE_DECLARATION, ("read", "get_graph"))
    other.get("/api/state/impostor")(impostor)
    app.include_router(other)


def _plain_app_route(app):
    @app.get("/api/state/leak/{project_id}")
    def leak(project_id: str):
        return {"notebook": SECRET}


ESCAPES = [
    ("mounted_subapplication", _leaking_subapplication, "/api/state/sub/notebook"),
    ("bare_starlette_route", _bare_starlette_route, "/api/state/bare"),
    ("another_router_with_the_declaration", _route_on_another_router_claiming_the_declaration,
     "/api/state/impostor"),
    ("plain_app_route", _plain_app_route, "/api/state/leak/p"),
]


@pytest.mark.parametrize("name,install,path", ESCAPES, ids=[e[0] for e in ESCAPES])
def test_a_route_under_the_prefix_is_refused_to_an_anonymous_caller(name, install, path, gated):
    """Pole one: the real `api/main.py` app, the real `require_reader`, no
    credentials. All four of these answered 200 with the body at some point in
    this card's history."""
    with _temporarily(api_main.app, install):
        with TestClient(api_main.app) as client:
            response = client.get(path)
    assert response.status_code == 403, (name, response.status_code, response.text[:200])
    assert SECRET not in response.text, name


@pytest.mark.parametrize("name,install,path", ESCAPES, ids=[e[0] for e in ESCAPES])
def test_the_same_route_is_served_once_the_verdict_allows(name, install, path, gated,
                                                          monkeypatch):
    """Pole two: same app, same route, an authorized caller. The refusal above
    is the verdict deciding, not the gate refusing everything it does not
    recognise."""
    monkeypatch.setattr(api_main.app.state, "_test_mode", True, raising=False)
    with _temporarily(api_main.app, install):
        with TestClient(api_main.app) as client:
            response = client.get(path)
    assert response.status_code == 200, (name, response.status_code, response.text[:200])
    assert SECRET in response.text, name


@contextmanager
def _without_the_prefix_gate(app):
    """Take the middleware half off, so the dependency half answers alone."""
    kept = list(app.user_middleware)
    app.user_middleware[:] = [m for m in kept if m.cls is not StatePrefixGate]
    app.middleware_stack = None
    try:
        yield
    finally:
        app.user_middleware[:] = kept
        app.middleware_stack = None


@pytest.mark.parametrize(
    "name,install,path",
    [entry for entry in ESCAPES if entry[0] in
     {"another_router_with_the_declaration", "plain_app_route"}],
    ids=["another_router_with_the_declaration", "plain_app_route"])
def test_the_dependency_half_refuses_on_its_own(name, install, path, gated):
    """What `dependencies=[Depends(state_prefix_verdict)]` is FOR.

    With the middleware taken off, a route under the prefix that the state
    router does not own has nothing but this dependency in front of it. Delete
    the argument from `api/main.py` - mutation N12 - and this goes red. Both of
    these routes leaked the notebook at some point in this card's history, and
    the impostor one did so by setting the declaration attribute on its own
    endpoint, which the previous prefix verdict read as an exemption.
    """
    with _without_the_prefix_gate(api_main.app):
        with _temporarily(api_main.app, install):
            with TestClient(api_main.app) as client:
                refused = client.get(path)
        with _temporarily(api_main.app, install):
            api_main.app.state._test_mode = True
            try:
                with TestClient(api_main.app) as client:
                    allowed = client.get(path)
            finally:
                api_main.app.state._test_mode = False
    assert refused.status_code == 403, (name, refused.status_code, refused.text[:200])
    assert SECRET not in refused.text, name
    assert allowed.status_code == 200, (name, allowed.status_code, allowed.text[:200])
    assert SECRET in allowed.text, name


def test_a_mounted_write_never_reaches_its_handler(gated):
    """The middleware half refuses BEFORE the sub-application runs, so this is
    not a response filter: the handler's side effect must not happen."""
    ran = []
    sub = FastAPI()

    @sub.post("/write")
    def write():
        ran.append("the mounted handler ran")
        return {"ok": True}

    with _temporarily(api_main.app, lambda app: app.mount("/api/state/sub", sub)):
        with TestClient(api_main.app) as client:
            response = client.post("/api/state/sub/write", json={})
    assert response.status_code == 403, (response.status_code, response.text[:200])
    assert ran == [], ran


# --- what r1 already bought, which must not regress --------------------------

def test_the_state_routers_own_routes_still_answer(gated):
    """A public read stays public and a private read stays refused: the prefix
    verdict stands down for the routes the state router's own guard judges,
    instead of refusing them all."""
    with TestClient(api_main.app) as client:
        public = client.get("/api/state/projects")
        private = client.get("/api/state/projects/p/driver-note")
    assert public.status_code == 200, public.status_code
    assert private.status_code == 403, private.status_code
    assert SECRET not in private.text


@pytest.mark.parametrize("path", ["/health", "/api/statistics-not-state"])
def test_a_path_outside_the_prefix_is_untouched(path, gated):
    """The prefix boundary: `/api/statistics...` is not `/api/state/...`."""
    with _temporarily(api_main.app,
                      lambda app: app.get("/api/statistics-not-state")(lambda: {"ok": True})):
        with TestClient(api_main.app) as client:
            response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)


@pytest.mark.parametrize("method", ["head", "options"])
def test_head_and_options_under_the_prefix_are_refused(method, gated):
    """A method the route does not declare must not become a way past the
    verdict."""
    with _temporarily(api_main.app, _plain_app_route):
        with TestClient(api_main.app) as client:
            response = getattr(client, method)("/api/state/leak/p")
    assert response.status_code in (403, 405), (method, response.status_code)


@pytest.mark.parametrize("path", ["/api/state/leak/p/", "/api/state/", "/api/state"],
                         ids=["trailing_slash", "bare_prefix_slash", "bare_prefix"])
def test_a_trailing_slash_is_not_a_way_past_the_verdict(path, gated):
    """Followed redirects included: whatever the path normalises to, the body
    does not come out and the answer is not a 200."""
    with _temporarily(api_main.app, _plain_app_route):
        with TestClient(api_main.app) as client:
            response = client.get(path, follow_redirects=True)
    assert response.status_code != 200, (path, response.status_code)
    assert SECRET not in response.text, path


def test_the_bare_prefix_and_a_trailing_slash_are_both_under_the_guard():
    assert is_state_prefix("/api/state")
    assert is_state_prefix("/api/state/")
    assert is_state_prefix("/api/state/projects")
    assert not is_state_prefix("/api/statistics")
    assert not is_state_prefix("/api/statecraft/x")


# --- the state-only deployment ------------------------------------------------

def test_state_only_refuses_a_mounted_route_without_the_bearer_token():
    """`api/state_only.py:90` embeds the same router. Its own verdict allows
    once the bearer middleware has passed the request, so the pole that moves
    here is the token."""
    app = _state_only_app()
    sub = FastAPI()

    @sub.get("/notebook")
    def notebook():
        return {"notebook": SECRET}

    app.mount("/api/state/sub", sub)
    with TestClient(app) as client:
        anonymous = client.get("/api/state/sub/notebook")
        authorized = client.get("/api/state/sub/notebook",
                                headers={"Authorization": f"Bearer {STATE_ONLY_TOKEN}"})
    assert anonymous.status_code == 401, anonymous.status_code
    assert SECRET not in anonymous.text
    assert authorized.status_code == 200, authorized.status_code


def test_state_only_applies_its_own_verdict_under_the_prefix():
    """And when that verdict refuses, the mounted route is refused too - both
    halves of the prefix gate are wired to the verdict this deployment named on
    `app.state.state_verdict`, not to a verdict it only claims to have."""
    app = _state_only_app()
    sub = FastAPI()

    @sub.get("/notebook")
    def notebook():
        return {"notebook": SECRET}

    app.mount("/api/state/sub", sub)

    def refuse(request: Request):
        raise HTTPException(403, "state-only refuses")

    app.dependency_overrides[app.state.state_verdict] = refuse
    with TestClient(app) as client:
        response = client.get("/api/state/sub/notebook",
                              headers={"Authorization": f"Bearer {STATE_ONLY_TOKEN}"})
    assert response.status_code == 403, (response.status_code, response.text[:200])
    assert SECRET not in response.text


def test_a_route_added_to_the_product_app_after_startup_is_still_judged(gated):
    """Routes are read at request time, so one added after the app booted is
    covered by the same verdict."""
    with TestClient(api_main.app) as client:
        with _temporarily(api_main.app, _plain_app_route):
            response = client.get("/api/state/leak/p")
    assert response.status_code == 403, response.status_code
    assert SECRET not in response.text
