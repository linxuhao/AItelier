"""Plugin step 12: the server enforces "the steward is read-only".

Two places, both a named RESTRICTION on a driver whose `capabilities.role` is
`steward` (never a grant):

* `core.state_commands.execute`'s write branch: only the steward whitelist
  passes; a driver bound to the reserved project without the steward role, or
  whose capabilities cannot be read, is refused (fail closed);
* `api.authz.write_denial_reason`: a non-GET/HEAD request with a steward token
  passes only for the State command/query families and the steward's OWN
  completion/heartbeat/activity; everything else is `steward_read_only`.

The HTTP tests run the real app with the write gate ON and test mode OFF, and
authenticate with the real driver tokens through the registry, never through a
test-mode bypass or a dependency override of the verdict.
"""
from __future__ import annotations

import copy
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, dsh_driver
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, load_roles
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

HOME = "aitelier-home"
PEPPER = "steward-fixture-pepper-" * 3
OWNER_TOKEN = "steward-fixture-owner-token-0123456789"
STEWARD = {"route": "driver", "reasoning_effort": "high", "compaction": {"thresholdRatio": 0.4},
           "prompt": "steward.md", "presented": ["ask_owner", "close_question"],
           "forbidden": ["subagent"], "presented_only": True}
NODE = {"key": "n1", "goal": "fixture goal",
        "acceptance": [{"id": "a", "kind": "test", "description": "fixture criterion"}]}


def _refusal(action):
    return f"steward_read_only: {action} is not available to the steward role"


