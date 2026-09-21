"""Nothing a route's author writes stands the state verdict down.

From r1 to r4 the "every route under the prefix carries a verdict" criterion was
bought with the same clause: a route that "has already been judged" makes both
the app-wide verdict and the prefix middleware return early. Each round kept the
clause and moved its CARRIER - r1 read an attribute off the endpoint
(`_state_route`), r4 read an attribute off a dependency
(`_is_state_router_guard`). Either way one `setattr` bought the exemption, and
the coverage test was green over the leaking route because it read the same
author-settable attribute the guard did.

This file is the observation point that was missing. It asks the question the
guard has to answer - "has this route already been judged?" - from the outside,
with a hostile route that says yes in every way a route's author can say it, on
the apps the PRODUCT builds, with the gate armed, anonymously, over a body that
carries recognisable private content.

Two things make it hard to leave behind:

* The set of attributes attacked is READ OUT OF THE GUARD MODULES' SOURCE, not
  written here. A mark added to `api/state_http.py` tomorrow is attacked
  tomorrow, by name, without anybody remembering this file exists.
* One corpus entry is not a name at all: it copies EVERY attribute the real
  router guard carries - including its `__name__`, `__qualname__` and
  `__module__` - onto an impostor. A stand-down that compares names, or any
  attribute whatsoever, hands that impostor the exemption. Only comparing the
  guard OBJECT survives it.
"""
from __future__ import annotations

import ast
import pathlib
import tempfile
from contextlib import contextmanager

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api import authz
from api import main as api_main
from api import state_graph_routers
from api import state_only
from api.state_http import STATE_ROUTE_DECLARATION

SECRET = "NOTEBOOK SECRET-BODY"
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every module that builds, installs or stands down a state verdict. A mark can
# only buy an exemption if one of these writes it down.
GUARD_MODULES = ("api/state_http.py", "api/state_graph_routers.py",
                 "api/state_only.py")


def author_settable_marks() -> list[str]:
    """Attribute names the guard modules name, read out of their source.

    Module-level assignments whose value is a string literal that is a valid
    identifier beginning with `_`: that is what an attribute name written down
    as a constant looks like, and it is how `_state_route`,
    `_is_state_verdict`, `_is_state_router_guard` and `_is_state_prefix_verdict`
    were all written. Derived, never listed here: a list here is exactly what
    let r4 move the carrier and keep the corpus passing.
    """
    found: set[str] = set()
    for relative in GUARD_MODULES:
        source = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if value.value.startswith("_") and value.value.isidentifier():
                found.add(value.value)
    return sorted(found)


def test_the_hostile_corpus_is_derived_and_not_empty():
    """The corpus cannot go vacuous without failing.

    If the derivation stops finding anything - a renamed constant, a moved
    module, a rewritten source - every parametrised case below would silently
    disappear and this file would pass over nothing. The declaration attribute
    is the floor because the guard genuinely reads it, so it can never be
    legitimately absent while the router still declares its routes.
    """
    marks = author_settable_marks()
    assert STATE_ROUTE_DECLARATION in marks, marks
    assert len(marks) >= 1, marks


def test_every_mark_the_source_names_is_carried_by_something_real():
    """The corpus attacks names that are really in use.

    Each derived name is carried either by a verdict object this process built
    or by a state route's endpoint. Stop setting one and this fails naming it,
    rather than leaving the corpus quietly attacking an attribute that no
    longer means anything to anybody - which is how a corpus keeps passing
    while the thing it was written for moves somewhere else.
    """
    carriers = [_the_real_router_guard(), state_graph_routers.state_prefix_verdict]
    carriers += [route.endpoint for route in state_graph_routers.router.routes]
    unclaimed = [mark for mark in author_settable_marks()
                 if not any(hasattr(carrier, mark) for carrier in carriers)]
    assert unclaimed == [], unclaimed


def _the_real_router_guard():
    """The guard object `create_state_router` built for the product's router."""
    dependencies = state_graph_routers.router.dependencies
    assert len(dependencies) == 1, dependencies
    return dependencies[0].dependency


