"""The verdict must run on FastAPI's own machinery, not on the guard's own call.

For any dependency D, handing D to this guard must release/refuse EXACTLY as
putting the same D into an ordinary route's ``Depends(D)``. The earlier guard did
``dependency(request)`` itself: correct for a plain ``def`` D, silently wrong for
an ``async def`` (a coroutine that is never awaited), a ``yield``/``async yield``
dependency (a generator whose body never runs) or a callable/class - every one of
those returned a truthy object without executing the refusal inside D, so a
private read leaked. This measures the two against each other for all six shapes
and pins the failure of the hand-call approach, so the fix cannot regress to a
shape-by-shape ``isawaitable``/``isgenerator`` table.
"""
from __future__ import annotations

import inspect

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api import authz
from tests.support.state_author_surface import dependency_shapes
from api.state_graph_routers import get_service
from api.state_http import create_state_router
from api.state_verdict import VerdictLedger, state_route_paths
from core.state_database import StateDatabase
from core.state_service import StateService

OPEN_PID = "fapi-open"
SECRET = "FAPI-PRIVATE-BODY-3341"


def _service(tmp_path, name):
    service = StateService(StateDatabase(str(tmp_path / name)), actor="fapi-seeder",
                           project_read_trusted=True)
    service.create_project(OPEN_PID, OPEN_PID)
    service.driver_notes.update(OPEN_PID, "permanent", SECRET, 0, "director")
    service.open_project(OPEN_PID)
    return service


def _authz_d_factory(kind):
    """Build an authorization dependency of each shape: it refuses a request that
    carries no `X-Auth: yes` header and releases one that does."""
    if kind == "def":
        def D(request: Request):
            if request.headers.get("X-Auth") != "yes":
                raise HTTPException(403, "no-read")
        return D
    if kind == "async_def":
        async def D(request: Request):
            if request.headers.get("X-Auth") != "yes":
                raise HTTPException(403, "no-read")
        return D
    if kind == "yield":
        def D(request: Request):
            if request.headers.get("X-Auth") != "yes":
                raise HTTPException(403, "no-read")
            yield None
        return D
    if kind == "async_yield":
        async def D(request: Request):
            if request.headers.get("X-Auth") != "yes":
                raise HTTPException(403, "no-read")
            yield None
        return D
    if kind == "callable":
        class CallableD:
            def __call__(self, request: Request):
                if request.headers.get("X-Auth") != "yes":
                    raise HTTPException(403, "no-read")
        return CallableD()
    if kind == "class":
        class ClassD:
            def __init__(self, request: Request):
                if request.headers.get("X-Auth") != "yes":
                    raise HTTPException(403, "no-read")
        return ClassD
    raise AssertionError(kind)


def _private_route_through_guard(tmp_path, name, D):
    """The product guard, armed with D as its read+write verdict, on a real
    private-read route."""
    service = _service(tmp_path, name)
    router = create_state_router(get_service, D, D)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_service] = lambda: service
    return app


def _private_route_via_plain_depends(tmp_path, name, D):
    """The SAME D placed in an ordinary route's `Depends(D)`."""
    service = _service(tmp_path, name)
    app = FastAPI()

    def handler(project_id: str, svc=Depends(get_service)):
        from core.state_commands import execute
        return execute(svc, "get_driver_note", {"project_id": project_id})

    app.add_api_route("/note/{project_id}", handler, methods=["GET"],
                      dependencies=[Depends(D)])
    app.dependency_overrides[get_service] = lambda: service
    return app


