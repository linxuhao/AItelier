"""A delivery the AST reader cannot see must be REFUSED, never approved.

The fail-open the review measured: `binding_for` approved a handler that
dispatched on the judged path parameter while a PRIVATE action left through a
delivery site the reader could not resolve - a module-level helper, a renamed
import, an `async def` inner function, or the action passed as the KEYWORD
`action=`. Each shape answered 200 with the private body, on a fresh app and
on the product app. A fifth shape delivers a PUBLIC literal beside a callee the
reader cannot resolve at all (a `functools.partial`); only the fail-closed
`opaque` branch refuses it. The fix is fail-closed: the reader follows a
helper through the handler's own globals (derivation), resolves an aliased
`execute` by object identity against the action table's own function, reads
keyword action slots, and marks every delivery it cannot explain `opaque` -
which refuses. This file pins all five shapes at the binding and at the guard.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api import authz
from api.state_graph_routers import get_service
from api.state_graph_routers import router as state_router
from api.state_verdict import binding_for
from tests.support.state_author_surface import (GEN_PID, LEAK_MARK, hiding_handlers,
                                                seed_private_mail)
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

TEMPLATE = "/api/state/gen/{action}"
KINDS = ["module_level_helper", "renamed_import", "async_inner", "keyword_action",
         "public_beside_unresolvable"]


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _app(tmp_path, name):
    service = StateService(StateDatabase(str(tmp_path / f"{name}.sqlite")),
                           actor="hide-seeder", project_read_trusted=True)
    service.create_project(GEN_PID, GEN_PID)
    seed_private_mail(service, GEN_PID, LEAK_MARK)
    service.open_project(GEN_PID)
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    return app


def _namespace():
    return {"Depends": Depends, "get_service": get_service, "execute": execute}


class TestTheBinderRefusesEveryHidingShape:
    @pytest.mark.parametrize("kind", KINDS)
    def test_the_binding_refuses(self, kind):
        handler = hiding_handlers(_namespace())[kind]
        binding = binding_for(handler, "get_graph", TEMPLATE)
        assert binding.ok is False, (kind, binding)


class TestTheGuardRefusesEveryHidingShape:
    @pytest.mark.parametrize("kind", KINDS)
    def test_anonymous_request_gets_403_not_the_private_body(self, kind, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app = _app(tmp_path, kind)
        handler = hiding_handlers(_namespace())[kind]
        handler._state_route = ("read", "get_graph")
        route = app.router.add_api_route(TEMPLATE, handler, methods=["GET"],
                                         dependencies=[Depends(state_router.dependencies[0].dependency)])
        app.router.routes.insert(0, app.router.routes.pop())
        try:
            with TestClient(app) as client:
                resp = client.get("/api/state/gen/get_graph")
        finally:
            app.router.routes = [r for r in app.router.routes if r is not route]
        assert resp.status_code == 403, (kind, resp.status_code, resp.text[:200])
        assert LEAK_MARK not in resp.text, kind
