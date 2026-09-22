"""The confidentiality verdict runs where the private read EXECUTES.

From a route's source one cannot decide what a handler delivers - that is
static analysis of arbitrary Python, and eight rounds of widening the reader
each found a shape it could not see. So the decision lives on the ACTION: the
one classification table (`read_visibility`, unknown -> private) plus one
refusal function (`core.state_privacy.refuse_private_read`) called from the
shared `execute` chokepoint AND from every service method a route could reach
without calling `execute`.

This module ENUMERATES the paths from the repository's own tables rather than
from a hand-written list: `WRITER_ONLY_READS` names the actions, and
`core.state_commands._handlers` names the callable each action executes. For
every such action it drives both the `execute` path and the direct-method path,
and it drives a route that bypasses `execute` on a fresh app and on
`api.main.app`.
"""
from __future__ import annotations

import asyncio
import inspect

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import core.state_commands as state_commands
import core.state_privacy as state_privacy
from api import authz
from core.state_commands import (READ_REQUESTS, WRITER_ONLY_READS, ProjectPrivate,
                                 execute, read_visibility)
from core.state_database import StateDatabase
from core.state_service import StateService

PROJECT = "execpoint-public"
SECRET = "EXECUTION-POINT-SECRET-3f2a"

# Minimal arguments per writer-only action. The verdict runs before the
# arguments are validated, so these only have to name the project. The keys are
# asserted against `WRITER_ONLY_READS`, so a writer-only read added later cannot
# slip past this module without someone choosing its arguments.
_ARGS = {
    "get_driver_note": {"project_id": PROJECT},
    "driver_note_history": {"project_id": PROJECT},
    "search_driver_note_history": {"project_id": PROJECT},
    "get_driver_note_entry": {"project_id": PROJECT, "entry_id": "note:0"},
    "check_driver_note_index": {"project_id": PROJECT},
    "driver_note_index": {"project_id": PROJECT},
    "list_director_messages": {"project_id": PROJECT},
    "get_driver_guide_section": {"address": "guide://driver-loop"},
    "events": {"project_id": PROJECT},
    "wait_for_state_change": {"project_id": PROJECT},
    "project_visibility": {"project_id": PROJECT},
}


def _args(action: str) -> dict:
    return dict(_ARGS[action])


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _seed(service):
    service.create_project(PROJECT, PROJECT)
    service.driver_notes.update(PROJECT, "permanent", SECRET, 0, "director")
    # PUBLIC project: the project gate is wide open, so only the ACTION stays
    # private. This is the shape the review measured leaking.
    service.open_project(PROJECT)


def _trusted(tmp_path, name="execpoint.sqlite"):
    service = StateService(StateDatabase(str(tmp_path / name)), actor="seeder",
                           project_read_trusted=True)
    _seed(service)
    return service


def _anonymous(service):
    """Mark a service - and the objects it handed its level to - anonymous."""
    service.project_read_trusted = False
    service.store.project_read_trusted = False
    service.driver_notes.project_read_trusted = False
    service.director_messages.project_read_trusted = False
    return service


def _drive_bypass(target, action):
    """Call the callable the action maps to DIRECTLY, without `execute`."""
    owner = getattr(target, "__self__", None)
    if owner is None:
        return None
    method = getattr(owner, target.__name__)
    result = method(**_args(action))
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _drive_execute(service, action):
    """Drive the shared chokepoint, awaiting a read that is a coroutine."""
    result = execute(service, action, _args(action))
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


class TestEveryPathIsJudgedAtExecution:
    def test_the_argument_table_covers_every_writer_only_read(self):
        assert set(_ARGS) == set(WRITER_ONLY_READS)
        for action in WRITER_ONLY_READS:
            assert action in READ_REQUESTS, action
            assert read_visibility(action) == "private", action

    def test_every_writer_only_read_is_refused_through_execute(self, tmp_path):
        service = _anonymous(_trusted(tmp_path))
        refused = []
        for action in sorted(WRITER_ONLY_READS):
            try:
                _drive_execute(service, action)
            except ProjectPrivate:
                refused.append(action)
        assert refused == sorted(WRITER_ONLY_READS), (
            "these writer-only reads were NOT refused through execute: "
            f"{sorted(set(WRITER_ONLY_READS) - set(refused))}")

    def test_every_writer_only_read_is_refused_when_called_directly(self, tmp_path):
        """A route that never calls `execute` gets the SAME refusal.

        The bypass callable is DERIVED: it is the very bound object `execute`
        would dispatch to, so a new writer-only read appears here automatically.
        """
        service = _anonymous(_trusted(tmp_path))
        targets = state_commands._handlers(service)
        refused, derived = [], []
        for action in sorted(WRITER_ONLY_READS):
            target = targets[action]
            if getattr(target, "__state_read_action__", None) == action:
                derived.append(action)
            try:
                _drive_bypass(target, action)
            except ProjectPrivate:
                refused.append(action)
        # The driver-guide section takes no service object, so it has no direct
        # call to guard; it is reachable only through `execute`, which refuses
        # it (proved by the `execute` test above).
        by_execute_only = {"get_driver_guide_section"}
        assert set(refused) == set(WRITER_ONLY_READS) - by_execute_only, (
            "these writer-only reads were NOT refused when called directly: "
            f"{sorted((set(WRITER_ONLY_READS) - by_execute_only) - set(refused))}")
        assert set(derived) == set(WRITER_ONLY_READS) - by_execute_only, (
            "these writer-only reads carry no execution-point verdict: "
            f"{sorted((set(WRITER_ONLY_READS) - by_execute_only) - set(derived))}")

    def test_a_public_read_and_a_trusted_read_are_unaffected(self, tmp_path):
        anonymous = _anonymous(_trusted(tmp_path))
        graph = execute(anonymous, "get_graph", {"project_id": PROJECT})
        assert "nodes" in graph
        trusted = _trusted(tmp_path, "execpoint-trusted.sqlite")
        note = execute(trusted, "get_driver_note", {"project_id": PROJECT})
        assert SECRET in note["permanent"]


