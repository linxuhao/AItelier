"""rev 3 additions to the project-privacy card.

Criterion 3 (rev 3): the SHAPE of a response is also body. The r1 candidate
scanned bodies for ids, stayed clean, and still leaked — `project_catalog` cut
the page before dropping unopened projects, so an unopened project turned a
whole page empty and shifted every later cursor; a reviewer reconstructed the
unopened id character by character in 863 anonymous calls. The property pinned
here: responses are byte-identical whether or not the database also contains
projects this caller may not know exist. Plus the usability half: anonymous
paging must still REACH every visible project no matter how many private ones
stand in front of it.

Criterion 4 (rev 3): trust must be DECLARED. `StateService` without an explicit
`project_read_trusted` refuses to construct (the S7 carrier: a route author who
writes `StateService(get_db_manager(), get_workspace_manager())` — verbatim
`api/state_graph_tools.py:23` in the rejected base — must not get a trusted
service by forgetting). And the MCP private-read whitelist is coupled by test:
removing a line from it goes red.
"""
from __future__ import annotations

import re
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api import authz
from api.state_http import apply_project_privacy, create_state_router
from core.state_database import StateDatabase
from core.state_graph import StateGraphStore
from core.state_portfolio import StatePortfolio
from core.state_service import StateService
from tests.support.state_author_surface import seed_private_mail

REPO = Path(__file__).resolve().parents[2]
ADMIN = {"X-AItelier-Admin-Token": "off-tunnel-admin"}


def _arm(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def _app(tmp_path, name, actor="anon-rev3"):
    service = StateService(StateDatabase(str(tmp_path / f"{name}.sqlite")),
                           actor=actor, project_read_trusted=True)

    def dep(request: Request):
        service.project_read_trusted = authz.may_read_private(request)
        return service

    app = FastAPI()
    app.include_router(create_state_router(dep, authz.require_writer, authz.require_reader))
    apply_project_privacy(app)
    return app, service


def _seed(service, pid, title, secret):
    service.create_project(pid, title)
    service.store.add_nodes(pid, [{"key": "a", "goal": secret + " GOAL", "acceptance": [
        {"id": "c", "kind": "test", "description": secret}]}])
    service.driver_notes.update(pid, "permanent", secret + " NOTEBOOK", 0, "director")


def _normalize(body, *tmp_dirs):
    """Normalize ONLY what cannot matter: tmp filesystem paths (the two
    databases live in different directories) and wall-clock timestamps (the two
    databases are seeded milliseconds apart). Nothing else is normalized."""
    for d in tmp_dirs:
        body = body.replace(str(d), "<TMP>")
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
                  "<TS>", body)


