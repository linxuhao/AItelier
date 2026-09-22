"""A declaration binds the action the handler ACTUALLY serves.

Two halves made an anonymous read of a private record possible, and this pins the
second one. A route could declare a PUBLIC read action while its handler executed
and RETURNED a PRIVATE one: the guard honoured the declaration and the private
body left. The declaration check was set membership (`action in reading.actions`),
so a handler only had to MENTION a public action somewhere - one line of dead code
did it.

The property now: a declaration is honoured only when the handler's own source
delivers that very action and delivers nothing private. `api.state_verdict`
reduces the handler body to the actions a reachable `return` can hand to a caller;
dead code binds nothing, an unreadable handler is refused, and a handler that
delivers several actions may declare a public one only when all of them are
public.

`TestNoStandDownAndTheVerdictIsIdempotent` pins the other half of the goal. The
exemption a route author could reach (endpoint attribute -> dependency attribute
-> object identity) existed so that a route already judged would not be judged
twice. Applying the verdict is idempotent, so the exemption protected nothing; the
tests here prove the second judgment is byte-identical to the first and that the
exemption and its carriers are gone from the verdict path.

`TestTheVerdictRanNotJustTheTree` measures coverage from judgments that actually
RAN (`VerdictLedger`), so a route that answers while no verdict ran is counted
UNCOVERED and named - the number that stayed True while the forged route leaked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route as StarletteRoute

from api import authz
from api.state_graph_routers import get_service, router as state_router
from api.state_verdict import (VerdictLedger, binding_for, coverage_report,
                               declaration_table, state_route_paths,
                               uncovered_routes)
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

REPO = Path(__file__).resolve().parents[2]
PRIVATE_PID = "binding-private"
OPEN_PID = "binding-open"
PRIVATE_SECRET = "BINDING-PRIVATE-BODY-9137"
OPEN_SECRET = "BINDING-OPEN-BODY-9137"
ADMIN = {"X-AItelier-Admin-Token": "binding-admin"}


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "binding-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _seed(service):
    for pid, secret in ((PRIVATE_PID, PRIVATE_SECRET), (OPEN_PID, OPEN_SECRET)):
        service.create_project(pid, pid)
        service.driver_notes.update(pid, "permanent", secret, 0, "director")
    service.open_project(OPEN_PID)


def _app(tmp_path, monkeypatch):
    _arm(monkeypatch)
    service = StateService(StateDatabase(str(tmp_path / "binding.sqlite")),
                           actor="binding-seeder", project_read_trusted=True)
    _seed(service)
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    return app, service


def _guard():
    """The product's OWN router-wide guard, reached the way a route author does."""
    return state_router.dependencies[0].dependency


def _mount(app, path, handler, declaration):
    setattr(handler, "_state_route", declaration)
    app.router.add_api_route(path, handler, methods=["GET"],
                             dependencies=[Depends(_guard())])
    app.router.routes.insert(0, app.router.routes.pop())
    return handler


# -- handlers whose source the binding has to read, one per rule ---------------
def declares_public_serves_private(project_id: str, service=Depends(get_service)):
    if False:  # dead: binds nothing
        execute(service, "list_projects", {})
    return execute(service, "get_driver_note", {"project_id": project_id})


def declares_private_serves_private(project_id: str, service=Depends(get_service)):
    return execute(service, "get_driver_note", {"project_id": project_id})


def declares_public_serves_public(project_id: str, service=Depends(get_service)):
    return execute(service, "get_graph", {"project_id": project_id})


def dead_zero_branch(project_id: str, service=Depends(get_service)):
    if 0:
        return execute(service, "list_projects", {})
    return execute(service, "get_driver_note", {"project_id": project_id})


def dead_after_return(project_id: str, service=Depends(get_service)):
    return execute(service, "get_driver_note", {"project_id": project_id})
    execute(service, "list_projects", {})  # unreachable


def dead_inner_function(project_id: str, service=Depends(get_service)):
    def _never_called():
        return execute(service, "list_projects", {})

    return execute(service, "get_driver_note", {"project_id": project_id})


def dead_comment(project_id: str, service=Depends(get_service)):
    # execute(service, "list_projects", {})
    return execute(service, "get_driver_note", {"project_id": project_id})


def mentions_two_public_delivers_one(project_id: str, service=Depends(get_service)):
    execute(service, "list_projects", {})  # discarded result
    return execute(service, "get_graph", {"project_id": project_id})


