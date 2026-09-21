"""What an anonymous internet reader may read from the State DAG, and what stays shut.

The change this pins: `/api/state` used to hang `require_writer` on the WHOLE
router, so every read — the graph, the nodes, the evidence — was 403 to a
visitor with no credential, and the refusal answered a READ request with "sign
in with an authorized account to make changes".

Now each read is classified PUBLIC or PRIVATE in one table
(`core.state_commands.PUBLIC_READS`), and the default is PRIVATE. That default is
the important half: publishing is irreversible, and the next person to add a
read action will not read this file first. So an action nobody classified —
including one added later, and including a name that does not exist yet — is
refused for an anonymous caller.

`TestClassification` is the table. `TestAnonymousHttp` is the verdict, taken
through the real router with the gate armed. `TestRefusalWording` is the copy a
reader gets. `TestNoLeak` checks the refusal carries no note body.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, state_graph_routers as routes
from api.state_http import create_state_router
from core.state_commands import (PUBLIC_READS, READ_REQUESTS, WRITER_ONLY_READS,
                                 is_public_read, read_visibility)
import core.state_commands as state_commands
from core.state_database import StateDatabase
from core.state_service import StateService

# The owner's ruling of 2026-09-21 named these as staying private, by name.
NOTEBOOK_AND_MAILBOX = {
    "get_driver_note", "driver_note_history", "search_driver_note_history",
    "get_driver_note_entry", "check_driver_note_index", "driver_note_index",
    "list_director_messages",
}


class TestClassification:
    def test_every_read_action_is_classified_exactly_once(self):
        """The table is exhaustive over READ_REQUESTS — no action is unlabelled."""
        for action in READ_REQUESTS:
            assert read_visibility(action) in {"public", "private"}, action
        overlap = PUBLIC_READS & WRITER_ONLY_READS
        assert overlap == set(), overlap
        # Every named action is a real read action, so a typo cannot silently
        # classify nothing while the real action falls to the default.
        assert WRITER_ONLY_READS <= set(READ_REQUESTS)
        assert PUBLIC_READS <= set(READ_REQUESTS)

    def test_the_notebook_and_the_mailbox_are_private(self):
        for action in NOTEBOOK_AND_MAILBOX:
            assert action in READ_REQUESTS, action
            assert read_visibility(action) == "private", action
            assert is_public_read(action) is False, action

    @pytest.mark.parametrize("action", [
        "list_projects", "get_graph", "get_node", "project_overview",
        "project_catalog", "get_attempt", "list_attempts", "evidence",
        "attempt_detail", "list_issues", "get_issue", "frontier",
        "references", "project_attempts", "project_run_summary", "run_owners",
        "search_nodes", "design_catalog", "get_design_revision",
        "search_design_items", "design_impact", "get_design_baseline",
        "get_design_bindings", "export_design_markdown", "check_design_markdown",
    ])
    def test_the_graph_progress_and_design_are_public(self, action):
        assert action in READ_REQUESTS, action
        assert read_visibility(action) == "public", action

    def test_an_unclassified_read_is_private_by_default(self):
        """THE case this card exists for: a read added LATER is not published."""
        for unknown in ("a_read_action_added_next_year", "get_driver_note_v2",
                        "", "get_graph "):
            assert read_visibility(unknown) == "private", unknown
            assert is_public_read(unknown) is False, unknown

    def test_a_write_action_is_not_a_public_read(self):
        from core.state_commands import WRITE_REQUESTS
        for action in WRITE_REQUESTS:
            assert is_public_read(action) is False, action


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """Anonymous visitor: the gate is armed and nothing verifies."""
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                           actor="visibility-test")
    service.create_project("p", "P")
    service.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": [
        {"id": "c", "kind": "test", "description": "Check"}]}])
    service.driver_notes.update("p", "permanent", "IN-FLIGHT RUN ID 4711 SECRET",
                                0, "director")
    service.driver_notes.write_entry("p", "The withheld reason is SECRET-BODY",
                                     "Entry body SECRET-BODY", "director")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: service
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin-token")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    with TestClient(app) as client:
        yield client, service


class TestAnonymousHttp:
    """Two poles: the reading visitor gets in, the notebook does not."""

    def test_the_visitor_reads_the_graph(self, gated):
        client, _ = gated
        assert client.get("/api/state/projects").status_code == 200
        assert client.get("/api/state/projects/p").status_code == 200
        assert client.get("/api/state/projects/p/overview").status_code == 200
        assert client.get("/api/state/projects/p/nodes/a").status_code == 200
        assert client.get("/api/state/projects/p/frontier").status_code == 200
        assert client.get("/api/state/projects/p/issues").status_code == 200
        assert client.get("/api/state/projects/p/attempts").status_code == 200
        assert client.post("/api/state/query/get_graph",
                           json={"project_id": "p"}).status_code == 200
        assert client.post("/api/state/query/frontier",
                           json={"project_id": "p"}).status_code == 200

    def test_the_visitor_is_refused_the_notebook_and_the_mailbox(self, gated):
        client, _ = gated
        assert client.get("/api/state/projects/p/driver-note").status_code == 403
        assert client.get("/api/state/projects/p/driver-note/history").status_code == 403
        assert client.get(
            "/api/state/projects/p/driver-note/history/search").status_code == 403
        for action in NOTEBOOK_AND_MAILBOX:
            response = client.post("/api/state/query/" + action,
                                   json={"project_id": "p"})
            assert response.status_code == 403, action
        assert client.post(
            "/api/state/director-messages/list_director_messages",
            json={"project_id": "p"}).status_code == 403

    def test_an_unclassified_read_action_is_refused_anonymously(self, gated):
        """Default DENY, proved at the HTTP surface, not only in the table."""
        client, _ = gated
        response = client.post("/api/state/query/get_driver_note_v2",
                               json={"project_id": "p"})
        assert response.status_code == 403, response.text
        # A WRITE action posted at the READ surface is refused for the same
        # reason: the verdict is taken before the body is parsed, so an
        # anonymous caller cannot reach the 422 that classifies the action.
        assert client.post("/api/state/query/revise_node",
                           json={"project_id": "p"}).status_code == 403

    def test_writes_stay_refused_anonymously(self, gated):
        client, service = gated
        response = client.post("/api/state/commands/add_nodes",
                               json={"project_id": "p", "nodes": [{"key": "x",
                                     "goal": "X", "acceptance": []}]})
        assert response.status_code == 403
        assert "x" not in [n["node_key"] for n in service.store.get_graph("p")["nodes"]]

    def test_a_writer_still_reads_the_notebook_unchanged(self, gated, monkeypatch):
        client, _ = gated
        monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin-token")
        headers = {"X-AItelier-Admin-Token": "off-tunnel-admin-token"}
        note = client.get("/api/state/projects/p/driver-note", headers=headers)
        assert note.status_code == 200
        assert "IN-FLIGHT RUN ID 4711 SECRET" in note.text
        listed = client.post("/api/state/query/driver_note_index",
                             json={"project_id": "p"}, headers=headers)
        assert listed.status_code == 200


class TestRefusalWording:
    def test_a_read_refusal_talks_about_reading(self, gated):
        client, _ = gated
        response = client.get("/api/state/projects/p/driver-note")
        body = response.text
        # The bug this fixes: a read request answered "to make changes".
        assert "to make changes" not in body
        assert response.headers["X-AItelier-Denial"] == \
            authz.READ_DENIED_NOT_AUTHENTICATED
        assert json.loads(body)["detail"] == authz.DENIAL_MESSAGES[
            authz.READ_DENIED_NOT_AUTHENTICATED]

    def test_a_write_refusal_still_talks_about_changing(self, gated):
        client, _ = gated
        response = client.post("/api/state/commands/create_project",
                               json={"project_id": "q", "title": "Q"})
        assert response.status_code == 403
        assert response.json()["detail"] == authz.DENIAL_MESSAGES[
            authz.WRITE_DENIED_NOT_AUTHENTICATED]
        assert "X-AItelier-Denial" not in response.headers

    def test_a_bad_credential_reads_the_same_as_no_credential(self, gated):
        """Presenting a broken credential must not buy a better answer."""
        client, _ = gated
        anonymous = client.get("/api/state/projects/p/driver-note")
        broken = client.get("/api/state/projects/p/driver-note",
                            headers={"Cf-Ray": "abc",
                                     "Cf-Access-Jwt-Assertion": "not-a-jwt"})
        assert broken.status_code == anonymous.status_code == 403
        assert broken.json() == anonymous.json()
        public_broken = client.get("/api/state/projects",
                                   headers={"Cf-Ray": "abc",
                                            "Cf-Access-Jwt-Assertion": "not-a-jwt"})
        assert public_broken.status_code == 200


class TestNoLeak:
    """The verdict is the server's. Nothing private is sent for the SPA to hide."""

    def test_the_refusal_bodies_carry_no_notebook_text(self, gated):
        client, _ = gated
        responses = [
            client.get("/api/state/projects/p/driver-note"),
            client.get("/api/state/projects/p/driver-note/history"),
            client.post("/api/state/query/get_driver_note",
                        json={"project_id": "p"}),
            client.post("/api/state/query/driver_note_index",
                        json={"project_id": "p"}),
            client.post("/api/state/query/list_director_messages",
                        json={"project_id": "p"}),
        ]
        for response in responses:
            assert response.status_code == 403
            for secret in ("SECRET-BODY", "IN-FLIGHT RUN ID 4711 SECRET",
                           "withheld reason"):
                assert secret not in response.text, response.url