@pytest.fixture
def world(tmp_path, monkeypatch):
    table = copy.deepcopy(load_roles())
    table["steward"] = copy.deepcopy(STEWARD)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps(table))
    monkeypatch.setattr(dsh_driver, "ROLES_FILE", roles)
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed(OWNER_TOKEN)
    owner = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "beta", HOME):  # none of them is opened: all private
        owner.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def service(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    def set_capabilities(row, text):
        with db.get_connection() as conn:
            conn.execute("UPDATE drivers SET capabilities_json=? WHERE driver_id=?", (text, row["driver_id"]))
            conn.commit()

    return type("World", (), dict(db=db, registry=registry, runtime=runtime, steward=steward,
                                  director=director, tokens=tokens, service=service,
                                  set_capabilities=set_capabilities, owner=owner))


def _project_ids(world):
    with world.db.get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT project_id FROM state_projects")}


# --- execute: the write whitelist ---------------------------------------------------------

REFUSED = {
    "add_nodes": {"project_id": HOME, "nodes": [NODE]},
    "claim_node": {"project_id": "alpha", "node_key": "n1"},
    "start_attempt": {"project_id": "alpha", "node_key": "n1"},
    "create_project": {"project_id": "gamma", "title": "Gamma"},
}


@pytest.mark.parametrize("action", sorted(REFUSED))
def test_the_steward_is_refused_every_write_outside_the_whitelist(world, action):
    with pytest.raises(StateGraphError) as refused:
        execute(world.service(world.steward), action, REFUSED[action], allow_write=True)
    assert str(refused.value) == _refusal(action)
    assert "gamma" not in _project_ids(world)


def test_the_whitelisted_writes_pass_the_steward_gate(world):
    steward = world.service(world.steward)
    sid = world.steward["driver_id"]
    sent = execute(steward, "send_user_message", {"request_key": "k1", "project_id": HOME,
                                                  "subject": "hello", "body": "fixture"}, allow_write=True)
    assert sent["sender_driver_id"] == sid
    # Its own notebooks: the private one and its bound project's.
    for scope in ({"driver_id": sid}, {"project_id": HOME}):
        written = execute(steward, "write_driver_note_entry", {**scope, "assertion": "rule one",
                          "body": "fixture body", "director_identity": sid}, allow_write=True)
        assert written
    # These reach their own validation: the steward gate let them through.
    for action, arguments in (
            ("withdraw_user_question", {"request_key": "k2", "project_id": HOME, "message_id": "m",
                                        "segment_id": "s", "reason": "fixture"}),
            ("supersede_driver_note_entry", {"driver_id": sid, "entry_id": "0" * 12, "reason": "r",
                                             "assertion": "a", "body": "b", "director_identity": sid}),
            ("delist_driver_note_entry", {"project_id": HOME, "entry_id": "0" * 12, "reason": "r",
                                          "director_identity": sid}),
            ("send_director_message", {"project_id": "alpha", "subject": "handoff", "body": "x"})):
        try:
            execute(steward, action, arguments, allow_write=True)
        except StateGraphError as exc:
            assert not str(exc).startswith(("steward_", "driver_capabilities")), (action, exc)


def test_the_steward_writes_no_other_notebook(world):
    steward = world.service(world.steward)
    sid = world.steward["driver_id"]
    for scope in ({"project_id": "alpha"}, {"driver_id": world.director["driver_id"]}):
        with pytest.raises(StateGraphError, match=re.escape(_refusal("write_driver_note_entry"))):
            execute(steward, "write_driver_note_entry", {**scope, "assertion": "a", "body": "b",
                                                         "director_identity": sid}, allow_write=True)


def test_proposal_actions_are_named_for_step_13():
    from core.state_commands import STEWARD_WRITE_ACTIONS
    assert {"create_steward_proposal", "withdraw_steward_proposal"} <= STEWARD_WRITE_ACTIONS


def test_a_director_is_unchanged(world):
    director = world.service(world.director)
    assert execute(director, "add_nodes", {"project_id": "alpha", "nodes": [NODE]}, allow_write=True)
    assert execute(director, "create_project", {"project_id": "gamma", "title": "Gamma"}, allow_write=True)
    # A registered driver with no capabilities at all (not DSH-managed) is ordinary too.
    world.registry.register("plain", "Plain", actor="fixture")
    plain = StateService(world.db, actor="driver:plain", driver_id="plain", project_read_trusted=True)
    assert execute(plain, "add_nodes", {"project_id": "beta", "nodes": [NODE]}, allow_write=True)


@pytest.mark.parametrize("capabilities, error", [
    (json.dumps({"runtime": "dsh", "project_id": HOME}), "steward_role_missing"),
    (json.dumps({"runtime": "dsh", "project_id": HOME, "role": "director"}), "steward_role_missing"),
    ("{}", "steward_role_missing"),
    ("{not json", "driver_capabilities_unreadable"),
    ("[]", "driver_capabilities_unreadable"),
])
def test_the_home_driver_fails_closed(world, capabilities, error):
    world.set_capabilities(world.steward, capabilities)
    steward = world.service(world.steward)
    for action, arguments in (("send_user_message", {"request_key": "k", "project_id": HOME,
                                                     "subject": "s", "body": "b"}),
                              ("add_nodes", REFUSED["add_nodes"])):
        with pytest.raises(StateGraphError) as refused:
            execute(steward, action, arguments, allow_write=True)
        assert str(refused.value) == error
    # Reads are not judged here.
    assert execute(steward, "list_projects", {})


# --- HTTP: the real app, gate on, test mode off, real tokens -------------------------------

@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            # After startup, so the lifespan still takes its test-mode path.
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled() and app.state._test_mode is False
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def _headers(world, who):
    return {"X-AItelier-Driver-Token": world.tokens[who]}


RUNTIME_BODIES = {"completion": {"session_id": "s", "messages": []}, "heartbeat": None,
                  "activity": {"phase": "running"}}
_PARAM = re.compile(r"{([^}:]+)(?::[^}]+)?}")


def _walk(routes, prefix=""):
    """(path, route) for every route, descending into included routers (this
    FastAPI keeps them as wrappers around the original router)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _walk(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
            continue
        yield prefix + (getattr(route, "path", "") or ""), route


def _write_routes():
    """Every (method, path template) of the app other than GET/HEAD. A route that
    takes any method (the bare /mcp route) is taken as POST. Mounts (the SPA's
    static files) and websocket routes carry no methods and are skipped."""
    out = set()
    for path, route in _walk(app.routes):
        methods = getattr(route, "methods", set())
        if methods is None:
            methods = {"POST"}
        out |= {(method, path) for method in methods - {"GET", "HEAD"}}
    return sorted(out)


def _fill(path, project):
    return _PARAM.sub(lambda m: project if m.group(1) == "project_id" else "x1", path)


def _klass(path):
    if path.startswith(("/api/state/commands/", "/api/state/query/")):
        return "state"
    if re.fullmatch(r"/api/dsh-drivers/\{project_id\}/(completion|heartbeat|activity)", path):
        return "runtime"
    return "other"


def test_the_enumeration_sees_every_kind_of_write_route():
    routes = _write_routes()
    assert len(routes) > 50
    paths = {path for _, path in routes}
    for expected in ("/api/state/commands/{action}", "/api/state/query/{action}", "/mcp",
                     "/api/dsh-drivers/{project_id}/start", "/api/dsh-drivers/{project_id}/stop",
                     "/api/dsh-drivers/{project_id}/work", "/api/dsh-drivers/{project_id}/completion",
                     "/api/dsh-drivers/{project_id}/heartbeat", "/api/dsh-drivers/{project_id}/activity"):
        assert expected in paths, expected


def test_every_write_route_with_a_steward_token(world, client):
    """Three classes: State command/query pass the gate (execute judges the
    command again), the steward's own runtime endpoints pass, everything else is
    steward_read_only - including every other project's runtime endpoints, every
    start/stop/work and MCP. A write route added later falls in the last class."""
    before = {row["project_id"]: (row["enabled"], row["phase"]) for row in world.runtime.rows()}
    headers = _headers(world, "steward")
    for method, template in _write_routes():
        klass = _klass(template)
        if klass == "state":
            action = "add_nodes" if "/commands/" in template else "list_projects"
            arguments = REFUSED["add_nodes"] if action == "add_nodes" else {}
            r = client.request(method, template.replace("{action}", action), json=arguments, headers=headers)
            if action == "add_nodes":
                assert r.status_code == 422 and r.json()["detail"] == _refusal("add_nodes"), (template, r.text)
            else:
                assert r.status_code == 200, (template, r.text)
            continue
        if klass == "runtime":
            endpoint = template.rsplit("/", 1)[1]
            r = client.request(method, _fill(template, HOME), json=RUNTIME_BODIES[endpoint], headers=headers)
            # Past both gates (this one and owned()): the handler answers that the
            # steward's runtime is stopped.
            assert r.status_code == 409 and r.json()["detail"] == "driver is stopped", (template, r.text)
        r = client.request(method, _fill(template, "alpha"), json={}, headers=headers)
        assert r.status_code == 403, (method, template, r.status_code, r.text)
        assert "steward_read_only" in r.text, (method, template, r.text)
    # The director-messaging REST path answers a denial of its four actions with its
    # closed v2 envelope (api/main.py write_gate), whatever the denial code.
    r = client.post("/api/state/director-messages/send_director_message", json={}, headers=headers)
    assert r.status_code == 403 and "unauthorized" in r.text, r.text
    after = {row["project_id"]: (row["enabled"], row["phase"]) for row in world.runtime.rows()}
    assert after == before, "no driver was started or stopped"
    assert _project_ids(world) == {"alpha", "beta", HOME}


def _request(method, path, token):
    return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                    "query_string": b"", "scheme": "http", "server": ("testserver", 80),
                    "headers": [(b"x-aitelier-driver-token", token.encode())]})


def test_the_same_enumeration_with_an_ordinary_driver_token_is_unchanged(world, monkeypatch):
    """An ordinary driver's off-tunnel token authorizes every write route, as it
    did before the steward existed. Judged at the verdict every guard shares
    (write_denial_reason), so no handler runs with a token that may write."""
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    owner_cli = OWNER_TOKEN
    for method, template in _write_routes():
        for project in ("alpha", HOME, "beta"):
            path = _fill(template, project)
            for token in (world.tokens["director"], owner_cli):
                assert authz.write_denial_reason(_request(method, path, token)) is None, (method, path)
            steward = authz.write_denial_reason(_request(method, path, world.tokens["steward"]))
            expected = None if _klass(template) == "state" or (
                _klass(template) == "runtime" and project == HOME) else authz.STEWARD_READ_ONLY
            assert steward == expected, (method, path, steward)
        # GET/HEAD are never narrowed.
        for safe in ("GET", "HEAD"):
            assert authz.write_denial_reason(_request(safe, _fill(template, "alpha"), world.tokens["steward"])) is None


def test_the_stewards_own_runtime_endpoints_succeed_when_it_runs(world, client, monkeypatch):
    world.runtime.update(HOME, enabled=1, phase="running")
    monkeypatch.setattr(world.runtime, "running", lambda project_id: project_id == HOME)
    headers = _headers(world, "steward")
    r = client.post(f"/api/dsh-drivers/{HOME}/heartbeat", headers=headers)
    assert r.status_code == 200 and r.json() == {"results": []}, r.text
    r = client.post(f"/api/dsh-drivers/{HOME}/activity", json={"phase": "waiting"}, headers=headers)
    assert r.status_code == 200 and world.runtime.get(HOME)["phase"] == "waiting", r.text
    # Another project's, even a running one, stays refused.
    world.runtime.update("alpha", enabled=1, phase="running")
    monkeypatch.setattr(world.runtime, "running", lambda project_id: True)
    r = client.post("/api/dsh-drivers/alpha/heartbeat", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "steward_read_only"


def test_steward_reads_still_work_and_private_projects_stay_readable(world, client):
    headers = _headers(world, "steward")
    assert client.get("/api/dsh-drivers", headers=headers).status_code == 200
    r = client.get("/api/dsh-drivers/beta/conversation", headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/state/query/list_user_conversation", json={"project_id": "beta"}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/state/query/project_overview", json={"project_id": "beta"}, headers=headers)
    assert r.status_code == 200 and '"beta"' in r.text, r.text
    assert client.get("/api/drivers/me", headers=headers).json()["driver_id"] == world.steward["driver_id"]


def test_the_steward_reads_no_other_drivers_inbox(world, client):
    headers = _headers(world, "steward")
    r = client.post("/api/state/query/list_driver_messages",
                    json={"driver_id": world.director["driver_id"]}, headers=headers)
    assert r.status_code == 422 and "not_inbox_recipient" in r.json()["detail"], r.text


def test_whitelisted_commands_work_over_http_and_refused_ones_do_not(world, client):
    headers = _headers(world, "steward")
    r = client.post("/api/state/commands/send_user_message", headers=headers,
                    json={"request_key": "k", "project_id": HOME, "subject": "s", "body": "b"})
    assert r.status_code == 200 and r.json()["sender_driver_id"] == world.steward["driver_id"], r.text
    for action in sorted(REFUSED):
        r = client.post(f"/api/state/commands/{action}", json=REFUSED[action], headers=headers)
        assert r.status_code == 422 and r.json()["detail"] == _refusal(action), (action, r.text)


def test_the_home_driver_without_its_role_is_refused_over_http(world, client):
    world.set_capabilities(world.steward, json.dumps({"runtime": "dsh", "project_id": HOME}))
    r = client.post("/api/state/commands/send_user_message", headers=_headers(world, "steward"),
                    json={"request_key": "k", "project_id": HOME, "subject": "s", "body": "b"})
    assert r.status_code == 422 and r.json()["detail"] == "steward_role_missing", r.text


def test_an_ordinary_driver_over_http_is_unchanged(world, client):
    headers = _headers(world, "director")
    r = client.post("/api/state/commands/add_nodes", json={"project_id": "alpha", "nodes": [NODE]},
                    headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/dsh-drivers/alpha/heartbeat", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "driver is stopped", r.text
    # The runtime endpoint of another project is refused by owned(), not by the gate.
    r = client.post(f"/api/dsh-drivers/{HOME}/heartbeat", headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == "this runtime belongs to another driver", r.text