def dispatch_on_body_action(action: str, service=Depends(get_service)):
    return execute(service, action, {})


def serves_no_state_action():
    return {"static": True}


class TestBindingIsAStatementAboutTheHandler:
    def test_a_public_declaration_over_a_private_delivery_is_REFUSED(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        _mount(app, "/api/state/forged", declares_public_serves_private,
               ("read", "list_projects"))
        with TestClient(app) as client:
            for pid, secret in ((PRIVATE_PID, PRIVATE_SECRET), (OPEN_PID, OPEN_SECRET)):
                response = client.get("/api/state/forged", params={"project_id": pid})
                assert response.status_code == 403, (pid, response.status_code, response.text)
                assert secret not in response.text

    def test_the_honest_declaration_is_judged_by_the_action_it_serves(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        _mount(app, "/api/state/honest", declares_private_serves_private,
               ("read", "get_driver_note"))
        with TestClient(app) as client:
            for pid in (PRIVATE_PID, OPEN_PID):
                anonymous = client.get("/api/state/honest", params={"project_id": pid})
                assert anonymous.status_code == 403, anonymous.text
                assert PRIVATE_SECRET not in anonymous.text
            writer = client.get("/api/state/honest", params={"project_id": OPEN_PID}, headers=ADMIN)
            assert writer.status_code == 200, writer.text
            assert OPEN_SECRET in writer.text

    def test_a_public_declaration_over_a_public_delivery_still_answers(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        _mount(app, "/api/state/ok", declares_public_serves_public, ("read", "get_graph"))
        with TestClient(app) as client:
            response = client.get("/api/state/ok", params={"project_id": OPEN_PID})
            assert response.status_code == 200, response.text

    @pytest.mark.parametrize("handler", [
        dead_zero_branch, dead_after_return, dead_inner_function, dead_comment,
        mentions_two_public_delivers_one, serves_no_state_action, dispatch_on_body_action,
    ])
    def test_a_declaration_its_source_does_not_bind_is_refused(self, tmp_path, monkeypatch, handler):
        app, _ = _app(tmp_path, monkeypatch)
        _mount(app, "/api/state/dead", handler, ("read", "list_projects"))
        with TestClient(app) as client:
            response = client.get("/api/state/dead", params={"project_id": OPEN_PID})
            assert response.status_code == 403, (handler.__name__, response.text)

    def test_binding_reads_the_source_not_a_set_of_names(self):
        """The unit half: `binding_for` on the endpoint directly, both poles."""
        ok = binding_for(declares_public_serves_public, "get_graph", "/api/state/x")
        assert ok.ok and ok.delivered == {"get_graph"}
        forged = binding_for(declares_public_serves_private, "list_projects", "/api/state/x")
        assert not forged.ok and forged.delivered == {"get_driver_note"}


class TestNoStandDownAndTheVerdictIsIdempotent:
    """Why the exemption is absent: judging twice changes nothing."""

    def test_the_exemption_and_its_carriers_are_gone_from_the_verdict_path(self):
        """No stand-down carrier survives in the CODE, not merely in the prose.

        The scan is over identifiers (AST names/attributes) and call names, so a
        docstring that names the removed exemption is not itself a carrier, while
        a live `_tree_holds`, a `WeakSet` registry or a `_MARK` constant is. A
        renamed fourth carrier of the same shape would still fail the attribute
        scan, because it would have to be reachable as an attribute of a module
        on the verdict path and be mentioned by the guard.
        """
        import ast

        import api.state_http as state_http
        import api.state_graph_routers  # noqa: F401  (builds the production router)

        assert not hasattr(state_http, "prefix_verdict_stands_down_for")
        assert not hasattr(state_http, "_tree_holds")
        banned = {"prefix_verdict_stands_down_for", "_tree_holds", "WeakSet"}
        for package in ("api", "core"):
            for path in (REPO / package).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute):
                        assert node.attr not in banned, (path, node.attr)
                    elif isinstance(node, ast.Name):
                        assert node.id not in banned, (path, node.id)


    def test_judging_a_request_N_times_equals_judging_it_once(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        with TestClient(app) as client:
            first = client.get(f"/api/state/projects/{PRIVATE_PID}/driver-note")
            for _ in range(5):
                again = client.get(f"/api/state/projects/{PRIVATE_PID}/driver-note")
                assert (again.status_code, again.text) == (first.status_code, first.text)
            assert first.status_code == 403
            assert PRIVATE_SECRET not in first.text

    def test_the_guard_is_armed_once_on_the_router_not_per_route(self):
        dependencies = state_router.dependencies
        assert len(dependencies) == 1, [d.dependency for d in dependencies]
        assert dependencies[0].dependency.__name__ == "_router_guard"
        assert state_router.routes, "the router under test must be non-empty"


class TestTheVerdictRanNotJustTheTree:
    """Coverage is 'did the verdict RUN', not 'is the guard in this route's tree'."""

    def test_every_state_route_under_the_prefix_is_enumerated_from_the_app(self):
        from api import main as main_module

        paths = state_route_paths(main_module.app)
        assert paths, "the product app must expose state routes"
        assert all(p == "/api/state" or p.startswith("/api/state/") for p in paths)
        assert any(p.endswith("/driver-note") for p in paths)
        table = declaration_table(main_module.app)
        assert set(table) == set(paths)
        for path, row in table.items():
            assert row["declaration"] is not None, path
            if row["verdict"] == "public":
                assert row["delivered"] or row["dispatch_params"], path

    def test_a_route_that_answers_without_a_judgment_is_NAMED_uncovered(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        exercised = {}
        ledger = VerdictLedger()
        with TestClient(app) as client:
            with ledger:
                for path in (f"/api/state/projects/{OPEN_PID}",
                             f"/api/state/projects/{OPEN_PID}/frontier"):
                    exercised.setdefault(path, []).append(client.get(path).status_code)
                # A route the guard NEVER sees: no dependency, so it answers
                # while no judgment is recorded for it.
                def ungated():
                    return {"detail": "served without the verdict"}

                setattr(ungated, "_state_route", ("read", "get_graph"))
                app.router.add_api_route("/api/state/ungated", ungated, methods=["GET"])
                exercised["/api/state/ungated"] = [client.get("/api/state/ungated").status_code]
        report = coverage_report(app, ledger, exercised)
        assert report["/api/state/ungated"]["uncovered"] is True
        assert report["/api/state/ungated"]["responded"] is True
        assert uncovered_routes(app, ledger, exercised) == ["/api/state/ungated"]
        # The gated routes WERE judged (their templates are in the ledger), so a
        report = coverage_report(app, ledger, exercised)
        assert report["/api/state/ungated"]["uncovered"] is True
        assert report["/api/state/ungated"]["responded"] is True
        assert uncovered_routes(app, ledger, exercised) == ["/api/state/ungated"]
        # The gated routes WERE judged (their templates are in the ledger), so a
        # concrete URL is matched to its template and reported as covered.
        for template in ("/api/state/projects/{project_id}",
                         "/api/state/projects/{project_id}/frontier"):
            assert report[template]["judged"] is True, template
            assert report[template]["responded"] is True, template
            assert report[template]["uncovered"] is False, template


    def test_a_mounted_sub_app_and_a_bare_starlette_route_are_both_enumerated(self, tmp_path, monkeypatch):
        app, _ = _app(tmp_path, monkeypatch)
        child = FastAPI()
        child.add_api_route("/api/state/child", serves_no_state_action, methods=["GET"])
        app.mount("/sub", child)

        async def bare(scope, receive, send):
            raise AssertionError("not served")

        app.router.routes.append(StarletteRoute("/api/state/bare", bare))
        paths = state_route_paths(app)
        assert "/api/state/child" in paths
        assert "/api/state/bare" in paths
        table = declaration_table(app)
        assert table["/api/state/bare"]["declaration"] is None
        assert table["/api/state/bare"]["verdict"] == "refused-undeclared"


class TestTheProductRouteTableIsFullyBound:
    """Every route the product builds: its declaration matches its handler."""

    def test_no_product_route_declares_a_public_action_it_does_not_serve(self):
        from api import main as main_module

        mismatches = {path: row for path, row in declaration_table(main_module.app).items()
                      if row["verdict"] == "refused-declaration-mismatch"}
        assert mismatches == {}, mismatches

    def test_the_second_assembly_point_is_bound_too(self, tmp_path):
        from api.state_only import create_app

        app = create_app(str(tmp_path / "second.sqlite"), "z" * 40, with_mcp=False)
        mismatches = {path: row for path, row in declaration_table(app).items()
                      if row["verdict"] == "refused-declaration-mismatch"}
        assert mismatches == {}, mismatches