class TestEmbedderDefaults:
    def test_an_embedder_without_a_read_verdict_gets_no_MORE_than_the_public_reads(
            self, tmp_path, monkeypatch):
        """`create_state_router(service, access)` with one verdict: the PUBLIC
        reads are the public ones and the private reads fall back to that
        verdict. It can never open a private read by omission — the fallback is
        the writer verdict, and the read table is the only thing that decides
        which reads are public."""
        service = StateService(StateDatabase(str(tmp_path / "s.sqlite")), actor="e")
        service.create_project("p", "P")
        app = FastAPI()
        app.include_router(create_state_router(lambda: service, authz.require_writer))
        monkeypatch.setattr(authz, "gate_enabled", lambda: True)
        monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
        with TestClient(app) as client:
            assert client.get("/api/state/projects").status_code == 200
            assert client.get("/api/state/projects/p/driver-note").status_code == 403
            assert client.post("/api/state/query/driver_note_index",
                               json={"project_id": "p"}).status_code == 403
            assert client.post("/api/state/query/anything_added_later",
                               json={"project_id": "p"}).status_code == 403

    def test_the_state_only_deployment_keeps_its_own_bearer_refusal(self, tmp_path):
        """Its ASGI middleware refuses every request before either verdict runs."""
        from api.state_only import create_app
        app = create_app(str(tmp_path / "only.sqlite"), "y" * 40, with_mcp=False)
        with TestClient(app) as client:
            assert client.get("/api/state/projects").status_code == 401
            assert client.get("/api/state/projects/p/driver-note").status_code == 401
            headers = {"Authorization": "Bearer " + "y" * 40}
        assert client.get("/api/state/projects", headers=headers).status_code == 200