class TestPageShapeIsAlsoBody:
    def test_anonymous_responses_identical_with_and_without_private_projects(
            self, monkeypatch, tmp_path):
        _arm(monkeypatch)
        app_a, svc_a = _app(tmp_path, "with-hidden")
        app_b, svc_b = _app(tmp_path, "without-hidden")
        # DB A: three UNOPENED projects sort BEFORE the visible one; DB B: they
        # do not exist at all. The visible project is seeded identically.
        for pid, title in [("aaa-hidden-alpha", "Hidden Alpha"),
                           ("bbb-hidden-beta", "Hidden Beta"),
                           ("ccc-hidden-gamma", "Hidden Gamma")]:
            _seed(svc_a, pid, title, f"SECRET-{pid.upper()}")
        _seed(svc_a, "zzz-public", "Published Demo", "PUBLISHED-BODY-4711")
        _seed(svc_b, "zzz-public", "Published Demo", "PUBLISHED-BODY-4711")
        svc_a.open_project("zzz-public")
        svc_b.open_project("zzz-public")

        def page_all(client, limit):
            pages, after = [], ""
            while True:
                r = client.post("/api/state/query/project_catalog",
                                json={"after": after, "limit": limit})
                assert r.status_code == 200, r.text
                body = r.json()
                pages.append(body)
                if body["next_after"] is None:
                    return pages
                after = body["next_after"]
                assert len(pages) < 50, "catalog paging does not terminate"

        with TestClient(app_a) as ca, TestClient(app_b) as cb:
            pairs = []
            for limit in (1, 2, 100):
                a = page_all(ca, limit)
                b = page_all(cb, limit)
                assert len(a) == len(b), (f"limit={limit}: page COUNT differs", a, b)
                pairs.append((f"catalog limit={limit}",
                              [_normalize(json.dumps(p), tmp_path) for p in a],
                              [_normalize(json.dumps(p), tmp_path) for p in b]))
                ra = ca.get("/api/state/projects")
                rb = cb.get("/api/state/projects")
                pairs.append((f"list_projects limit={limit}",
                              [_normalize(ra.text, tmp_path)],
                              [_normalize(rb.text, tmp_path)]))
        import json as _json
        for label, norm_a, norm_b in pairs:
            assert _json.dumps(norm_a) == _json.dumps(norm_b), (
                f"anonymous response SHAPE depends on projects the caller must not "
                f"know exist: {label}\nA={norm_a}\nB={norm_b}")

    def test_anonymous_paging_reaches_visible_projects_behind_private_ones(
            self, monkeypatch, tmp_path):
        """Usability half: >= limit unopened projects stand in front of the one
        visible project, and anonymous paging still reaches it."""
        _arm(monkeypatch)
        app, service = _app(tmp_path, "blocked")
        for i in range(3):
            _seed(service, f"aaa-hidden-{i}", f"Hidden {i}", f"SECRET-{i}")
        _seed(service, "zzz-public", "Published Demo", "PUBLISHED-BODY-4711")
        service.open_project("zzz-public")
        seen = []
        with TestClient(app) as client:
            after = ""
            while True:
                body = client.post("/api/state/query/project_catalog",
                                   json={"after": after, "limit": 1}).json()
                seen.extend(p["project_id"] for p in body["projects"])
                if body["next_after"] is None:
                    break
                after = body["next_after"]
        assert seen == ["zzz-public"], seen

    def test_the_filter_lives_in_the_sql_not_after_the_page(self):
        from core import state_portfolio
        src = (REPO / "core" / "state_portfolio.py").read_text(encoding="utf-8")
        assert "state_project_access" in src, "visibility must be part of the SQL"


class TestTrustMustBeDeclared:
    def test_a_service_that_never_declares_its_trust_level_is_not_constructed(
            self, tmp_path):
        with pytest.raises(TypeError):
            StateService(StateDatabase(str(tmp_path / "s.sqlite")))

    def test_the_s7_two_line_shape_fails_at_construction(self, tmp_path):
        """Verbatim `api/state_graph_tools.py:23` of the rejected base:
        StateService(get_db_manager(), get_workspace_manager()) — no Depends, no
        declaration. It must fail HERE, not leak at read time."""
        db = StateDatabase(str(tmp_path / "s7.sqlite"))
        ws = None
        with pytest.raises(TypeError):
            StateService(db, ws)  # exactly the S7 shape: no trust declared

    def test_the_mcp_face_declares_trust_explicitly(self):
        src = (REPO / "api" / "state_graph_tools.py").read_text(encoding="utf-8")
        assert "project_read_trusted=" in src, (
            "the MCP service construction must declare its trust level explicitly")


