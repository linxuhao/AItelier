"""The verdict must be EFFECTIVE, not merely present in a route's tree.

The failure this pins is the one a coverage number hid: a forged route pushed a
private notebook out over 200 while `route_carries_state_verdict` reported True
and the whole suite stayed green. "The guard object is somewhere in this route's
tree" and "the verdict actually ran for this request" are different claims, and
only the second one is the property.

So coverage here is measured from JUDGMENTS THAT RAN (`VerdictLedger`), and the
whole route set is DERIVED from the app's own table - never a hand-written list.
Two things are then proved on the PRODUCT's app object and on the second
assembly point:

  * every `/api/state` route that answers was judged first (`uncovered_routes`
    is empty), because a route that responds while no judgment was recorded is
    counted uncovered and NAMED;
  * a route injected with no guard at all is counted uncovered and named - the
    metric moves when it should, which is the reason the metric exists.

The criterion-1 ATTACK is then done verbatim on each assembly point, with both
poles: refused on the unopened project (and the body absent) and on the opened
project too, because what is wrong with the forged route is its declaration.
An honest route on the same app is the liveness control that proves the attack
really landed.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

import api.state_graph_routers as state_graph_routers
from api import authz
from api.dependencies import get_db_manager, get_workspace_manager
from api.state_graph_routers import get_service, router as state_router
from api.state_verdict import (VerdictLedger, binding_for, coverage_report,
                               declaration_table, delivered_actions,
                               state_route_paths, uncovered_routes)
from core.db_manager import DBManager
from core.state_commands import PUBLIC_READS, execute, is_public_read
from core.state_database import StateDatabase
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager
from tests.support.state_author_surface import seed_private_mail

PRIVATE_PID = "verdict-private"
OPEN_PID = "verdict-open"
PRIVATE_SECRET = "VERDICT-PRIVATE-BODY-5521"
OPEN_SECRET = "VERDICT-OPEN-BODY-5521"
ATTACK = "/api/state/attack"
CONTROL = "/api/state/control"
STATE_ONLY_TOKEN = "v" * 40

# Concrete values for every path parameter the State router declares. Coverage
# matches a concrete request URL to its route TEMPLATE, so these are the values
# a real caller would send.
_PATH_VALUES = {"project_id": PRIVATE_PID, "node_key": "a", "issue_id": "x",
                "attempt_id": "nope", "run_id": "nope", "action": "get_graph"}


def _fill(path: str) -> str:
    for name, value in _PATH_VALUES.items():
        path = path.replace("{" + name + "}", value)
    return path


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "verdict-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _seed(service):
    # Each secret is in the project's notebook AND its director mailbox. An
    # opened project's notebook is public (owner ruling 2026-09-22), so the
    # private DELIVERY the forged routes attempt is the mailbox
    # (`PRIVATE_ACTION`, private per the one table); the unopened project's
    # notebook stays private and backs the driver-note route tests.
    for pid, secret in ((PRIVATE_PID, PRIVATE_SECRET), (OPEN_PID, OPEN_SECRET)):
        service.create_project(pid, pid)
        service.driver_notes.update(pid, "permanent", secret, 0, "director")
        seed_private_mail(service, pid, secret)
    service.open_project(OPEN_PID)


def _drive_every_state_route(client, app):
    """Request every `/api/state` route with a method it actually declares.

    A method the route does not declare is answered 405 BEFORE the route is
    matched, so it would look uncovered for a reason that has nothing to do
    with the verdict. Each route is therefore driven with its own methods, and
    every response is recorded under the concrete URL it was sent to.
    """
    exercised: dict = {}
    for path, route in state_route_paths(app).items():
        methods = sorted((getattr(route, "methods", None) or ()) - {"HEAD", "OPTIONS"})
        assert methods, path
        for method in methods:
            url = _fill(path)
            if method == "GET":
                response = client.get(url)
            else:
                response = client.post(url, json={"project_id": PRIVATE_PID})
            exercised.setdefault(url, []).append(response.status_code)
    return exercised


def _forge(app, router, path, handler, declaration, *, mount_guard=True):
    """Mount a route whose declaration is `declaration`, in front of any mount."""
    setattr(handler, "_state_route", declaration)
    if mount_guard:
        guard = router.dependencies[0].dependency
        app.router.add_api_route(path, handler, methods=["GET"],
                                 dependencies=[Depends(guard)])
    else:
        app.router.add_api_route(path, handler, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())


def _drop(app, *paths):
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "path", None) not in paths]


def _state_guard(app):
    """The product's OWN router-wide guard, the way a route author reaches it."""
    assert state_route_paths(app), "the assembly point must expose state routes"
    return state_router.dependencies[0].dependency


