"""The private-delivery check runs before any early `ok=True`.

`binding_for`'s dispatch branch used to `return Binding(True, ...)` as soon as a
handler dispatched on the path parameter the guard judged - never checking that
the same handler also RETURNED a private body. Three lines of handler earned an
anonymous 200 plus a private note. The fix: the private-delivery check runs on
`delivery.actions` before any early approval, so a handler that dispatches on the
judged parameter AND delivers something else private is refused. This pins both
poles, proves the legitimate `/query/{action}` route still serves, and shows the
write/director families are untouched (they never route through `binding_for`).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api import authz, state_http
from api.state_author_surface import GEN_PID, LEAK_MARK, carrier_shapes, compile_handler
from api.state_graph_routers import get_service
from api.state_graph_routers import router as state_router
from api.state_verdict import (Binding, binding_for, delivered_actions, route_verdict,
                               state_route_paths)
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _app(tmp_path, name="carrier"):
    service = StateService(StateDatabase(str(tmp_path / f"{name}.sqlite")),
                           actor="c5-seeder", project_read_trusted=True)
    service.create_project(GEN_PID, GEN_PID)
    service.driver_notes.update(GEN_PID, "permanent", LEAK_MARK, 0, "director")
    service.open_project(GEN_PID)
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    return app


class TestBothPolesOfTheBinder:
    def test_dispatch_that_also_delivers_private_is_refused(self, tmp_path):
        shape = carrier_shapes()["carrier4_param_name_template"]
        handler = compile_handler(shape, namespace={
            "Depends": Depends, "get_service": get_service, "execute": execute})
        binding = binding_for(handler, "get_graph", "/api/state/gen/{action}")
        assert binding.ok is False, binding
        assert "get_driver_note" in binding.reason, binding.reason

    def test_honest_dispatch_on_the_judged_parameter_still_binds(self, tmp_path):
        from api.state_author_surface import Body, Shape, Param
        honest = Shape(name="honest", param=Param.ACTION, template="/api/state/gen/{action}",
                       method="GET", body=Body.DISPATCH_PARAM, declaration=("read", None),
                       stand_down_attr=False, label="honest", intent="genuine dispatch")
        handler = compile_handler(honest, namespace={
            "Depends": Depends, "get_service": get_service, "execute": execute})
        binding = binding_for(handler, "get_graph", "/api/state/gen/{action}")
        assert binding.ok is True, binding


class TestQueryRouteStillServes:
    def test_the_product_query_dispatches_the_judged_path_action(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app = _app(tmp_path, "query")
        with TestClient(app) as client:
            served = client.post("/api/state/query/get_graph", json={"project_id": GEN_PID})
            assert served.status_code == 200, (served.status_code, served.text[:200])
            # A private action on the same dispatch route is refused (its body
            # never reaches an anonymous caller).
            refused = client.post("/api/state/query/get_driver_note", json={"project_id": GEN_PID})
            assert refused.status_code in (401, 403), refused.status_code
            assert LEAK_MARK not in refused.text


class TestWriteAndDirectorFamiliesUntouched:
    def test_binding_for_is_never_consulted_for_write_or_director(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app = _app(tmp_path, "families")
        calls = []

        def spy(endpoint, judged_action, route_path=""):
            calls.append((getattr(endpoint, "__name__", "?"), judged_action))
            return Binding(False, "spy-refused", delivered_actions(endpoint).actions)

        monkeypatch.setattr(state_http, "binding_for", spy)
        with TestClient(app) as client:
            client.post("/api/state/commands/close_project", json={"project_id": GEN_PID})
            client.post("/api/state/director-messages/send_director_message",
                        json={"project_id": GEN_PID})
        # Neither the write nor the director family routes through binding_for.
        assert calls == [], calls

    def test_the_static_verdict_of_each_family_is_its_own_ruling(self, tmp_path):
        app = _app(tmp_path, "verdicts")
        table = state_route_paths(app)
        assert route_verdict(table["/api/state/commands/{action}"]) == "write-verdict"
        assert route_verdict(table["/api/state/director-messages/{action}"]) == "director-verdict"
        assert route_verdict(table["/api/state/query/{action}"]) == "dispatch-on-path-action"


class TestMutationReopeningTheOrder:
    def test_reverting_the_order_leaks_the_note_through_the_guard(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app = _app(tmp_path, "mutation")
        from core.state_commands import is_public_read

        def old_binding_for(endpoint, judged_action, route_path=""):
            delivery = delivered_actions(endpoint)
            if not delivery.readable:
                return Binding(False, "unreadable", delivery.actions)
            if delivery.opaque:
                return Binding(False, "opaque", delivery.actions)
            if delivery.params:
                if (delivery.params == {"action"} and "{action}" in route_path
                        and judged_action is not None):
                    return Binding(True, "", delivery.actions)  # BUG: no private check
                return Binding(False, "not judged", delivery.actions)
            if not delivery.actions:
                return Binding(False, "none", delivery.actions)
            for action in sorted(delivery.actions):
                if not is_public_read(action):
                    return Binding(False, f"private {action}", delivery.actions)
            return Binding(True, "", delivery.actions)

        monkeypatch.setattr(state_http, "binding_for", old_binding_for)
        shape = carrier_shapes()["carrier4_param_name_template"]
        handler = compile_handler(shape, namespace={
            "Depends": Depends, "get_service": get_service, "execute": execute})
        handler._state_route = shape.declaration
        app.router.add_api_route(shape.template, handler, methods=["GET"],
                                 dependencies=[Depends(state_router.dependencies[0].dependency)])
        app.router.routes.insert(0, app.router.routes.pop())
        with TestClient(app) as client:
            resp = client.get("/api/state/gen/get_graph")
        # Under the reverted order the carrier answers 200 and the note leaks.
        assert resp.status_code == 200, (resp.status_code, "mutation did not fire")
        assert LEAK_MARK in resp.text, "mutation fired but did not leak"
