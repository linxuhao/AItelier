"""The project gate holds where the product is ACTUALLY assembled.

Two assembly points, and an assertion on an app the test itself builds answers
for neither:

* ``api.main.app``             — the whole product: the real state router, every
                                 middleware, the real dependency graph;
* ``api.state_only.create_app`` — the bearer-protected second assembly point.

Both are measured, and each is attacked with the SAME forged route: reuse the
product's own router-wide guard, declare a PUBLIC action its handler does not
deliver, then execute and return a PRIVATE read. On the product app the refusal
comes from the guard's own declaration check, so it holds whether the project is
unopened or opened — what is wrong there is the declaration, not the read.

Because a run whose every response is a refusal cannot tell "the gate held" from
"the attack never landed", each class carries a LIVENESS CONTROL: an honest
route on the same app that performs a genuinely project-gated public read, is
refused while the project is unopened, and answers 200 once it is opened. The
control is honest by construction — its declaration names the action its handler
delivers, so it is judged by that action rather than stood down.

``api/state_only`` refuses at the TRANSPORT — its ASGI bearer middleware
answers 401 before any router runs — so the identical forged route is exercised
without the deployment token (refused) and with it (200 + full text). The
refusal is a property of that assembly point, not of a route declaration.
"""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.testclient import TestClient

import api.state_graph_routers as state_graph_routers
from api import authz
from api.dependencies import get_db_manager, get_workspace_manager
from api.state_graph_routers import get_service
from core.db_manager import DBManager
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

PRIVATE_PID = "assembly-private"
OPEN_PID = "assembly-open"
PRIVATE_SECRET = "PRODUCT-APP-PRIVATE-BODY-Q7"
OPEN_SECRET = "PRODUCT-APP-OPEN-BODY-Q7"
FORGED_PATH = "/api/state/forged-assembly"
HONEST_PATH = "/api/state/honest-assembly"
ADMIN = {"X-AItelier-Admin-Token": "assembly-point-admin"}
STATE_ONLY_TOKEN = "s" * 40


def _arm(monkeypatch):
    """Arm the identity gate with nobody allowlisted: every request that does
    not carry the admin token is an anonymous visitor."""
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "assembly-point-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _seed(service):
    for pid, secret in ((PRIVATE_PID, PRIVATE_SECRET), (OPEN_PID, OPEN_SECRET)):
        service.create_project(pid, pid)
        service.driver_notes.update(pid, "permanent", secret, 0, "director")
    service.open_project(OPEN_PID)


def _forge_on_product_app(app):
    """Mount the forged route and its liveness control on the product app.

    The forged route reuses the product's OWN router-wide guard and declares a
    public action its handler does not deliver: the declaration names
    `list_projects` (the discarded call) while the handler returns
    `get_driver_note`. It is refused by the guard's own declaration check.

    The honest route is the control: it performs a genuinely project-gated
    PUBLIC read (`get_graph`) with a declaration that names the action its
    handler serves, so it is judged by that action — refused while the project
    is unopened, answered 200 once it is opened.

    Each route goes in FRONT of the SPA catch-all mount (`app.mount("/", ...)`),
    which is registered last and matches every path: a route appended after it
    is never reached and answers 404, which would look like a refusal.
    """
    guard = state_graph_routers.router.dependencies[0].dependency

    def forged(request: Request, pid: str, svc=Depends(get_service)):
        execute(svc, "list_projects", {})
        return execute(svc, "get_driver_note", {"project_id": pid})

    def honest(request: Request, pid: str, svc=Depends(get_service)):
        return execute(svc, "get_graph", {"project_id": pid})

    for path, handler, declaration in (
            (FORGED_PATH, forged, ("read", "list_projects")),
            (HONEST_PATH, honest, ("read", "get_graph"))):
        setattr(handler, "_state_route", declaration)
        app.router.add_api_route(path, handler, methods=["GET"],
                                 dependencies=[Depends(guard)])
        app.router.routes.insert(0, app.router.routes.pop())


def _drop_forged_route(app):
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "path", None) not in (FORGED_PATH, HONEST_PATH)]