class TestTheProductAppObjectIsWhatIsMeasured:
    """The app the PRODUCT builds is the one measured, and the number moves when
    the verdict does not run."""

    def test_every_state_route_on_the_product_app_was_judged_before_it_answered(
            self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        from api import main as main_module

        app = main_module.app
        db = DBManager(str(tmp_path / "effective.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws"))
        _seed(StateService(db, ws, actor="verdict-seeder", project_read_trusted=True))
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws
        try:
            client = TestClient(app, client=("127.0.0.1", 53000))
            assert state_route_paths(app), "the product app must expose state routes"
            ledger = VerdictLedger()
            with ledger:
                exercised = _drive_every_state_route(client, app)
            report = coverage_report(app, ledger, exercised)
            responded = {path for path, row in report.items() if row["responded"]}
            assert responded, "no state route answered - the sweep measured nothing"
            # The assertion the criterion is about: nothing under the prefix may
            # answer without the verdict having run, and anything that did is
            # NAMED rather than quietly averaged away.
            assert uncovered_routes(app, ledger, exercised) == []
            for path in responded:
                # `judged` means an authorization dependency EXECUTED; a public
                # read approved by its declaration binding answers without one
                # and is honest `cleared` - never a disguised judgment.
                assert report[path]["judged"] or report[path]["cleared"], path
        finally:
            app.dependency_overrides.clear()

    def test_a_route_injected_with_no_guard_is_counted_uncovered_and_named(
            self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        from api import main as main_module

        app = main_module.app
        db = DBManager(str(tmp_path / "injected.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws2"))
        _seed(StateService(db, ws, actor="verdict-seeder", project_read_trusted=True))
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws
        injected_path = "/api/state/injected-no-guard"

        def injected_probe():
            return {"detail": "answered with no verdict"}

        try:
            # No dependency, no guard: it answers while no judgment exists. It is
            # discovered by the SAME walk, without anybody editing a list.
            app.router.add_api_route(injected_path, injected_probe, methods=["GET"])
            app.router.routes.insert(0, app.router.routes.pop())
            assert injected_path in state_route_paths(app), \
                "a newly injected route must be enumerated automatically"
            client = TestClient(app, client=("127.0.0.1", 53001))
            ledger = VerdictLedger()
            with ledger:
                exercised = _drive_every_state_route(client, app)
                exercised.setdefault(injected_path, []).append(
                    client.get(injected_path).status_code)
            report = coverage_report(app, ledger, exercised)
            assert report[injected_path]["responded"] is True
            assert report[injected_path]["uncovered"] is True
            assert injected_path in uncovered_routes(app, ledger, exercised)
        finally:
            _drop(app, injected_path)
            app.dependency_overrides.clear()

    def test_the_full_table_is_derived_from_the_route_table_and_names_every_route(
            self, tmp_path):
        """route -> declaration -> delivered actions -> verdict, one row each."""
        from api import main as main_module

        app = main_module.app
        paths = state_route_paths(app)
        table = declaration_table(app)
        assert set(table) == set(paths)
        assert len(table) >= 20, sorted(table)
        for path, row in table.items():
            assert row["declaration"] is not None, path
            assert row["verdict"], path
            if row["verdict"] == "public":
                assert row["delivered"] or row["dispatch_params"], path

        second_db = str(tmp_path / "second.sqlite")
        _seed(StateService(StateDatabase(second_db), actor="verdict-seeder",
                           project_read_trusted=True))
        from api.state_only import create_app

        second = create_app(second_db, STATE_ONLY_TOKEN, with_mcp=False)
        second_paths = state_route_paths(second)
        second_table = declaration_table(second)
        assert second_paths, "the second assembly point must expose state routes"
        assert set(second_table) == set(second_paths)
        for path, row in second_table.items():
            assert row["declaration"] is not None, path
        assert uncovered_routes(second, VerdictLedger(), {}) == []


class TestTheAttackOnBothAssemblyPoints:
    """The criterion-1 attack, done verbatim where the product is assembled."""

    def test_the_attack_and_its_control_on_the_product_app(self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        from api import main as main_module

        app = main_module.app
        db = DBManager(str(tmp_path / "attack.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws3"))
        _seed(StateService(db, ws, actor="verdict-seeder", project_read_trusted=True))
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws

        def forged(request: Request, pid: str, svc=Depends(get_service)):
            execute(svc, "list_projects", {})
            return execute(svc, "list_director_messages", {"project_id": pid})

        def honest(request: Request, pid: str, svc=Depends(get_service)):
            return execute(svc, "get_graph", {"project_id": pid})

        _forge(app, state_graph_routers.router, ATTACK, forged,
               ("read", "list_projects"))
        _forge(app, state_graph_routers.router, CONTROL, honest,
               ("read", "get_graph"))
        try:
            client = TestClient(app, client=("127.0.0.1", 53002))
            # The forged declaration is refused on BOTH poles.
            private = client.get(ATTACK, params={"pid": PRIVATE_PID})
            assert private.status_code == 403, (private.status_code, private.text[:200])
            assert PRIVATE_SECRET not in private.text
            opened = client.get(ATTACK, params={"pid": OPEN_PID})
            assert opened.status_code == 403, (opened.status_code, opened.text[:200])
            assert OPEN_SECRET not in opened.text
            # Liveness control: an HONEST public read on the same app is refused
            # while the project is unopened and answers with the project once it
            # is opened, so the 403s above are refusals and not a route that
            # never landed.
            control_private = client.get(CONTROL, params={"pid": PRIVATE_PID})
            assert control_private.status_code == 403, control_private.status_code
            assert PRIVATE_PID not in control_private.text
            control_live = client.get(CONTROL, params={"pid": OPEN_PID})
            assert control_live.status_code == 200, (control_live.status_code,
                                                    control_live.text[:200])
            assert OPEN_PID in control_live.text
        finally:
            _drop(app, ATTACK, CONTROL)
            app.dependency_overrides.clear()

    def test_the_same_attack_on_the_second_assembly_point(self, tmp_path):
        """`api.state_only` - the guard is the SAME one, and the bearer token is
        a transport refusal on top of it, not a substitute for it."""
        from api.state_only import create_app

        db_path = str(tmp_path / "state-only-attack.sqlite")
        _seed(StateService(StateDatabase(db_path), actor="verdict-seeder",
                           project_read_trusted=True))
        app = create_app(db_path, STATE_ONLY_TOKEN, with_mcp=False)
        service = app.state.state_service
        guard = _state_guard(app)

        def forged(request: Request, pid: str):
            execute(service, "list_projects", {})
            return execute(service, "list_director_messages", {"project_id": pid})

        def honest(request: Request, pid: str):
            return execute(service, "get_graph", {"project_id": pid})

        for path, handler, declaration in ((ATTACK, forged, ("read", "list_projects")),
                                          (CONTROL, honest, ("read", "get_graph"))):
            setattr(handler, "_state_route", declaration)
            app.router.add_api_route(path, handler, methods=["GET"],
                                     dependencies=[Depends(guard)])
            app.router.routes.insert(0, app.router.routes.pop())
        headers = {"Authorization": f"Bearer {STATE_ONLY_TOKEN}"}
        with TestClient(app) as client:
            # Without the token the TRANSPORT refuses before any router runs.
            assert client.get(ATTACK,
                              params={"pid": PRIVATE_PID}).status_code == 401
            # With the token the request reaches the guard, and the guard
            # refuses the declaration its handler contradicts - on both poles.
            private = client.get(ATTACK, params={"pid": PRIVATE_PID}, headers=headers)
            assert private.status_code == 403, (private.status_code, private.text[:200])
            assert PRIVATE_SECRET not in private.text
            opened = client.get(ATTACK, params={"pid": OPEN_PID}, headers=headers)
            assert opened.status_code == 403, (opened.status_code, opened.text[:200])
            assert OPEN_SECRET not in opened.text
            # The discriminating pole on THIS assembly point: with the SAME
            # token, the honest public read answers 200 with the project while
            # the forged one answered 403 above. The refusal is therefore the
            # declaration, not the token and not the transport.
            control_live = client.get(CONTROL, params={"pid": OPEN_PID}, headers=headers)
            assert control_live.status_code == 200, (control_live.status_code,
                                                    control_live.text[:200])
            assert OPEN_PID in control_live.text


class TestThePropertyForgeryCorpusIsRefused:
    """A forged route declaring ANY public read the corpus lists is refused.

    The corpus is DERIVED from `PUBLIC_READS`, not written out, so an action
    reclassified public later cannot become a fresh hole without this going red.
    """

    def test_no_public_declaration_over_a_private_delivery_is_honoured(
            self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "corpus.sqlite")),
                               actor="verdict-seeder", project_read_trusted=True)
        _seed(service)
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_service] = lambda: service

        def forged(project_id: str, svc=Depends(get_service)):
            return execute(svc, "list_director_messages", {"project_id": project_id})

        corpus = sorted(action for action in PUBLIC_READS if is_public_read(action))
        assert corpus, "the corpus is empty - the derivation is broken"
        with TestClient(app) as client:
            for action in corpus:
                path = "/api/state/forged-" + action
                _forge(app, state_router, path, forged, ("read", action))
                try:
                    response = client.get(path, params={"project_id": PRIVATE_PID})
                    assert response.status_code == 403, (action, response.status_code)
                    assert PRIVATE_SECRET not in response.text, action
                finally:
                    _drop(app, path)


