"""Which sessions may be sent an image (step 24.2b, design-steward 7.8.12a).

`GET /api/dsh-drivers/{pid}/route-input` answers `{root, child}` by the same
completion_route `/completion` routes by: the root session takes the row's
model when it has one, else its role's route; every child takes the worker
route. A route takes images only when every candidate is listed with `image`
in the table's `_input`. `/completion` refuses an `image_url` part on a route
that does not take images with 422 `route_no_image`, before any gateway is
built, so nothing is sent to a provider.
"""
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz
from api.dsh_driver_routers import manager, router
from core import ai_router, model_routes
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

# The committed roles.json routes: director `driver`, steward `steward`, worker `worker`.
ROUTES = {
    "driver": ["alpha/vis-1", "beta/vis-1"],
    "steward": ["alpha/vis-1", "beta/txt-1"],
    "worker": ["alpha/vis-1", "gamma/txt-2"],
    "seeing": ["beta/vis-1"],
    "blind": ["gamma/txt-2"],
    "_input": {"alpha/vis-1": ["text", "image"], "beta/vis-1": ["text", "image"],
               "beta/txt-1": ["text"]},
}
IMAGE = {"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}
TEXT = {"role": "user", "content": "work"}


@pytest.fixture
def runtime(tmp_path, state_db_copy, monkeypatch):
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps(ROUTES))
    monkeypatch.setattr(model_routes, "default_routes_file", lambda: str(routes))
    monkeypatch.setattr(model_routes, "_CACHE", {})
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    for pid in ("alpha", "beta", "aitelier-home"):
        service.store.create_project(pid, pid)
    registry = DriverRegistry(db, "dsh-fixture-pepper-" * 3)
    registry.seed("fixture-owner-token")
    return DshDrivers(db, registry, tmp_path / "drivers",
                      command=[sys.executable, "-c", "import time; time.sleep(90)"])


@pytest.fixture
def client(runtime, monkeypatch):
    identity = SimpleNamespace(driver_id=None)
    monkeypatch.setattr(authz, "request_identity", lambda _: identity)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[manager] = lambda: runtime
    app.dependency_overrides[authz.require_reader] = lambda: None
    app.dependency_overrides[authz.require_writer] = lambda: None
    with TestClient(app) as c:
        c.identity = identity
        yield c


def _driver(runtime, pid, model):
    """A provisioned, enabled row whose token `client.identity` presents."""
    runtime._provision(pid, "aitelier", model, "fixture")
    runtime.update(pid, enabled=1)
    return runtime.get(pid)


def _read(client, row):
    client.identity.driver_id = row["driver_id"]
    return client.get(f"/api/dsh-drivers/{row['project_id']}/route-input")


# ── GET route-input ──────────────────────────────────────────────────────────

def test_root_takes_the_role_route_and_child_the_worker_route(runtime, client):
    row = _driver(runtime, "alpha", "")
    response = _read(client, row)
    assert response.status_code == 200, response.text
    # director route `driver`: both candidates see; worker has a text-only one.
    assert response.json() == {"root": ["text", "image"], "child": ["text"]}


def test_the_rows_model_overrides_the_role_route_for_the_root_only(runtime, client):
    blind = _driver(runtime, "alpha", "blind")
    assert _read(client, blind).json() == {"root": ["text"], "child": ["text"]}
    seeing = _driver(runtime, "beta", "seeing")
    assert _read(client, seeing).json() == {"root": ["text", "image"], "child": ["text"]}


def test_the_steward_project_reads_the_steward_route(runtime, client):
    row = _driver(runtime, "aitelier-home", "")
    assert _read(client, row).json() == {"root": ["text"], "child": ["text"]}


def test_a_concrete_model_is_its_own_candidate_and_unlisted_means_text(runtime, client):
    unlisted = _driver(runtime, "alpha", "zeta/unregistered-1")
    assert _read(client, unlisted).json()["root"] == ["text"]
    listed = _driver(runtime, "beta", "alpha/vis-1")
    assert _read(client, listed).json()["root"] == ["text", "image"]


def test_a_worker_route_where_every_candidate_sees_gives_child_image(runtime, client, tmp_path):
    (tmp_path / "model_routes.json").write_text(json.dumps({**ROUTES, "worker": ["alpha/vis-1"]}))
    model_routes._CACHE.clear()
    row = _driver(runtime, "alpha", "")
    assert _read(client, row).json() == {"root": ["text", "image"], "child": ["text", "image"]}


def test_another_drivers_token_is_refused(runtime, client):
    alpha = _driver(runtime, "alpha", "")
    beta = _driver(runtime, "beta", "")
    client.identity.driver_id = beta["driver_id"]
    response = client.get("/api/dsh-drivers/alpha/route-input")
    assert response.status_code == 403
    assert response.json()["detail"] == "this runtime belongs to another driver"
    client.identity.driver_id = None
    assert client.get("/api/dsh-drivers/alpha/route-input").status_code == 403
    assert alpha["driver_id"] != beta["driver_id"]


# ── POST /completion backstop ────────────────────────────────────────────────

@pytest.fixture
def gateways(monkeypatch, runtime):
    """A stub AIGateway that records every construction and call."""
    built, calls = [], []

    class Stub:
        def __init__(self, route, **kw):
            built.append(route)
            self.active_model, self.last_usage = f"{route}-served", {}

        def return_to_earlier(self, cold=False):
            pass

        def generate_native(self, messages, tools=None):
            calls.append(messages)
            return SimpleNamespace(text="ok", tool_calls=None, reasoning_content=None, truncated=False)

    monkeypatch.setattr(ai_router, "AIGateway", Stub)
    monkeypatch.setattr(runtime, "running", lambda _pid: True)
    return SimpleNamespace(built=built, calls=calls)


def _complete(client, row, session_id, message):
    client.identity.driver_id = row["driver_id"]
    return client.post(f"/api/dsh-drivers/{row['project_id']}/completion",
                       json={"session_id": session_id, "messages": [message]})


def test_an_image_on_a_text_route_is_refused_before_any_gateway(runtime, client, gateways):
    row = _driver(runtime, "alpha", "blind")
    response = _complete(client, row, row["session_id"], IMAGE)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "route_no_image"
    assert gateways.built == [] and gateways.calls == []
    assert runtime.gateways.get("alpha", {}) == {}


def test_an_image_in_a_child_session_follows_the_worker_route(runtime, client, gateways):
    row = _driver(runtime, "alpha", "seeing")
    response = _complete(client, row, "child-1", IMAGE)
    assert response.status_code == 422
    assert response.json()["detail"] == "route_no_image"
    assert gateways.built == [] and gateways.calls == []


def test_text_on_a_text_route_and_an_image_on_an_image_route_go_through(runtime, client, gateways):
    blind = _driver(runtime, "alpha", "blind")
    response = _complete(client, blind, blind["session_id"], TEXT)
    assert response.status_code == 200, response.text
    assert json.loads(response.text)["served_by"] == "blind-served"
    seeing = _driver(runtime, "beta", "seeing")
    response = _complete(client, seeing, seeing["session_id"], IMAGE)
    assert response.status_code == 200, response.text
    assert gateways.built == ["blind", "seeing"]
    assert gateways.calls == [[TEXT], [IMAGE]]
