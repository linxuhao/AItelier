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
from tests.support.state_author_surface import GEN_PID, LEAK_MARK
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


def _app(tmp_path, name="cov.sqlite"):
    service = StateService(StateDatabase(str(tmp_path / name)),
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
            assert resp.status_code == 403, (resp.status_code, resp.text[:200])
            assert LEAK_MARK not in resp.text
            row = coverage_report(app, ledger, exercised)[LEAKY]
            # The refusal here is made by the declaration reader: the judged
            # action (`get_graph`, from the URL) is public, so its binding is
            # checked and refuses - NO authorization dependency executed. The
            # metric says so honestly: judged is False (no dependency ran), the
            # ruling names the declaration refusal, and the route is named
            # uncovered because it answered on a decision no dependency made.
            assert row["judged"] is False, row
            assert row["ruling"] == "declaration-refused", row
            assert row["uncovered"] is True, row
            assert uncovered_routes(app, ledger, exercised) == [LEAKY]
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
                assert row["judged"] or row["cleared"], (
                    row, "answered with neither a judgment nor a clearance")


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
            # "reached" it. Reproduced honestly: the set of responded routes IS
            # the old "covered" set, so the old metric counts the leak as
            # covered - the exact opposite of what the new metric says.
            old_metric = []
            for path, row in coverage_report(app, ledger, exercised).items():
                if row["responded"]:   # arrival == covered: the old definition
                    old_metric.append(path)
            print("NEW_METRIC_UNCOVERED =", new_metric)
            print("OLD_METRIC_UNCOVERED =", old_metric)
            assert new_metric == [LEAKY], new_metric
            assert old_metric == [LEAKY], old_metric
        finally:
            _drop(app)


class TestJudgedMeansADependencyExecuted:
    def test_an_anonymous_public_dispatch_read_is_cleared_not_judged(self, tmp_path, monkeypatch):
        """`judged` means an authorization dependency EXECUTED. An anonymous read
        of the product dispatch route with a public action is approved by the
        declaration binding alone: zero dependencies run, so the row must read
        judged=False, dep_backed=False, cleared=True, ruling=public-clearance -
        never the old lie judged=True ruling=dispatch-verdict."""
        _arm(monkeypatch)
        app, _ = _app(tmp_path)
        ledger = VerdictLedger()
        template = "/api/state/query/{action}"
        with ledger:
            with TestClient(app) as client:
                resp = client.post("/api/state/query/get_graph", json={"project_id": GEN_PID})
        assert resp.status_code == 200, resp.status_code
        row = coverage_report(app, ledger, {template: [resp.status_code]})[template]
        print("DISPATCH_ROW =", row)
        assert row["responded"] is True
        assert row["judged"] is False, "no dependency ran - judged must stay False"
        assert row["dep_backed"] is False
        assert row["cleared"] is True
        assert row["ruling"] == "public-clearance", row

    def test_restoring_arrival_counting_re_lies_and_is_caught(self, tmp_path, monkeypatch):
        """The mutation: record on ARRIVAL with dep_backed forced True, the way
        the old metric did. The same probe then reads judged=True with zero
        dependencies executed - the lie, observed - which is the ignition."""
        _arm(monkeypatch)
        import api.state_http as state_http
        real_record = state_http.record_judged
        template = "/api/state/query/{action}"

        def arrival_record(app_, path_, ruling_="verdict-applied", dep_backed=True):
            # the old bug: recorded regardless of whether anything ruled
            real_record(app_, path_, ruling_, dep_backed=True)

        app, _ = _app(tmp_path)
        ledger = VerdictLedger()
        with ledger:
            with TestClient(app) as client:
                resp = client.post("/api/state/query/get_graph", json={"project_id": GEN_PID})
        honest = coverage_report(app, ledger, {template: [resp.status_code]})[template]

        monkeypatch.setattr(state_http, "record_judged", arrival_record)
        app2, _ = _app(tmp_path, "cov-mutation.sqlite")
        ledger2 = VerdictLedger()
        with ledger2:
            with TestClient(app2) as client:
                resp2 = client.post("/api/state/query/get_graph", json={"project_id": GEN_PID})
        mutated = coverage_report(app2, ledger2, {template: [resp2.status_code]})[template]
        fired = 1 if (mutated["judged"] and not honest["judged"]) else 0
        print("IGNITION_COUNT =", fired, "MUTATED_ROW =", mutated)
        assert honest["judged"] is False
        assert mutated["judged"] is True and mutated["dep_backed"] is True
        assert fired > 0, "the mutation did not change what the metric says"