class TestTheRefusalReachesTheTransport:
    def test_confidentiality_does_not_depend_on_the_route_reader(
            self, tmp_path, monkeypatch):
        """Neuter the route-layer reader entirely and the refusal still fires.

        The guard is given a declaration it approves (`binding_for` forced to
        `ok`), so nothing at the route layer stops the handler; the handler
        bypasses `execute` and reads the notebook from the service object. The
        403 can only come from the execution point.
        """
        _arm(monkeypatch)
        import api.state_graph_routers as sgr
        import api.state_http as state_http
        from api.state_verdict import Binding

        monkeypatch.setattr(state_http, "binding_for",
                            lambda *a, **k: Binding(True, "", frozenset()))
        service = _anonymous(_trusted(tmp_path))
        app = FastAPI()
        app.include_router(sgr.router)
        app.dependency_overrides[sgr.get_service] = lambda: service
        guard = sgr.router.dependencies[0].dependency

        def bypass(pid: str, svc=Depends(sgr.get_service)):
            return svc.driver_notes.get(pid)

        setattr(bypass, "_state_route", ("read", "get_graph"))
        path = "/api/state/reader-neutered/{pid}"
        app.router.add_api_route(path, bypass, methods=["GET"],
                                 dependencies=[Depends(guard)])
        app.router.routes.insert(0, app.router.routes.pop())
        with TestClient(app) as client:
            response = client.get(path.replace("{pid}", PROJECT))
        assert response.status_code == 403, (response.status_code, response.text[:200])
        assert SECRET not in response.text

    def test_a_fresh_app_refuses_a_direct_method_route(self, tmp_path):
        """No guard, no `execute`: a handler that returns the notebook itself."""
        service = _anonymous(_trusted(tmp_path))
        app = FastAPI()

        @app.get("/bypass/{pid}")
        def bypass(pid: str):
            return service.driver_notes.get(pid)

        with TestClient(app) as client:
            response = client.get(f"/bypass/{PROJECT}")
        assert response.status_code == 403, (response.status_code, response.text[:200])
        assert SECRET not in response.text

    def test_the_product_app_refuses_a_direct_method_route(self, tmp_path, monkeypatch):
        """The same bypass on `api.main.app`, with the REAL per-request service."""
        _arm(monkeypatch)
        from api import main as main_module
        from api.dependencies import get_db_manager, get_workspace_manager
        from api.state_graph_routers import get_service
        from core.db_manager import DBManager
        from core.workspace_manager import WorkspaceManager

        app = main_module.app
        db = DBManager(str(tmp_path / "product.sqlite"))
        ws = WorkspaceManager(str(tmp_path / "ws"))
        _seed(StateService(db, ws, actor="seeder", project_read_trusted=True))
        app.dependency_overrides[get_db_manager] = lambda: db
        app.dependency_overrides[get_workspace_manager] = lambda: ws

        def bypass(pid: str, svc=Depends(get_service)):
            # The reviewer's "attribute call" shape: the private read is obtained
            # from the service object, never through `execute`.
            return svc.driver_notes.get(pid)

        path = "/api/state/execpoint-bypass/{pid}"
        app.router.add_api_route(path, bypass, methods=["GET"])
        app.router.routes.insert(0, app.router.routes.pop())
        try:
            client = TestClient(app, client=("127.0.0.1", 51100))
            response = client.get(path.replace("{pid}", PROJECT))
            assert response.status_code == 403, (response.status_code, response.text[:200])
            assert SECRET not in response.text
        finally:
            app.dependency_overrides.clear()
            app.router.routes = [r for r in app.router.routes
                                 if getattr(r, "path", None) != path]


class TestTheVerdictHasTeeth:
    def test_removing_the_one_verdict_leaks_and_the_probe_names_the_actions(
            self, tmp_path, monkeypatch):
        """Neutralize `refuse_private_read` and watch the paths open."""
        service = _anonymous(_trusted(tmp_path))
        monkeypatch.setattr(state_privacy, "refuse_private_read",
                            lambda trusted, action: None)
        leaked = []
        for action in sorted(WRITER_ONLY_READS):
            try:
                _drive_execute(service, action)
            except ProjectPrivate:
                continue
            except Exception:  # a bogus argument is not a leak
                continue
            leaked.append(action)
        print("IGNITION_COUNT =", len(leaked), "LEAKED =", leaked)
        assert len(leaked) > 0, "removing the verdict left every path shut"
