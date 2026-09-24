"""No datum a route author writes decides whether the prefix verdict applies.

Four rounds removed one exemption carrier and grew the next one - endpoint
attribute, dependency attribute, object identity, then a parameter-name-plus-
path-template branch - because nothing in the repository READ the shape "is a
route-author-written datum deciding whether to judge?" This is that reader.
`tests.support.state_author_surface` generates the WHOLE author-writable surface (not a
hand-written list) and drives every generated route through the product's own
router guard. The invariant: a route answers with a private body for NO
generated shape, and it answers at all only when an INDEPENDENT reader agrees
the shape is a public delivery. When a shape is refused, the refusal names it,
so a carrier that slipped through would be a red test that points at the shape.

The four known carriers are each generated; the criterion-1/5 private-delivery
handler is caught; and a deliberate mutation of the old ordering (dispatch
returning ok before the private check) is shown to leak - the ignition that
proves the reader has teeth, not just a passing assertion.
"""
from __future__ import annotations

import copy

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api import authz, state_http
from tests.support.state_author_surface import (GEN_PID, LEAK_MARK, PRIVATE_ACTION, Body, Shape,
                                                compile_handler, carrier_shapes,
                                                expected_can_serve, seed_private_mail,
                                                shape_count, shapes)
from api.state_graph_routers import get_service
from api.state_verdict import (Binding, VerdictLedger, binding_for, delivered_actions,
                               state_route_paths)
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService


def _arm(monkeypatch):
    """Force the gate ON and make every caller an anonymous non-reader, so a
    private read is refused and only a public, all-public delivery may answer."""
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _build_app(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "surface.sqlite")),
                           actor="surface-seeder", project_read_trusted=True)
    service.create_project(GEN_PID, GEN_PID)
    seed_private_mail(service, GEN_PID, LEAK_MARK)
    service.open_project(GEN_PID)
    app = FastAPI()
    from api.state_graph_routers import router as state_router
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    return app, service


def _namespace():
    return {"Depends": Depends, "get_service": get_service, "execute": execute}


def _mount(app, guard, shape, handler):
    setattr(handler, "_state_route", shape.declaration)
    route = app.router.add_api_route(shape.template, handler, methods=[shape.method],
                                     dependencies=[Depends(guard)])
    app.router.routes.insert(0, app.router.routes.pop())
    return route


def _unmount(app, route):
    app.router.routes = [r for r in app.router.routes if r is not route]


def _concrete(shape):
    return (shape.template.replace("{action}", "get_graph")
            .replace("{note}", "get_graph"))


def _drive(client, shape):
    url = _concrete(shape)
    if shape.method == "GET":
        return client.get(url)
    return client.post(url, json={"project_id": GEN_PID})


class TestTheGeneratorIsTheSurface:
    def test_it_enumerates_the_author_surface_and_prints_the_count(self):
        total = shape_count()
        print("GENERATED_ROUTE_COMBINATIONS =", total)
        # The cross product is 3 params x 2 methods x 7 bodies x 2 stand-down
        # flags, plus the one runtime-built (unreadable) refuse pole.
        assert total == 3 * 2 * 7 * 2 + 1, total
        labels = {s.label for s in shapes()}
        assert len(labels) == total, "generated shapes must be distinct"

    def test_the_four_named_carriers_are_generated_not_hand_picked(self):
        carriers = carrier_shapes()
        surface = {s.name for s in shapes()}
        assert len(carriers) == 4, sorted(carriers)
        for name, shape in carriers.items():
            # Each carrier is a member of the generated surface, not a private
            # helper - the generator reaches it by enumeration, not by name.
            assert shape.name in surface, (name, shape.name)
        assert len({s.name for s in carriers.values()}) == 4