_PATH_VALUES = {"project_id": "p", "node_key": "a", "issue_id": "x",
                "attempt_id": "x", "run_id": "x"}


def _fill(path: str) -> str:
    for name, value in _PATH_VALUES.items():
        path = path.replace("{" + name + "}", value)
    return path


def _declared(route):
    """The read action a route declares on its own endpoint, or None."""
    return getattr(route.endpoint, "_state_route", None)


class TestTheVerdictIsDerivedNotRemembered:
    """A route's visibility must come from the read table, not from memory.

    The shape this replaces: 13 of the 20 GET routes answered an anonymous
    visitor because nobody had ATTACHED a dependency to them — public by
    omission — and the test meant to catch that asserted "not 403 unless the
    door is in a hardcoded PRIVATE_GET_DOORS set". Its default was PUBLIC.

    Now every route declares the read action it serves, ONE router-wide guard
    derives the class from `read_visibility`, and a route that declares nothing
    is refused. These tests fail if any of those three pieces is removed.
    """

    def test_every_state_route_declares_what_it_serves(self):
        declared = {route.path: _declared(route) for route in routes.router.routes}
        for path, declaration in declared.items():
            assert declaration is not None, ("undeclared route", path)
        # An empty or renamed router must fail this test, not pass it vacuously.
        assert len(declared) >= 20, sorted(declared)
        # `schema` is the fourth kind: `/schema` executes no state action, so it
        # declares that rather than a read of an action nothing could
        # cross-check it against. A kind outside this set has no branch in
        # the router guard and would fall through to the refusal.
        assert {kind for kind, _ in declared.values()} <= {"read", "write",
                                                           "director", "schema"}

    def test_a_reclassified_action_moves_EVERY_route_that_reaches_it(
            self, gated, monkeypatch):
        """Both poles on one action, including the route that had no guard.

        `frontier` is public today. Its GET route carried NO dependency, so it
        answered 200 only because nobody had looked; its POST door was already
        table-driven. Reclassifying the ACTION must move both — that is the
        difference between deriving the verdict and remembering to arm it.
        """
        client, _ = gated
        assert client.get("/api/state/projects/p/frontier").status_code == 200
        assert client.post("/api/state/query/frontier",
                           json={"project_id": "p"}).status_code == 200
        monkeypatch.setattr(state_commands, "PUBLIC_READS",
                            PUBLIC_READS - {"frontier"})
        assert read_visibility("frontier") == "private"
        assert is_public_read("frontier") is False
        post = client.post("/api/state/query/frontier", json={"project_id": "p"})
        get = client.get("/api/state/projects/p/frontier")
        assert post.status_code == 403, post.text
        assert get.status_code == 403, get.text

    def test_a_route_added_with_no_declaration_is_refused(self, gated):
        """THE case the card exists for, built the way a future author would.

        A new GET route on the same factory, reading the private notebook, with
        NO extra step taken — no dependency attached, no declaration. It must be
        refused for an anonymous caller, because the default has to be DENY.
        """
        _client, service = gated
        second = create_state_router(lambda: service, authz.require_writer,
                                     authz.require_reader)

        @second.get("/projects/{project_id}/undeclared-door")
        def undeclared_door(project_id: str):
            return {"notebook": "IN-FLIGHT RUN ID 4711 SECRET"}

        app = FastAPI()
        app.include_router(second)
        with TestClient(app) as undeclared_client:
            response = undeclared_client.get(
                "/api/state/projects/p/undeclared-door")
        assert response.status_code == 403, response.text
        assert "IN-FLIGHT RUN ID 4711 SECRET" not in response.text


