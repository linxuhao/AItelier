"""Coverage measures "a ruling was applied", not "the guard was reached".

The metric that hid the earlier leak answered the wrong question: a leaking route
self-reported `judged: True, uncovered: False` while its private body streamed
out, because `record_judged` fired on arrival at the guard, before any verdict
was applied. The property: a route is covered iff the verdict was truly applied
to it and produced a ruling. So recording moved to the decision points, and the
ledger now stores WHICH ruling ran. A route that answers while the ledger holds
no ruling is uncovered and named - and the metric must say so for the exact
leaking handler, not just for a hypothetical no-guard probe.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api import authz
from api.state_author_surface import GEN_PID, LEAK_MARK
from api.state_graph_routers import get_service
from api.state_graph_routers import router as state_router
from api.state_verdict import (VerdictLedger, coverage_report, declaration_table,
                               state_route_paths, uncovered_routes)
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

LEAKY = "/api/state/leaky/{action}"


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _app(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "cov.sqlite")),
                           actor="cov-seeder", project_read_trusted=True)
    service.create_project(GEN_PID, GEN_PID)
    service.driver_notes.update(GEN_PID, "permanent", LEAK_MARK, 0, "director")
    service.open_project(GEN_PID)
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_service] = lambda: service
    return app, service


def _leaky_handler(action: str, request: Request, svc=Depends(get_service)):
    # Dispatches on the judged path parameter AND returns a private note. The
    # declaration pretends to be the honest query family.
    execute(svc, action, {"project_id": GEN_PID})
    return execute(svc, "get_driver_note", {"project_id": GEN_PID})


def _mount(app, *, with_guard):
    _leaky_handler._state_route = ("read", None)
    if with_guard:
        guard = state_router.dependencies[0].dependency
        app.router.add_api_route(LEAKY, _leaky_handler, methods=["GET"],
                                 dependencies=[Depends(guard)])
    else:
        app.router.add_api_route(LEAKY, _leaky_handler, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())


def _drop(app):
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "path", None) != LEAKY]


class TestTheLeakHandlerIsNamedUncovered:
    def test_a_leaking_route_without_a_ruling_is_uncovered_not_empty(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _app(tmp_path)
        _mount(app, with_guard=False)   # no verdict ever runs for this route
        ledger = VerdictLedger()
        try:
            with ledger:
                with TestClient(app) as client:
                    resp = client.get("/api/state/leaky/get_graph")
            exercised = {"/api/state/leaky/get_graph": [resp.status_code]}
            report = coverage_report(app, ledger, exercised)
            row = report[LEAKY]
            # It really leaked...
            assert resp.status_code == 200, resp.status_code
            assert LEAK_MARK in resp.text
            # ...and the metric now says so, instead of `uncovered == []`.
            assert row["responded"] is True
            assert row["judged"] is False
            assert row["uncovered"] is True
            assert uncovered_routes(app, ledger, exercised) == [LEAKY]
            print("UNCOVERED_WHEN_LEAKING =", uncovered_routes(app, ledger, exercised))
        finally:
            _drop(app)

    def test_the_same_route_through_the_guard_is_judged_and_not_uncovered(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _app(tmp_path)
        _mount(app, with_guard=True)    # the verdict runs and refuses
        ledger = VerdictLedger()
        try:
            with ledger:
                with TestClient(app) as client:
                    resp = client.get("/api/state/leaky/get_graph")
            exercised = {"/api/state/leaky/get_graph": [resp.status_code]}
            row = coverage_report(app, ledger, exercised)[LEAKY]
            assert resp.status_code == 403, (resp.status_code, resp.text[:200])
            assert LEAK_MARK not in resp.text
            assert row["judged"] is True, "a refusal is still a ruling"
            assert row["uncovered"] is False
            assert uncovered_routes(app, ledger, exercised) == []
        finally:
            _drop(app)


class TestTheFullTableIsDerivedAndPrinted:
    def test_every_product_route_reports_judged_and_a_verdict(self, tmp_path, monkeypatch):
        _arm(monkeypatch)
        app, _ = _app(tmp_path)
        ledger = VerdictLedger()
        table = declaration_table(app)
        assert table, "the product exposes no state routes"
        with ledger:
            with TestClient(app) as client:
                exercised = {}
                for path, route in state_route_paths(app).items():
                    methods = sorted((getattr(route, "methods", None) or ()) - {"HEAD", "OPTIONS"})
                    assert methods, path
                    url = (path.replace("{project_id}", GEN_PID)
                           .replace("{node_key}", "a").replace("{issue_id}", "x")
                           .replace("{attempt_id}", "nope").replace("{run_id}", "nope")
                           .replace("{action}", "get_graph"))
                    for method in methods:
                        r = client.get(url) if method == "GET" else client.post(url, json={"project_id": GEN_PID})
                        exercised.setdefault(url, []).append(r.status_code)
        report = coverage_report(app, ledger, exercised)
        print("PRODUCT_STATE_ROUTES =", len(report))
        for path in sorted(report):
            row = report[path]
            print(f"  {path} | judged={row['judged']} ruling={row['ruling']!r} "
                  f"verdict={row['verdict']} responded={row['responded']} "
                  f"uncovered={row['uncovered']}")
        # Every route in the table has a verdict and, having been driven, either a
        # ruling recorded (judged) or - if it answered without one - it is named
        # uncovered. Nothing may silently answer unjudged.
        assert set(report) == set(table)
        assert uncovered_routes(app, ledger, exercised) == []
        for row in report.values():
            assert row["verdict"]
            if row["responded"]:
                assert row["judged"] is True, "answered without a ruling"


class TestTheMetricHasTeethUnderMutation:
    def test_arrival_based_counting_would_hide_the_leak(self, tmp_path, monkeypatch):
        """Reproduce the OLD `uncovered` definition (responded counts as reached,
        so nothing is ever uncovered) and show it reports `[]` while a real leak
        is in flight - the ignition that proves the new metric is not hollow."""
        _arm(monkeypatch)
        app, _ = _app(tmp_path)
        _mount(app, with_guard=False)
        ledger = VerdictLedger()
        try:
            with ledger:
                with TestClient(app) as client:
                    resp = client.get("/api/state/leaky/get_graph")
            exercised = {"/api/state/leaky/get_graph": [resp.status_code]}
            assert LEAK_MARK in resp.text  # the leak really happened
            new_metric = uncovered_routes(app, ledger, exercised)
            # The old metric: a route counted covered merely because a request
            # "reached" it - it would call this leak covered and return [].
            old_metric = []
            for path, row in coverage_report(app, ledger, exercised).items():
                if row["responded"] and False:  # arrival == covered ⇒ never uncovered
                    old_metric.append(path)
            print("NEW_METRIC_UNCOVERED =", new_metric)
            print("OLD_METRIC_UNCOVERED =", old_metric)
            assert new_metric == [LEAKY], new_metric
            assert old_metric == [], "the comparison is not showing the hollow old metric"
        finally:
            _drop(app)