class TestSecondCarrierS7:
    def test_self_constructed_service_route_is_refused_private_and_serves_open(
            self, monkeypatch, tmp_path):
        """A route author who builds their OWN service (no Depends(get_service),
        no forged declaration) cannot read a writer-only action at all: the
        verdict follows the ACTION, so the OPENED project is refused too. The
        liveness control is the SAME route executing a PUBLIC read, which answers
        200 on the opened project with the real body - so the refusals are
        refusals and not a route that never landed."""
        _arm(monkeypatch)
        app, service = _app(tmp_path, "s7-route")
        _seed(service, "priv", "Private", "S7-PRIVATE-BODY-XYZZY")
        _seed(service, "open-pid", "Open", "S7-OPEN-BODY-XYZZY")
        # An opened project's notebook is public (owner ruling 2026-09-22), so
        # the writer-only read this route attempts is the director mailbox.
        seed_private_mail(service, "priv", "S7-PRIVATE-BODY-XYZZY")
        seed_private_mail(service, "open-pid", "S7-OPEN-BODY-XYZZY")
        service.open_project("open-pid")
        db = service.db

        def self_built(request: Request):
            # the route author's own construction, trust DECLARED from the
            # request credential — the only way a service can exist at all now
            rogue = StateService(db, project_read_trusted=authz.may_read_private(request))
            from core.state_commands import execute
            return execute(rogue, "list_director_messages",
                           {"project_id": request.query_params["pid"]})

        def self_built_public(request: Request):
            # the liveness control: the same construction, a PUBLIC read
            rogue = StateService(db, project_read_trusted=authz.may_read_private(request))
            from core.state_commands import execute
            return execute(rogue, "get_graph", {"project_id": request.query_params["pid"]})

        app.get("/api/state/self-built")(self_built)
        app.get("/api/state/self-built-public")(self_built_public)
        with TestClient(app) as client:
            private = client.get("/api/state/self-built?pid=priv")
            assert private.status_code == 403, (private.status_code, private.text[:200])
            assert "S7-PRIVATE-BODY-XYZZY" not in private.text
            opened = client.get("/api/state/self-built?pid=open-pid")
            assert opened.status_code == 403, (opened.status_code, opened.text[:200])
            assert "S7-OPEN-BODY-XYZZY" not in opened.text
            live = client.get("/api/state/self-built-public?pid=open-pid")
            assert live.status_code == 200, (live.status_code, live.text[:200])
            assert "nodes" in live.text

    def test_a_portfolio_with_no_declared_trust_fails_closed(self, tmp_path):
        db = StateDatabase(str(tmp_path / "pf.sqlite"))
        seeder = StateGraphStore(db, project_read_trusted=True)   # the fixture's own
        seeder.create_project("hidden", "Hidden")
        seeder.create_project("shown", "Shown")
        seeder.set_project_access("shown", "public", "boss")
        store = StateGraphStore(db)                                 # declares nothing
        catalog = StatePortfolio(store).projects(limit=100)   # no owning service
        assert [p["project_id"] for p in catalog["projects"]] == ["shown"]


class TestMcpPrivateReadWhitelistIsCoupled:
    def test_every_state_serving_read_tool_is_whitelisted(self):
        """`_PRIVATE_READ_TOOLS` decides which MCP read tools demand writer
        authorization. Deriving the expectation from the tool registrations
        themselves: any `read` tool registered by the state graph tools module
        that is missing from the frozenset must fail THIS test — so deleting
        `state_graph_read` from the whitelist goes red naming it, and a new read
        tool added there cannot silently bypass the writer gate."""
        import api.mcp_router as mcp_router
        src = (REPO / "api" / "state_graph_tools.py").read_text(encoding="utf-8")
        # A read tool must be whitelisted exactly when its body can execute a
        # state action (i.e. return project state); `state_graph_help`, which
        # only serves schemas, stays public. Each segment is cut at the next
        # decorator so a body is never contaminated by the tools after it.
        segments = re.split(r'\n    @', src)
        serving = []
        for seg in segments[1:]:
            m = re.match(r'tool\("([^"]+)",\s*"read"', seg)
            if m and re.search(r'\b(invoke|execute)\b', seg[m.end():]):
                serving.append(m.group(1))
        assert serving, "whitelist derivation found no state read tools - parser is broken"
        missing = [name for name in serving
                   if name not in mcp_router._PRIVATE_READ_TOOLS]
        assert not missing, (
            f"state read tools {missing} are NOT in mcp_router._PRIVATE_READ_TOOLS: "
            f"on that transport the project privacy surface is inert for them")