def _every_attribute_of_the_real_guard() -> dict:
    real = _the_real_router_guard()
    attributes = dict(vars(real))
    for name in ("__name__", "__qualname__", "__module__", "__doc__"):
        value = getattr(real, name, None)
        if value is not None:
            attributes[name] = value
    return attributes


def _attribute_sets():
    """One entry per way a route's author can claim to have been judged."""
    sets = [(mark, {mark: True}) for mark in author_settable_marks()]
    sets.append(("every_attribute_of_the_real_guard",
                 _every_attribute_of_the_real_guard()))
    return sets


ATTRIBUTE_SETS = _attribute_sets()


def _stamp(target, attributes: dict):
    for name, value in attributes.items():
        try:
            setattr(target, name, value)
        except (AttributeError, TypeError):  # pragma: no cover - not a carrier
            pass
    return target


def _serving_the_secret():
    def forged():
        return {"notebook": SECRET}
    return forged


# --- the five places a claim can be written ----------------------------------

def _endpoint_attribute(app, path, attributes):
    endpoint = _stamp(_serving_the_secret(), attributes)
    app.get(path)(endpoint)


def _direct_dependency(app, path, attributes):
    def claim(request: Request) -> None:
        return None
    app.get(path, dependencies=[Depends(_stamp(claim, attributes))])(_serving_the_secret())


def _nested_sub_dependency(app, path, attributes):
    def claim(request: Request) -> None:
        return None
    _stamp(claim, attributes)

    def outer(_inner=Depends(claim)) -> None:
        return None

    app.get(path, dependencies=[Depends(outer)])(_serving_the_secret())


def _another_routers_dependency(app, path, attributes):
    def claim(request: Request) -> None:
        return None
    other = APIRouter(dependencies=[Depends(_stamp(claim, attributes))])
    other.get(path)(_serving_the_secret())
    app.include_router(other)


def _callable_class_dependency(app, path, attributes):
    class Claim:
        def __call__(self, request: Request) -> None:
            return None

    app.get(path, dependencies=[Depends(_stamp(Claim(), attributes))])(_serving_the_secret())


POSITIONS = [
    ("endpoint_attribute", _endpoint_attribute),
    ("direct_dependency", _direct_dependency),
    ("nested_sub_dependency", _nested_sub_dependency),
    ("another_routers_dependency", _another_routers_dependency),
    ("callable_class_dependency", _callable_class_dependency),
]

CASES = [(mark, attributes, position_name, install)
         for mark, attributes in ATTRIBUTE_SETS
         for position_name, install in POSITIONS]
CASE_IDS = [f"{mark}-{position}" for mark, _, position, _ in CASES]


# --- the two product apps ----------------------------------------------------

STATE_ONLY_TOKEN = "state-only-token-for-tests-at-least-32-bytes-long"
STATE_ONLY_DIRECTORY = tempfile.mkdtemp(prefix="state-only-forgery-test-")


def _state_only_app():
    database = pathlib.Path(STATE_ONLY_DIRECTORY) / "state.sqlite"
    return state_only.create_app(str(database), STATE_ONLY_TOKEN, with_mcp=False)


STATE_ONLY_APP = _state_only_app()


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

    Same helper, and for the same reason, as
    `tests/unit/test_state_prefix_on_the_product_app.py`: the routes go to the
    FRONT, because `api/main.py` ends with `app.mount("/", StaticFiles(...))`,
    which matches every path - a route appended after it 404s, and a 404 read as
    a refusal is how this file would pass over a route that was never there.
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


FORGED_PATH = "/api/state/forged"


@pytest.mark.parametrize("mark,attributes,position,install", CASES, ids=CASE_IDS)
def test_a_forged_claim_of_having_been_judged_is_refused(mark, attributes, position,
                                                         install, gated):
    """`api/main.py`, the real `require_reader`, no credentials.

    Every one of these was `200 leaked=True` on `34f380aa` for the marks r4's
    stand-down read, and the coverage test called the same routes covered.
    """
    with _temporarily(api_main.app, lambda app: install(app, FORGED_PATH, attributes)):
        with TestClient(api_main.app) as client:
            response = client.get(FORGED_PATH)
    assert response.status_code == 403, (mark, position, response.status_code,
                                         response.text[:200])
    assert SECRET not in response.text, (mark, position)


