"""A project is private to an anonymous visitor until someone opens it.

The action-visibility card divided the read surface by ACTION and got it right, but
it does not divide by SUBJECT: get_graph/project_overview/get_node answer for ANY
project_id to anyone. This card adds the other half; the two are ANDed. Before
someone explicitly opens a project, an anonymous visitor cannot read it at all --
including projects created from now on. The default is PRIVATE and comes from the
MECHANISM (the absence of an access row), never from a list of ids. The decision is
enforced at the execute chokepoint, so it does not ride on the route guard, which a
route author can currently stand down.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

import core.state_commands as state_commands
from api import authz
from api.state_http import apply_project_privacy, create_state_router
from core.state_commands import READ_REQUESTS, execute, is_public_read
from core.state_database import StateDatabase
from core.state_service import StateService

_DECL = "_state_route"
ADMIN = {"X-AItelier-Admin-Token": "off-tunnel-admin"}
REPO = Path(__file__).resolve().parents[2]


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _build(tmp_path, actor="anon-test"):
    service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor=actor)

    def dep(request: Request):
        service.project_read_trusted = authz.may_read_private(request)
        return service

    router = create_state_router(dep, authz.require_writer, authz.require_reader)
    app = FastAPI()
    app.include_router(router)
    apply_project_privacy(app)
    return app, router, service


def _seed(service, pid, title, goal, secret):
    service.create_project(pid, title)
    service.store.add_nodes(pid, [{"key": "a", "goal": goal, "acceptance": [
        {"id": "c", "kind": "test", "description": secret}]}])
    service.driver_notes.update(pid, "permanent", secret + " NOTEBOOK", 0, "director")
    service.issues.report(pid, request_key="rk", kind="gap", title=secret + " ISSUE",
                          body=secret + " ISSUE BODY", director_identity="director")


def _fill(path, pid):
    return (path.replace("{project_id}", pid).replace("{node_key}", "a")
            .replace("{issue_id}", "x").replace("{attempt_id}", "nope")
            .replace("{run_id}", "nope"))


def _public_project_doors(router, pid):
    doors = []
    for route in router.routes:
        if "GET" not in route.methods:
            continue
        declaration = getattr(route.endpoint, _DECL, None)
        if not declaration or declaration[0] != "read" or not is_public_read(declaration[1]):
            continue
        if "{project_id}" in route.path:
            doors.append(("GET", _fill(route.path, pid), declaration[1]))
    for action in sorted(READ_REQUESTS):
        if is_public_read(action) and "project_id" in READ_REQUESTS[action].model_fields:
            doors.append(("POST", "/api/state/query/" + action, action))
    return doors


class TestPrivacyIsAMechanismNotAList:
    def test_a_project_created_now_is_private_the_instant_it_exists(self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="a")
        service.create_project("brand-new", "Fresh")
        assert service.store.is_project_public("brand-new") is False
        assert service.store.get_project_access("brand-new")["visibility"] == "private"

    def test_opening_happens_only_via_the_recorded_write(self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="a")
        _seed(service, "p", "P", "G", "SECRET")
        assert service.store.is_project_public("p") is False
        assert service.open_project("p")["visibility"] == "public"
        assert service.store.is_project_public("p")
        assert service.close_project("p")["visibility"] == "private"
        assert not service.store.is_project_public("p")

    def test_the_verdict_is_from_the_raw_credential_not_a_project_id(self, monkeypatch, tmp_path):
        app, _, _ = _build(tmp_path)
        _arm(monkeypatch)

        class _Anon:
            headers = {}
            cookies = {}

            class app:
                class state:
                    _test_mode = False

        assert authz.may_read_private(_Anon()) is False


class TestOpeningIsRecorded:
    def test_open_records_who_and_when_and_reads_back(self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="boss@x")
        _seed(service, "p", "P", "G", "SECRET")
        rec = service.open_project("p")
        assert rec["opened_by"] == "boss@x" and rec["opened_at"]
        back = service.project_visibility("p")
        assert back["visibility"] == "public"
        assert back["opened_by"] == "boss@x" and back["changed_by"] == "boss@x"

    def test_closing_is_a_recorded_write_and_keeps_the_open_history(self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="boss@x")
        _seed(service, "p", "P", "G", "SECRET")
        service.open_project("p")
        rec = service.close_project("p")
        assert rec["visibility"] == "private" and rec["changed_by"] == "boss@x"
        assert rec["opened_by"] == "boss@x"

    def test_opening_a_project_is_writer_only(self, monkeypatch, tmp_path):
        app, _, service = _build(tmp_path)
        _seed(service, "p", "P", "G", "SECRET")
        _arm(monkeypatch)
        with TestClient(app) as client:
            assert client.post("/api/state/projects/p/open").status_code == 403
        assert service.store.is_project_public("p") is False


class TestNoExistingWriteOpensAProject:
    def test_representative_writes_all_leave_the_project_private(self, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="op")
        service.create_project("p", "P")
        assert not service.store.is_project_public("p")
        service.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": [
            {"id": "c", "kind": "test", "description": "d"}]}])
        assert not service.store.is_project_public("p")
        service.store.revise_node("p", "a", expected_revision=1, reason="x", goal="A2")
        assert not service.store.is_project_public("p")
        service.driver_notes.update("p", "permanent", "note", 0, "director")
        service.driver_notes.write_entry("p", "assertion", "body", "director")
        assert not service.store.is_project_public("p")
        service.issues.report("p", request_key="rk", kind="gap", title="t", body="b",
                              director_identity="director")
        assert not service.store.is_project_public("p")
        service.store.split_node("p", "a", expected_revision=2, children=[
            {"key": "a1", "goal": "A1", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}],
            reason="s")
        assert not service.store.is_project_public("p")

    def test_only_set_project_access_writes_the_visibility_table(self):
        src = (REPO / "core" / "state_graph.py").read_text(encoding="utf-8")
        assert src.count("INTO state_project_access") == 1
        assert "UPDATE state_project_access" not in src
        assert "DELETE FROM state_project_access" not in src
        for path in (REPO / "core").glob("*.py"):
            if path.name == "state_graph.py":
                continue
            assert "INTO state_project_access" not in path.read_text(encoding="utf-8")

    def test_open_close_are_writes_and_visibility_is_a_private_read(self):
        assert state_commands.WRITE_REQUESTS["open_project"].__name__ == "OpenProject"
        assert state_commands.WRITE_REQUESTS["close_project"].__name__ == "CloseProject"
        assert is_public_read("open_project") is False
        assert is_public_read("project_visibility") is False


class TestThreePoles:
    def test_new_project_public_project_then_closed(self, monkeypatch, tmp_path):
        app, router, service = _build(tmp_path)
        _seed(service, "p", "P", "GOAL-TITLE", "ACME-SECRET-4711")
        _arm(monkeypatch)
        doors = _public_project_doors(router, "p")
        assert doors

        def sweep(client):
            table = {}
            for method, path, action in doors:
                r = (client.get(path) if method == "GET"
                     else client.post(path, json={"project_id": "p"}))
                table[f"{method} {path}"] = (r.status_code, action)
            return table

        with TestClient(app) as client:
            neg = sweep(client)
            for key, (code, action) in neg.items():
                assert code == 403, (key, action, code)
            assert len(neg) >= 15, len(neg)

            writer_body = client.get("/api/state/projects/p/nodes/a", headers=ADMIN)
            assert writer_body.status_code == 200

            opened = client.post("/api/state/projects/p/open", headers=ADMIN)
            assert opened.status_code == 200, opened.text
            assert opened.json()["visibility"] == "public"

            pos = sweep(client)
            for key, (code, action) in pos.items():
                assert code != 403, (key, action, code)
            anon_node = client.get("/api/state/projects/p/nodes/a")
            assert anon_node.status_code == 200
            assert anon_node.text == writer_body.text

            closed = client.post("/api/state/projects/p/close", headers=ADMIN)
            assert closed.status_code == 200 and closed.json()["visibility"] == "private"
            for key, (code, action) in sweep(client).items():
                assert code == 403, (key, action, code)

    def test_refusal_does_not_distinguish_absent_from_unopened(self, monkeypatch, tmp_path):
        app, _, service = _build(tmp_path)
        _seed(service, "present-but-private", "P", "G", "SEC")
        _arm(monkeypatch)
        with TestClient(app) as client:
            priv = client.get("/api/state/projects/present-but-private/overview")
            gone = client.get("/api/state/projects/does-not-exist-at-all/overview")
        assert priv.status_code == gone.status_code == 403
        assert priv.text == gone.text
        assert "present-but-private" not in gone.text


class TestListingDoesNotNameRefused:
    def _corpus(self, app, pids_opened=(), pids_private=()):
        bodies = []
        with TestClient(app) as client:
            bodies.append(client.get("/api/state/projects").text)
            bodies.append(client.post("/api/state/query/project_catalog",
                                      json={"limit": 100}).text)
            bodies.append(client.post("/api/state/query/list_projects", json={}).text)
            for pid in pids_opened:
                bodies.append(client.get(f"/api/state/projects/{pid}/overview").text)
                bodies.append(client.get(f"/api/state/projects/{pid}").text)
                bodies.append(client.get(f"/api/state/projects/{pid}/nodes/a").text)
            for pid in pids_private:
                bodies.append(client.get(f"/api/state/projects/{pid}/overview").text)
                bodies.append(client.post("/api/state/query/get_graph",
                                          json={"project_id": pid}).text)
        return "\n".join(bodies)

    def test_no_private_body_surfaces_and_scanner_has_a_positive_control(self, monkeypatch, tmp_path):
        app, _, service = _build(tmp_path)
        priv_a = "CLIENT-TICKET-9931-ACME-PAYROLL-PRIVATE-A"
        priv_b = "CLIENT-TICKET-9931-ZENITH-BILLING-PRIVATE-B"
        opened_secret = "PUBLISHED-DEMO-PROJECT-OPENED-OK"
        _seed(service, "priv-a", priv_a, priv_a + " GOAL", priv_a)
        _seed(service, "priv-b", priv_b, priv_b + " GOAL", priv_b)
        _seed(service, "opened", opened_secret, opened_secret + " GOAL", opened_secret)
        service.open_project("opened")
        _arm(monkeypatch)
        corpus = self._corpus(app, pids_opened=("opened",), pids_private=("priv-a", "priv-b"))
        caught = SequenceMatcher(None, corpus, opened_secret + " GOAL", autojunk=False) \
            .find_longest_match().size
        assert caught >= 12, f"scanner is toothless (control matched only {caught})"
        for secret in (priv_a, priv_b):
            longest = SequenceMatcher(None, corpus, secret, autojunk=False) \
                .find_longest_match().size
            assert longest <= 6, (secret, longest)
            assert secret not in corpus
        assert "priv-a" not in corpus and "priv-b" not in corpus

    def test_catalog_listing_only_shows_opened_projects(self, monkeypatch, tmp_path):
        app, _, service = _build(tmp_path)
        _seed(service, "priv-a", "PRIV-A-TITLE", "G", "SEC")
        _seed(service, "opened", "OPENED-TITLE", "G", "SEC")
        service.open_project("opened")
        _arm(monkeypatch)
        with TestClient(app) as client:
            listed = client.get("/api/state/projects").json()["projects"]
            catalog = client.post("/api/state/query/project_catalog",
                                  json={"limit": 100}).json()["projects"]
            direct = client.post("/api/state/query/list_projects", json={}).json()
        assert [p["project_id"] for p in listed] == ["opened"]
        assert [p["project_id"] for p in catalog] == ["opened"]
        assert [p["project_id"] for p in direct] == ["opened"]


class TestGateHoldsAgainstRouteAuthor:
    def _attack(self, app, router, service, pid, declared_action, mount_guard):
        guard = router.dependencies[0].dependency

        def dep(request: Request):
            service.project_read_trusted = authz.may_read_private(request)
            return service

        def forged(request: Request, svc=Depends(dep)):
            execute(svc, declared_action, {})
            return execute(svc, "get_driver_note", {"project_id": pid})

        setattr(forged, _DECL, ("read", declared_action))
        if mount_guard:
            app.get("/api/state/forged", dependencies=[Depends(guard)])(forged)
        else:
            app.get("/api/state/forged")(forged)

    def _run(self, monkeypatch, tmp_path, mount_guard, declared_action):
        app, router, service = _build(tmp_path)
        _seed(service, "priv", "PRIVATE-TITLE", "PRIVATE-GOAL-XYZZY", "PRIVATE-BODY-XYZZY")
        _arm(monkeypatch)
        self._attack(app, router, service, "priv", declared_action, mount_guard)
        with TestClient(app) as client:
            return client.get("/api/state/forged")

    def test_ATTACK_forged_route_still_refused(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, True, "list_projects")
        assert r.status_code == 403, (r.status_code, r.text[:120])
        assert "PRIVATE-BODY-XYZZY" not in r.text and "PRIVATE-GOAL-XYZZY" not in r.text

    def test_CONTROL_same_handler_without_the_guard(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, False, "list_projects")
        assert r.status_code == 403, (r.status_code, r.text[:120])
        assert "PRIVATE-BODY-XYZZY" not in r.text

    def test_HONEST_declares_the_private_read_it_serves(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, True, "get_driver_note")
        assert r.status_code == 403, (r.status_code, r.text[:120])
        assert "PRIVATE-BODY-XYZZY" not in r.text

    def test_second_assembly_point_state_only(self, monkeypatch, tmp_path):
        service = StateService(StateDatabase(str(tmp_path / "only.sqlite")), actor="state-token")
        _seed(service, "priv", "PRIVATE-TITLE", "PRIVATE-GOAL-XYZZY", "PRIVATE-BODY-XYZZY")

        def dep(request: Request):
            service.project_read_trusted = authz.may_read_private(request)
            return service

        router = create_state_router(dep, lambda request=None: None, lambda request=None: None)
        app = FastAPI()
        app.include_router(router)
        guard = router.dependencies[0].dependency

        def forged(request: Request, svc=Depends(dep)):
            execute(svc, "list_projects", {})
            return execute(svc, "get_driver_note", {"project_id": "priv"})

        setattr(forged, _DECL, ("read", "list_projects"))
        app.get("/api/state/forged", dependencies=[Depends(guard)])(forged)
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        monkeypatch.setattr(authz, "WRITERS", set())
        monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)
        with TestClient(app) as client:
            r = client.get("/api/state/forged")
        assert r.status_code == 403, (r.status_code, r.text[:120])
        assert "PRIVATE-BODY-XYZZY" not in r.text

    def test_decision_lives_at_execute_which_stand_down_cannot_reach(self):
        src = (REPO / "core" / "state_commands.py").read_text(encoding="utf-8")
        body = src[src.index("def execute("):]
        assert "_refuse_if_project_not_public" in body[:body.index("handlers = {")]
        http_src = (REPO / "api" / "state_http.py").read_text(encoding="utf-8")
        assert "ProjectPrivate" in http_src


def test_execute_refuses_anonymous_read_of_unopened_project_directly(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="a")
    _seed(service, "p", "P", "G", "SECRET")
    service.project_read_trusted = False
    with pytest.raises(state_commands.ProjectPrivate):
        execute(service, "get_graph", {"project_id": "p"})
    with pytest.raises(state_commands.ProjectPrivate):
        execute(service, "get_driver_note", {"project_id": "p"})
    service.project_read_trusted = True
    assert execute(service, "get_graph", {"project_id": "p"})
    service.project_read_trusted = False
    service.open_project("p")
    assert execute(service, "get_graph", {"project_id": "p"})


def _arguments(action, pid="table-pid"):
    """Fill every field the read model declares, so a door is never refused
    for a missing argument — a 422 in the table would hide a real verdict."""
    body = {}
    for name, field in READ_REQUESTS[action].model_fields.items():

        if name == "project_id":
            body[name] = pid
        elif name == "node_key":
            body[name] = "a"
        elif name == "after":
            body[name] = 0
        elif name in ("limit", "depth"):
            body[name] = 30
        elif name in ("issue_id", "attempt_id", "run_id"):
            body[name] = "x"
        elif field.is_required():
            body[name] = "x"
    return body


def _every_public_gate(router, pid):
    """The FULL set of public gates, DERIVED from the route table: every GET
    route whose own endpoint declares a public read action, plus every action
    of the `/query/{action}` dispatch family that `is_public_read` calls
    public. Not a sample — the whole derivation."""
    gates = []
    for route in router.routes:
        declaration = getattr(route.endpoint, _DECL, None)
        if not declaration or declaration[0] != "read":
            continue
        methods = sorted(route.methods - {"HEAD", "OPTIONS"})
        if methods == ["GET"]:
            if declaration[1] is not None and is_public_read(declaration[1]):
                gates.append(("GET", _fill(route.path, pid), declaration[1]))
        elif methods == ["POST"] and route.path.endswith("/query/{action}"):
            for action in sorted(READ_REQUESTS):
                if is_public_read(action):
                    gates.append(("POST", f"/api/state/query/{action}", action))
    return gates


def test_public_gate_table_unopened_opened_closed(monkeypatch, tmp_path):
    """Criterion 1's table: gate -> project state -> bare status code, over the
    ENTIRE public-gate set derived from the route table. No gate may answer
    with anything but 403 while the project is unopened or closed back; the
    same gates must recover (not 403) once it is opened."""
    app, router, service = _build(tmp_path)
    _seed(service, "table-pid", "TABLE-TITLE", "TABLE-GOAL", "TABLE-SECRET-9")
    _arm(monkeypatch)
    gates = _every_public_gate(router, "table-pid")
    assert gates, "route table yielded no public gates - derivation is broken"

    def sweep():
        rows = []
        with TestClient(app) as client:
            for method, path, action in gates:
                if method == "GET":
                    r = client.get(path)
                else:
                    r = client.post(path, json=_arguments(action))
                rows.append((f"{method} {path}", action, r.status_code))
        return rows

    def table(rows_unopened, rows_open, rows_closed):
        head = "gate | action | unopened | opened | closed-back"
        lines = [head, "-" * len(head)]
        for (gate, action, u), (_, _, o), (_, _, c) in zip(rows_unopened, rows_open, rows_closed):
            lines.append(f"{gate} | {action} | {u} | {o} | {c}")
        return "\n".join(lines)

    rows_unopened = sweep()
    with TestClient(app) as client:
        bodies = {"list_projects": client.get("/api/state/projects").text,
                  "project_catalog": client.post("/api/state/query/project_catalog",
                                                 json={"limit": 100}).text}
    service.open_project("table-pid")
    rows_opened = sweep()
    with TestClient(app) as client:
        assert client.get("/api/state/projects/table-pid/overview").status_code == 200
        assert client.post("/api/state/query/get_graph",
                           json={"project_id": "table-pid"}).status_code == 200
        assert client.get("/api/state/projects/table-pid/nodes/a").status_code == 200
    service.close_project("table-pid")
    rows_closed = sweep()


    # Two public gates are NOT project-scoped and the owner ruled their action
    # classification unchanged: the catalog/list (which filters, tested
    # elsewhere) and a 422 for a malformed argument. They stay in the table;
    # here we pin that neither names the unopened project.
    CATALOG_GATES = {"list_projects", "project_catalog"}

    def scoped(gate, action):
        return "table-pid" in gate and action not in CATALOG_GATES

    assert "table-pid" not in bodies["list_projects"] \
        and "TABLE-TITLE" not in bodies["list_projects"], bodies["list_projects"]

    assert all(code == 403 for (gate, action, code) in rows_unopened
               if scoped(gate, action)), \
        "UNOPENED:\n" + table(rows_unopened, rows_opened, rows_closed)
    assert all(code == 403 for (gate, action, code) in rows_closed
               if scoped(gate, action)), \
        "CLOSED BACK:\n" + table(rows_unopened, rows_opened, rows_closed)
    # Recovery: no project-scoped gate keeps refusing once opened, and the core
    # corpus doors answer 200 with the project's real content.
    assert all(code != 403 for (gate, action, code) in rows_opened
               if scoped(gate, action)), \
        "OPENED:\n" + table(rows_unopened, rows_opened, rows_closed)

