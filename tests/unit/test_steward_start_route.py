"""The real start route must leave an absent `model` absent all the way down.

The dashboard posts `{}` to POST /api/dsh-drivers/<project>/start. The handler
forwards `body.model` to `DshDrivers.start`, which substitutes the project's role
route only for a FALSY model. A schema default of "driver" therefore named a
model explicitly, so the reserved steward project was provisioned with the
director's route. These arms drive the HTTP route itself and read the row it
provisioned; the host-mode runtime makes no model call and spawns no process.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, dsh_driver_routers
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, load_roles
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

HOME = "aitelier-home"
DIRECTOR_PROJECT = "alpha"
EXPLICIT_PROJECT = "gamma"


@pytest.fixture
def world(tmp_path, state_db_copy, monkeypatch):
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    for project in (HOME, DIRECTOR_PROJECT, EXPLICIT_PROJECT):
        service.store.create_project(project, project.title())
    registry = DriverRegistry(db, "steward-start-route-pepper-not-a-secret")
    registry.seed("fixture-owner-token")
    root = tmp_path / "drivers"
    root.mkdir()
    # Production's own switch to the host controller: `start` then provisions and
    # stamps the row without ever reaching `create_subprocess_exec`.
    (root / "runtime.json").write_text(json.dumps({"mode": "host"}))
    runtime = DshDrivers(db, registry, root)
    assert runtime.external_host

    app = FastAPI()
    app.include_router(dsh_driver_routers.router)
    app.dependency_overrides[dsh_driver_routers.manager] = lambda: runtime
    app.dependency_overrides[authz.require_writer] = lambda: None
    # Auth inputs only: the route, schema validation and runtime are the product's.
    monkeypatch.setattr(authz, "request_actor", lambda request: "fixture")
    with TestClient(app) as http:
        yield runtime, http


def test_absent_model_takes_the_steward_role_route_on_the_reserved_project(world):
    runtime, http = world
    roles = load_roles()
    assert roles["steward"]["route"] == "steward"

    response = http.post(f"/api/dsh-drivers/{HOME}/start", json={})

    assert response.status_code == 200, response.text
    row = runtime.get(HOME)
    assert row is not None
    assert row["model"] == "steward"
    assert row["provider"] == "aitelier", row


def test_absent_model_takes_the_director_role_route_on_an_ordinary_project(world):
    runtime, http = world
    roles = load_roles()
    assert roles["director"]["route"] == "driver"

    response = http.post(f"/api/dsh-drivers/{DIRECTOR_PROJECT}/start", json={})

    assert response.status_code == 200, response.text
    row = runtime.get(DIRECTOR_PROJECT)
    assert row is not None
    assert row["model"] == "driver", row
    assert row["provider"] == "aitelier", row


def test_an_explicit_model_overrides_the_project_role_route(world):
    runtime, http = world

    response = http.post(f"/api/dsh-drivers/{EXPLICIT_PROJECT}/start",
                         json={"model": "fixture-named-route"})

    assert response.status_code == 200, response.text
    row = runtime.get(EXPLICIT_PROJECT)
    assert row is not None
    assert row["model"] == "fixture-named-route", row