class TestTheRegressionsTheEarlierRoundsBought:
    """The wins of the previous rounds are re-asserted, not re-litigated."""

    def _gated_app(self, tmp_path, name, monkeypatch):
        """The gate ON and the product ``get_service``: an anonymous request
        gets the service its own credential earns, not a trusted fixture."""
        _arm(monkeypatch)
        db = StateDatabase(str(tmp_path / name))
        _seed(StateService(db, actor="verdict-seeder", project_read_trusted=True))
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: None
        return app

    def test_a_declaration_for_an_action_added_later_is_refused(self, tmp_path, monkeypatch):
        """An unrecognised action fails CLOSED, so a read added later is private
        until someone classifies it."""
        app = self._gated_app(tmp_path, "later.sqlite", monkeypatch)
        path = "/api/state/forged-added-later"

        def added_later(project_id: str):
            return {"detail": "no verdict asked for this action"}

        try:
            _forge(app, state_router, path, added_later,
                   ("read", "an_action_added_later"))
            with TestClient(app) as client:
                response = client.get(path, params={"project_id": PRIVATE_PID})
                assert response.status_code == 403, (response.status_code, response.text)
        finally:
            _drop(app, path)

    def test_a_route_that_declares_nothing_is_refused_not_opened(self, tmp_path, monkeypatch):
        """The author attached the real guard and declared NOTHING: DENY."""
        app = self._gated_app(tmp_path, "undeclared.sqlite", monkeypatch)
        path = "/api/state/undeclared"

        def undeclared(project_id: str):
            return {"notebook": PRIVATE_SECRET}

        app.router.add_api_route(path, undeclared, methods=["GET"],
                                 dependencies=[Depends(_state_guard(app))])
        app.router.routes.insert(0, app.router.routes.pop())
        try:
            with TestClient(app) as client:
                response = client.get(path, params={"project_id": PRIVATE_PID})
                assert response.status_code == 403, (response.status_code, response.text)
                assert PRIVATE_SECRET not in response.text
        finally:
            _drop(app, path)

    def test_head_and_a_trailing_slash_do_not_leak_a_private_body(self, tmp_path, monkeypatch):
        app = self._gated_app(tmp_path, "methods.sqlite", monkeypatch)
        route = f"/api/state/projects/{PRIVATE_PID}/driver-note"
        with TestClient(app) as client:
            for response in (client.head(route), client.get(route + "/")):
                assert response.status_code != 200, (response.status_code, response.text)
                assert PRIVATE_SECRET not in response.text

    def test_the_prefix_boundary_is_a_boundary(self, tmp_path, monkeypatch):
        """`/api/stateful` is a near miss, not a member of the prefix."""
        app = self._gated_app(tmp_path, "boundary.sqlite", monkeypatch)
        near_miss = "/api/stateful"
        app.router.add_api_route(near_miss, lambda: {"detail": "outside the prefix"},
                                 methods=["GET"])
        try:
            assert near_miss not in state_route_paths(app)
            assert all(p == "/api/state" or p.startswith("/api/state/")
                       for p in state_route_paths(app))
        finally:
            _drop(app, near_miss)

    def test_a_mounted_sub_app_and_a_bare_starlette_route_are_enumerated(self, tmp_path, monkeypatch):
        from starlette.routing import Route as StarletteRoute

        app = self._gated_app(tmp_path, "mounted.sqlite", monkeypatch)
        child = FastAPI()

        def child_handler():
            return {"detail": "child"}

        child.add_api_route("/api/state/child", child_handler, methods=["GET"])
        app.mount("/sub", child)

        async def bare(scope, receive, send):
            raise AssertionError("never served")

        app.router.routes.append(StarletteRoute("/api/state/bare", bare))
        paths = state_route_paths(app)
        assert "/api/state/child" in paths
        assert "/api/state/bare" in paths
        assert declaration_table(app)["/api/state/bare"]["verdict"] == \
            "refused-undeclared"

    def test_the_verdict_is_idempotent_for_the_same_request(self, tmp_path, monkeypatch):
        """Judging N times yields the status and body of judging it once."""
        app = self._gated_app(tmp_path, "idem.sqlite", monkeypatch)
        route = f"/api/state/projects/{PRIVATE_PID}/driver-note"
        with TestClient(app) as client:
            first = client.get(route)
            for _ in range(5):
                again = client.get(route)
                assert (again.status_code, again.text) == (first.status_code, first.text)
            assert first.status_code == 403
            assert PRIVATE_SECRET not in first.text

    def test_an_unreadable_declaration_refuses_it_does_not_approve(self):
        """An unread declaration is not an approval.

        `delivered_actions` cannot read a handler built at runtime (`exec`), so
        the binding must answer REFUSED - never "no objection found, therefore
        public". That is what keeps the property holding for handlers this
        module was never written against.
        """
        namespace: dict = {}
        exec("def dynamic_handler(project_id):\n    return {'x': 1}\n", namespace)
        dynamic = namespace["dynamic_handler"]
        assert delivered_actions(dynamic).readable is False
        assert binding_for(dynamic, "get_graph", "/api/state/dynamic").ok is False
        # A builtin has no Python source at all, and is refused for the same
        # reason rather than passing for having delivered nothing.
        assert delivered_actions(len).readable is False
        assert binding_for(len, "get_graph", "/api/state/builtin").ok is False