class TestEveryGeneratedRouteIsJudged:
    def test_no_generated_shape_leaks_and_only_agreeing_shapes_serve(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router
        guard = state_router.dependencies[0].dependency
        namespace = _namespace()
        ledger = VerdictLedger()
        served_count = 0
        with ledger:
            with TestClient(app) as client:
                for shape in shapes():
                    handler = compile_handler(shape, namespace=namespace)
                    route = _mount(app, guard, shape, handler)
                    try:
                        response = _drive(client, shape)
                    finally:
                        _unmount(app, route)
                    text = response.text
                    # (1) the universal safety invariant: never the private body.
                    assert LEAK_MARK not in text, (shape.label, response.status_code, text[:200])
                    # (2) the reader and the guard agree on who may answer.
                    answered = 200 <= response.status_code < 300
                    assert answered == expected_can_serve(shape), (
                        shape.label, shape.intent, "status=", response.status_code,
                        "expected_serve=", expected_can_serve(shape), text[:120])
                    # (3) any route that answered was JUDGED (a ruling recorded).
                    if answered:
                        served_count += 1
                        assert (shape.template in ledger.judged(app)
                                or shape.template in ledger.cleared(app)), shape.label
        print("ANSWERED_SHAPES =", served_count)
        assert served_count > 0, "nothing served - the invariant checked an empty set"
    def test_a_standalone_attribute_never_changes_the_verdict(self, tmp_path, monkeypatch):
        """Each generated shape and its stand-down-attributed twin behave the same:
        a datum written onto the endpoint does not decide whether to judge. This is
        carrier 1 across the whole surface, not a single hand-written case."""
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router
        guard = state_router.dependencies[0].dependency
        namespace = _namespace()
        groups = {}
        for shape in shapes():
            groups.setdefault((shape.param, shape.method, shape.body), []).append(shape)
        compared = 0
        with TestClient(app) as client:
            for group in groups.values():
                if len(group) < 2:
                    continue
                outcome = {}
                for shape in group:
                    handler = compile_handler(shape, namespace=namespace)
                    route = _mount(app, guard, shape, handler)
                    try:
                        resp = _drive(client, shape)
                    finally:
                        _unmount(app, route)
                    outcome[shape.stand_down_attr] = (resp.status_code, LEAK_MARK in resp.text)
                assert outcome[True] == outcome[False], group[0].label
                compared += 1
        assert compared > 0, "no stand-down twins to compare"



class TestTheFourCarriersAreCaught:
    def _assert_caught(self, app, guard, namespace, shape):
        handler = compile_handler(shape, namespace=namespace)
        _mount(app, guard, shape, handler)
        with TestClient(app) as client:
            response = _drive(client, shape)
        app.router.routes = [r for r in app.router.routes
                             if getattr(r, "endpoint", None) is not handler]
        assert response.status_code == 403, (shape.label, response.status_code, response.text[:200])
        assert LEAK_MARK not in response.text, shape.label
        return response

    @pytest.mark.parametrize("carrier", ["carrier1_endpoint_attr", "carrier2_dependency_attr"])
    def test_endpoint_and_dependency_attribute_carriers_are_refused(self, carrier, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router
        shape = carrier_shapes()[carrier]
        # carrier2 stands in for a dependency attribute; the endpoint attribute it
        # also carries must not exempt it either way.
        self._assert_caught(app, state_router.dependencies[0].dependency, _namespace(), shape)

    def test_carrier3_object_identity_does_not_exempt(self, tmp_path, monkeypatch):
        """The route carries the guard object itself as its own dependency: the
        second judgment is idempotent and the private delivery is still refused."""
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router
        guard = state_router.dependencies[0].dependency
        shape = Shape(
            name="h_carrier3_identity", param="action",
            template="/api/state/gen/{action}", method="GET",
            body=Body.PRIVATE_ONLY, declaration=("read", "get_graph"),
            stand_down_attr=False, label="carrier3_object_identity",
            intent="route depends on the guard object itself",
        )
        handler = compile_handler(shape, namespace=_namespace())
        setattr(handler, "_state_route", shape.declaration)
        # Depend on the guard object itself, exactly the third carrier's trick.
        app.router.add_api_route(shape.template, handler, methods=["GET"],
                                 dependencies=[Depends(guard), Depends(guard)])
        app.router.routes.insert(0, app.router.routes.pop())
        with TestClient(app) as client:
            response = _drive(client, shape)
        assert response.status_code == 403, (response.status_code, response.text[:200])
        assert LEAK_MARK not in response.text

    def test_carrier4_param_name_and_template_is_refused(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router
        shape = carrier_shapes()["carrier4_param_name_template"]
        self._assert_caught(app, state_router.dependencies[0].dependency, _namespace(), shape)

    def test_the_binder_directly_refuses_the_carrier4_shape(self, tmp_path):
        """The reader's OWN verdict, on the endpoint, both poles - no HTTP."""
        shape = carrier_shapes()["carrier4_param_name_template"]
        handler = compile_handler(shape, namespace=_namespace())
        binding = binding_for(handler, "get_graph", "/api/state/gen/{action}")
        assert binding.ok is False, binding
        assert PRIVATE_ACTION in binding.reason, binding.reason

    def test_reverting_the_order_makes_carrier4_leak(self, tmp_path, monkeypatch):
        """The mutation: put `return ok=True` for the dispatch branch back BEFORE
        the private-delivery check. The generator must then report a leak (the
        ignition), proving the test can actually falsify the fix."""
        _arm(monkeypatch)
        app, _ = _build_app(tmp_path)
        from api.state_graph_routers import router as state_router

        def old_binding_for(endpoint, judged_action, route_path=""):
            delivery = delivered_actions(endpoint)
            if not delivery.readable:
                return Binding(False, "unreadable", delivery.actions)
            if delivery.opaque:
                return Binding(False, "opaque", delivery.actions)
            if delivery.params:
                if (delivery.params == {"action"} and "{action}" in route_path
                        and judged_action is not None):
                    return Binding(True, "", delivery.actions)  # BUG: private check skipped
                return Binding(False, "dispatch not judged", delivery.actions)
            if not delivery.actions:
                return Binding(False, "no state action", delivery.actions)
            from core.state_commands import is_public_read
            for action in sorted(delivery.actions):
                if not is_public_read(action):
                    return Binding(False, f"private {action}", delivery.actions)
            return Binding(True, "", delivery.actions)

        monkeypatch.setattr(state_http, "binding_for", old_binding_for)
        shape = carrier_shapes()["carrier4_param_name_template"]
        handler = compile_handler(shape, namespace=_namespace())
        _mount(app, state_router.dependencies[0].dependency, shape, handler)
        with TestClient(app) as client:
            response = _drive(client, shape)
        # Under the reverted order the carrier answers 200 and leaks the note.
        assert response.status_code == 200, (response.status_code, "mutation did not fire")
        assert LEAK_MARK in response.text, "mutation fired but did not leak - the test is hollow"


def test_the_expected_model_follows_the_generated_source(monkeypatch):
    """The model reads the SOURCE, not the body's name.

    The `_BODY_DELIVERS` dictionary this round deleted was keyed by the very
    body names the generator uses, so the "independent" model and the guard
    shared one author-written input. Here the SHAPE is untouched - body name,
    declaration, parameter all as the generator builds them - and only the
    SOURCE a body generates is tampered with: the model must FOLLOW the source.
    A model keyed by body name ignores the tampering and keeps refusing, so this
    test fires on it.
    """
    from tests.support import state_author_surface as surface

    shape = next(s for s in surface.shapes() if s.body == surface.Body.PRIVATE_ONLY)
    honest = surface._model_delivery(shape)
    assert honest[0] == frozenset({PRIVATE_ACTION}), honest
    assert surface.expected_can_serve(shape) is False

    real_source = surface._handler_source

    def tampered_source(param, body, name):
        return real_source(param, body, name).replace(repr(PRIVATE_ACTION), "'get_graph'")

    monkeypatch.setattr(surface, "_handler_source", tampered_source)
    mutated = surface._model_delivery(shape)
    assert mutated != honest, "the model ignored the source change - it is name-driven"
    assert mutated[0] == frozenset({"get_graph"}), mutated
    assert surface.expected_can_serve(shape) is True