@pytest.mark.parametrize("mark,attributes,position,install", CASES, ids=CASE_IDS)
def test_the_same_forged_route_is_served_once_the_verdict_allows(mark, attributes,
                                                                 position, install,
                                                                 gated, monkeypatch):
    """Pole two. Without it a 403 could be the route never having been built:
    the same forged route, the same app, an authorized caller, 200 and the body.
    """
    monkeypatch.setattr(api_main.app.state, "_test_mode", True, raising=False)
    with _temporarily(api_main.app, lambda app: install(app, FORGED_PATH, attributes)):
        with TestClient(api_main.app) as client:
            response = client.get(FORGED_PATH)
    assert response.status_code == 200, (mark, position, response.status_code,
                                         response.text[:200])
    assert SECRET in response.text, (mark, position)


@pytest.mark.parametrize("mark,attributes,position,install", CASES, ids=CASE_IDS)
def test_the_state_only_deployment_refuses_the_same_forgery(mark, attributes, position,
                                                            install):
    """The second product app. Its bearer middleware is satisfied on purpose -
    otherwise the 401 would answer before the verdict ever ran, and this file
    would be measuring the token rather than the stand-down. The verdict itself
    is overridden to refuse, exactly as
    `test_state_only_applies_its_own_verdict_under_the_prefix` does, so what
    moves is whether the forged claim stood that verdict down.
    """
    def refuse(request: Request) -> None:
        raise HTTPException(403, "state-only refuses")

    app = STATE_ONLY_APP
    app.dependency_overrides[app.state.state_verdict] = refuse
    try:
        with _temporarily(app, lambda a: install(a, FORGED_PATH, attributes)):
            with TestClient(app) as client:
                response = client.get(
                    FORGED_PATH, headers={"Authorization": f"Bearer {STATE_ONLY_TOKEN}"})
    finally:
        app.dependency_overrides.pop(app.state.state_verdict, None)
    assert response.status_code == 403, (mark, position, response.status_code,
                                         response.text[:200])
    assert SECRET not in response.text, (mark, position)


@pytest.mark.parametrize("declares", [False, True],
                         ids=["declaring_nothing", "declaring_a_public_read"])
def test_the_real_guard_hung_on_a_route_of_ones_own_still_refuses_it(declares, gated):
    """The one thing a forger could do that is not a forgery: obtain the real
    guard object - it is reachable through the router - and hang it on a route
    of their own. It is no way round, because it is the GUARD: it reads the
    route's declaration and refuses a route that declares nothing, and a route
    that declares a public read is cross-checked against what its own source
    executes. Here the handler executes nothing of the sort, so both poles of
    the declaration end in a refusal with the body still inside.
    """
    guard = _the_real_router_guard()

    def install(app):
        endpoint = _serving_the_secret()
        if declares:
            setattr(endpoint, STATE_ROUTE_DECLARATION, ("read", "get_graph"))
        app.get(FORGED_PATH, dependencies=[Depends(guard)])(endpoint)

    with _temporarily(api_main.app, install):
        with TestClient(api_main.app) as client:
            response = client.get(FORGED_PATH)
    assert response.status_code == 403, (declares, response.status_code,
                                         response.text[:200])
    assert SECRET not in response.text, declares


def test_the_state_only_deployment_refuses_the_forgery_anonymously():
    """And with no token at all, for every position: 401, nothing out."""
    app = STATE_ONLY_APP
    for mark, attributes in ATTRIBUTE_SETS:
        for position, install in POSITIONS:
            with _temporarily(app, lambda a: install(a, FORGED_PATH, attributes)):
                with TestClient(app) as client:
                    response = client.get(FORGED_PATH)
            assert response.status_code == 401, (mark, position, response.status_code)
            assert SECRET not in response.text, (mark, position)