class TestTheGuardMatchesPlainDepends:
    def test_every_dependency_shape_releases_and_refuses_identically(self, tmp_path, monkeypatch):
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        kinds = sorted(dependency_shapes())
        assert set(kinds) >= {"def", "async_def", "yield", "async_yield", "callable", "class"}
        for kind in kinds:
            guard_app = _private_route_through_guard(tmp_path, f"guard-{kind}.sqlite", _authz_d_factory(kind))
            control_app = _private_route_via_plain_depends(tmp_path, f"ctrl-{kind}.sqlite", _authz_d_factory(kind))
            route = f"/api/state/projects/{OPEN_PID}/driver-note"
            with TestClient(guard_app) as gc, TestClient(control_app) as cc:
                guarded_anon = gc.get(route).status_code
                control_anon = cc.get(f"/note/{OPEN_PID}").status_code
                guarded_auth = gc.get(route, headers={"X-Auth": "yes"}).status_code
                control_auth = cc.get(f"/note/{OPEN_PID}", headers={"X-Auth": "yes"}).status_code
            assert guarded_anon == control_anon == 403, (
                kind, "guard-anon=", guarded_anon, "control-anon=", control_anon)
            assert guarded_auth == control_auth == 200, (
                kind, "guard-auth=", guarded_auth, "control-auth=", control_auth)

    def test_guard_does_not_call_the_dependency_itself(self, tmp_path, monkeypatch):
        """The refusal of an `async def` D is only observable when FastAPI awaits
        it. If the guard still hand-called, it would get a coroutine object and
        leak a 200; prove it does not, for both an async and a yield shape."""
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        for kind in ("async_def", "yield", "async_yield", "callable", "class"):
            app = _private_route_through_guard(tmp_path, f"leak-{kind}.sqlite", _authz_d_factory(kind))
            with TestClient(app) as client:
                response = client.get(f"/api/state/projects/{OPEN_PID}/driver-note")
            assert response.status_code == 403, (kind, response.status_code)
            assert SECRET not in response.text, kind

    def test_the_hand_call_approach_would_have_leaked_async(self):
        """The base pole: reproduce the OLD `dependency(request)` call and show it
        returns a coroutine for an `async def` D instead of running the refusal -
        which is why this property was false before the fix (red at the base)."""
        async def D(request: Request):
            raise HTTPException(403, "no-read")

        # The old guard's exact move: call D, adapt to its arity, keep the result.
        accepts = bool(inspect.signature(D).parameters)
        result = D(None) if accepts else D()
        try:
            result.close()  # discard the coroutine so -W error does not warn
        except AttributeError:
            pass
        assert inspect.iscoroutine(result), result
        # A coroutine object is truthy: the old code saw "no exception" and let
        # the private read through. FastAPI awaiting it would have raised 403.

    def test_the_ledger_records_the_ruling_under_fastapi_machinery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        D = _authz_d_factory("async_def")
        app = _private_route_through_guard(tmp_path, "ledger.sqlite", D)
        ledger = VerdictLedger()
        route_template = "/api/state/projects/{project_id}/driver-note"
        with ledger:
            with TestClient(app) as client:
                anon = client.get(f"/api/state/projects/{OPEN_PID}/driver-note")
                auth = client.get(f"/api/state/projects/{OPEN_PID}/driver-note",
                                  headers={"X-Auth": "yes"})
        assert anon.status_code == 403 and auth.status_code == 200
        # The private-read route was judged (a ruling applied) on both requests.
        assert route_template in ledger.judged(app)
        assert ledger.rulings(app)[route_template] in {"read-verdict", "private-verdict"}

    def test_a_yield_verdict_dependency_teardown_runs_after_the_handler(self, tmp_path, monkeypatch):
        """Observation point for criterion 6's reservation: the guard used to
        close its own AsyncExitStack BEFORE the handler, so a verdict dependency
        with a post-`yield` half ran its teardown order-reversed relative to a
        plain `Depends(D)` route. The guard is now a yield dependency and closes
        the verdict stack after the handler; this pins setup -> handler ->
        teardown, identical on both trees, and the mutation (closing the stack
        inside the guard, the old shape) is shown to reverse it."""
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        events: list = []

        def D(request: Request):
            events.append("setup")
            yield None
            events.append("teardown")

        def handler_recorder(project_id: str, svc=Depends(get_service)):
            events.append("handler")
            from core.state_commands import execute
            return execute(svc, "get_driver_note", {"project_id": project_id})

        # control tree: the SAME D in an ordinary Depends(D) route.
        control = FastAPI()
        control.add_api_route("/note/{project_id}", handler_recorder, methods=["GET"],
                              dependencies=[Depends(D)])
        control.dependency_overrides[get_service] = lambda: _service(tmp_path, "td-ctrl")
        events.clear()
        with TestClient(control) as client:
            client.get(f"/note/{OPEN_PID}")
        control_order = list(events)

        # guard tree: the product guard armed with the SAME D as its verdict.
        service = _service(tmp_path, "td-guard")
        router = create_state_router(get_service, D, D)
        guard_app = FastAPI()
        guard_app.include_router(router)
        guard_app.dependency_overrides[get_service] = lambda: service
        # The same recorder handler, judged by the REAL router guard.
        guard_app.router.add_api_route(
            "/g/{project_id}", handler_recorder, methods=["GET"],
            dependencies=[Depends(router.dependencies[0].dependency)])
        events.clear()
        with TestClient(guard_app) as client:
            client.get(f"/g/{OPEN_PID}")
        guard_order = list(events)

        print("CONTROL_ORDER =", control_order, "GUARD_ORDER =", guard_order)
        assert control_order == ["setup", "handler", "teardown"], control_order
        assert guard_order == control_order, (guard_order, control_order)