class TestExhaustiveDoors:
    """EVERY door, not a sample. One leaked door is irreversible.

      POST ``/api/state/query/{action}`` — one door per ``READ_REQUESTS`` action.
      GET  every route on the state router, with its declared action read back
           and compared against the SAME table the server consults.

    The judgment is 403 / non-403, never 200: a 403 means the door refused; any
    other code (404, 409, 422) means the request reached the handler and the
    door let it through.
    """

    def test_every_read_action_is_refused_iff_it_is_private(self, gated):
        client, _ = gated
        probed = {}
        for action in sorted(READ_REQUESTS):
            response = client.post("/api/state/query/" + action,
                                   json={"project_id": "p"})
            probed[action] = response.status_code
            if is_public_read(action):
                assert response.status_code != 403, (action, response.text)
            else:
                assert response.status_code == 403, (action, response.text)
        # Coverage is the point: every action was probed, none skipped.
        assert set(probed) == set(READ_REQUESTS)

    def test_every_state_get_route_is_judged_by_its_own_declaration(self, gated):
        """The expectation is DERIVED, so a new private door cannot hide.

        No hardcoded list of private doors: each route's declared action is run
        through `is_public_read`, the same call the server's guard makes. An
        undeclared route expects 403. A future route that reaches a private
        action and forgets everything therefore fails HERE instead of passing.
        """
        client, _ = gated
        get_routes = [route for route in routes.router.routes
                      if "GET" in route.methods]
        assert len(get_routes) >= 16, [route.path for route in get_routes]
        probed = {}
        for route in get_routes:
            response = client.get(_fill(route.path))
            probed[route.path] = response.status_code
            declaration = _declared(route)
            assert declaration is not None, ("undeclared route", route.path)
            kind, action = declaration
            if kind == "read" and action is not None and is_public_read(action):
                assert response.status_code != 403, (route.path, response.text)
            else:
                assert response.status_code == 403, (route.path, response.text)
        assert set(probed) == {route.path for route in get_routes}