class TestProductAppObject:
    def test_forged_route_on_the_real_app_refuses_private_and_serves_open(
            self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        from api import main as main_module

        app = main_module.app
        db = DBManager(str(tmp_path / "assembly.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws"))
        _seed(StateService(db, ws, actor="assembly-seeder", project_read_trusted=True))
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws
        _forge_on_product_app(app)
        try:
            # `client=` puts the request on 127.0.0.1 so the real app's
            # localhost guard passes for the same reason production traffic
            # does; the app is NOT put in test mode, because test mode would
            # make `may_read_private` trusted and hide the whole question.
            client = TestClient(app, client=("127.0.0.1", 51000))

            private = client.get(FORGED_PATH, params={"pid": PRIVATE_PID})
            assert private.status_code == 403, (private.status_code, private.text[:200])
            assert PRIVATE_SECRET not in private.text

            # The lying declaration is refused on the OPENED project too: the
            # refusal is the declaration mismatch, not only the private read.
            lying = client.get(FORGED_PATH, params={"pid": OPEN_PID})
            assert lying.status_code == 403, (lying.status_code, lying.text[:200])
            assert OPEN_SECRET not in lying.text

            # Liveness control: an HONEST, genuinely project-gated public read
            # on the same app is refused while the project is unopened and
            # answers 200 once it is opened — so the refusals above are refusals
            # and not a route that never landed.
            honest_private = client.get(HONEST_PATH, params={"pid": PRIVATE_PID})
            assert honest_private.status_code == 403, honest_private.status_code
            assert PRIVATE_PID not in honest_private.text
            honest_live = client.get(HONEST_PATH, params={"pid": OPEN_PID})
            assert honest_live.status_code == 200, (honest_live.status_code,
                                                    honest_live.text[:200])
            assert OPEN_PID in honest_live.text
        finally:
            app.dependency_overrides.clear()
            _drop_forged_route(app)

    def test_closing_the_project_shuts_the_same_gate_again(self, monkeypatch, tmp_path):
        """Third pole: the gate is not a one-way latch."""
        _arm(monkeypatch)
        from api import main as main_module

        app = main_module.app
        db = DBManager(str(tmp_path / "assembly2.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws2"))
        seeder = StateService(db, ws, actor="assembly-seeder", project_read_trusted=True)
        _seed(seeder)
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws
        _forge_on_product_app(app)
        try:
            client = TestClient(app, client=("127.0.0.1", 51001))
            assert client.get(HONEST_PATH, params={"pid": OPEN_PID}).status_code == 200
            seeder.close_project(OPEN_PID)
            closed = client.get(HONEST_PATH, params={"pid": OPEN_PID})
            assert closed.status_code == 403, (closed.status_code, closed.text[:200])
            assert OPEN_SECRET not in closed.text
        finally:
            app.dependency_overrides.clear()
            _drop_forged_route(app)


class TestSecondAssemblyPointStateOnly:
    """`api.state_only.create_app` — the deployment assembled without the
    workflow runtime. Nothing here rides on the route guard: the ASGI bearer
    middleware refuses an anonymous request before routing."""

    def _app(self, tmp_path):
        from api.state_only import create_app

        db_path = str(tmp_path / "state-only.sqlite")
        _seed(StateService(StateDatabase(db_path), actor="state-only-seeder",
                           project_read_trusted=True))
        return create_app(db_path, STATE_ONLY_TOKEN, with_mcp=False)

    def test_anonymous_is_refused_and_the_token_reads_the_same_route(self, tmp_path):
        app = self._app(tmp_path)
        path = f"/api/state/projects/{PRIVATE_PID}/driver-note"

        def forged(request: Request):
            return execute(app.state.state_service, "get_driver_note",
                           {"project_id": request.query_params["pid"]})

        setattr(forged, "_state_route", ("read", "list_projects"))
        app.get(FORGED_PATH)(forged)
        with TestClient(app) as client:
            anonymous = client.get(path)
            assert anonymous.status_code == 401, (anonymous.status_code, anonymous.text[:200])
            assert PRIVATE_SECRET not in anonymous.text

            without_token = client.get(FORGED_PATH, params={"pid": PRIVATE_PID})
            assert without_token.status_code == 401, without_token.status_code
            assert PRIVATE_SECRET not in without_token.text

            headers = {"Authorization": f"Bearer {STATE_ONLY_TOKEN}"}
            live = client.get(FORGED_PATH, params={"pid": PRIVATE_PID}, headers=headers)
            assert live.status_code == 200, (live.status_code, live.text[:200])
            assert PRIVATE_SECRET in live.text

    def test_no_state_route_is_reachable_without_the_bearer_token(self, tmp_path):
        app = self._app(tmp_path)
        with TestClient(app) as client:
            for path in (f"/api/state/projects/{PRIVATE_PID}",
                         f"/api/state/projects/{PRIVATE_PID}/driver-note",
                         "/api/state/projects"):
                response = client.get(path)
                assert response.status_code == 401, (path, response.status_code)
            assert client.get("/health").status_code == 200
            assert client.get("/health").status_code == 200
