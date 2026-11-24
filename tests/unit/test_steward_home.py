"""Plugin step 14, backend: the owner's memory requests (design-steward 3.4), the
driver role on /api/dsh-drivers, and aitelier-home as an ordinary private project
(design-steward 6): the anonymous identity cannot see it on any of the five read
surfaces the design lists, one assertion per surface.

HTTP runs the real app with the write gate ON and test mode OFF."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import ProjectPrivate, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService
from core.state_commands import STEWARD_WRITE_ACTIONS
from core.state_steward import EXPIRED, SCHEMA
from tests.support import state_template

HOME = "aitelier-home"
OWNER = "owner@example.com"
OWNER_HEADERS = {"X-Test-Email": OWNER}
HOME_TEXT = "home-only-goal-text-5c1f"
NODE = {"key": "n1", "goal": HOME_TEXT, "acceptance": [{"id": "a", "kind": "test", "description": "c"}]}


@pytest.fixture
def world(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, "steward-home-pepper-" * 3)
    registry.seed("steward-home-owner-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "opened", HOME):
        seeder.store.create_project(project, project.title())
        seeder.store.add_nodes(project, [NODE])
    seeder.open_project("opened")   # the control: an opened project IS visible anonymously
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    return type("World", (), dict(
        db=db, registry=registry, runtime=runtime, tokens=tokens,
        owner=StateService(db, actor="owner:" + OWNER, project_read_trusted=True),
        anonymous=StateService(db, actor="anonymous", project_read_trusted=False),
        steward=as_driver(steward), director=as_driver(director)))


@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def token(world, who):
    return {"X-AItelier-Driver-Token": world.tokens[who]}


# --- aitelier-home is private on every read surface ---------------------------------------

def test_anonymous_store_list_projects_omits_home(world):
    listed = {p["project_id"] for p in world.anonymous.store.list_projects()}
    assert "opened" in listed and HOME not in listed


def test_anonymous_portfolio_catalog_omits_home(world):
    catalog = json.dumps(execute(world.anonymous, "project_catalog", {}))
    assert '"opened"' in catalog and HOME not in catalog


def test_anonymous_project_overview_of_home_is_refused(world):
    assert execute(world.anonymous, "project_overview", {"project_id": "opened"})
    with pytest.raises(ProjectPrivate):
        execute(world.anonymous, "project_overview", {"project_id": HOME})


def test_anonymous_search_nodes_in_home_is_refused(world):
    assert HOME_TEXT in json.dumps(execute(world.anonymous, "search_nodes",
                                           {"project_id": "opened", "query": HOME_TEXT}))
    with pytest.raises(ProjectPrivate):
        execute(world.anonymous, "search_nodes", {"project_id": HOME, "query": HOME_TEXT})


def test_anonymous_direct_link_to_home_is_refused(world, client):
    assert client.get("/api/state/projects/opened").status_code == 200
    r = client.get(f"/api/state/projects/{HOME}")
    assert r.status_code == 403 and HOME_TEXT not in r.text
    # The owner opens the same link.
    assert client.get(f"/api/state/projects/{HOME}", headers=OWNER_HEADERS).status_code == 200


def test_dsh_driver_rows_carry_their_role(world, client):
    r = client.get("/api/dsh-drivers", headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    projects = r.json()["projects"]
    assert projects[HOME]["managed"]["role"] == "steward"
    assert projects["alpha"]["managed"]["role"] == "director"


# --- the owner's memory requests ----------------------------------------------------------

def create(client, kind="profile_read", payload=None, headers=OWNER_HEADERS):
    return client.post("/api/steward/memory-requests", json={"kind": kind, "payload": payload or {}},
                       headers=headers)


def test_a_memory_request_round_trip(world, client):
    r = create(client, "profile_update", {"text": "new profile", "expected_version": "abc", "reason": "edit"})
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]
    assert r.json() == {"request_id": request_id, "status": "pending"}
    # The steward's runtime is woken by a State event on its project, which names no payload.
    with world.db.get_connection() as conn:
        event = conn.execute("SELECT payload_json FROM state_events WHERE project_id=? AND "
                             "event_type='steward_memory_request_created'", (HOME,)).fetchone()
    assert event and json.loads(event[0])["request_id"] == request_id and "new profile" not in event[0]
    r = client.get(f"/api/steward/memory-requests/{request_id}", headers=OWNER_HEADERS)
    assert r.json() == {"request_id": request_id, "kind": "profile_update", "status": "pending", "result": None}
    # The runtime, with the steward's own token, over the State routes it may use.
    r = client.post("/api/state/query/list_steward_memory_requests", json={"status": "pending", "limit": 10},
                    headers=token(world, "steward"))
    assert r.status_code == 200, r.text
    listed = (r.json().get("result") or r.json())["requests"]
    assert listed == [{"request_id": request_id, "kind": "profile_update",
                       "payload": {"text": "new profile", "expected_version": "abc", "reason": "edit"}}]
    body = {"request_key": f"memory-request:{request_id}", "request_id": request_id,
            "result": {"version": "def", "text": "new profile"}}
    r = client.post("/api/state/commands/complete_steward_memory_request", json=body,
                    headers=token(world, "steward"))
    assert r.status_code == 200, r.text
    again = client.post("/api/state/commands/complete_steward_memory_request", json=body,
                        headers=token(world, "steward"))
    assert again.status_code == 200 and again.json() == r.json(), "a replay"
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT payload_json FROM state_steward_memory_requests WHERE request_id=?",
                            (request_id,)).fetchone()[0] == "{}", "the payload is cleared when done"
    r = client.get(f"/api/steward/memory-requests/{request_id}", headers=OWNER_HEADERS)
    assert r.json()["status"] == "done" and r.json()["result"] == {"version": "def", "text": "new profile"}
    # Read once: the profile text does not stay in the database.
    r = client.get(f"/api/steward/memory-requests/{request_id}", headers=OWNER_HEADERS)
    assert r.json()["result"] == EXPIRED
    with world.db.get_connection() as conn:
        assert "new profile" not in json.dumps([tuple(r) for r in conn.execute(
            "SELECT * FROM state_steward_memory_requests")])
        assert "new profile" not in json.dumps([tuple(r) for r in conn.execute(
            "SELECT * FROM driver_inbox_requests")]), "the idempotency ledger keeps a digest only"


def test_an_unread_result_expires_ten_minutes_after_completion(world):
    request_id = world.owner.steward.create_memory_request("profile_read", {})["request_id"]
    execute(world.steward, "complete_steward_memory_request",
            {"request_key": "k", "request_id": request_id, "result": {"text": "profile"}}, allow_write=True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_memory_requests SET completed_at=?", (old,))
        conn.commit()
    # Any later touch of the table sweeps it, here another owner request.
    world.owner.steward.create_memory_request("forgotten_list", {})
    with world.db.get_connection() as conn:
        assert json.loads(conn.execute("SELECT result_json FROM state_steward_memory_requests WHERE "
                                       "request_id=?", (request_id,)).fetchone()[0]) == EXPIRED
    assert world.owner.steward.read_memory_request(request_id)["result"] == EXPIRED


def test_only_the_owner_creates_and_reads_memory_requests(world, client):
    request_id = create(client).json()["request_id"]
    for who, code in (("director", "owner_session_required"), ("steward", "steward_read_only")):
        r = create(client, headers=token(world, who))
        assert r.status_code == 403 and code in r.text, (who, r.text)
    for who in ("director", "steward"):
        r = client.get(f"/api/steward/memory-requests/{request_id}", headers=token(world, who))
        assert r.status_code == 403 and "owner_session_required" in r.text, r.text
    assert create(client, headers={}).status_code == 403
    assert client.get(f"/api/steward/memory-requests/{request_id}").status_code == 403
    assert create(client, kind="drop_everything").status_code == 422
    assert create(client, kind="restore", payload={"ids": ["x"]}).status_code == 422


def test_only_the_home_steward_lists_and_completes(world):
    request_id = world.owner.steward.create_memory_request("forget", {"ids": ["m1"], "reason": "r"})["request_id"]
    for service in (world.director, world.owner):
        with pytest.raises(StateGraphError, match="steward_role_required"):
            execute(service, "list_steward_memory_requests", {})
        with pytest.raises(StateGraphError, match="steward_role_required"):
            execute(service, "complete_steward_memory_request", {"project_id": HOME, "request_key": "k",
                    "request_id": request_id, "result": {}}, allow_write=True)
    with pytest.raises(ProjectPrivate):
        execute(world.anonymous, "list_steward_memory_requests", {})
    assert [r["request_id"] for r in execute(world.steward, "list_steward_memory_requests", {})["requests"]] \
        == [request_id]
    execute(world.steward, "complete_steward_memory_request",
            {"request_key": "k", "request_id": request_id, "result": {"forgotten": ["m1"]}}, allow_write=True)
    assert execute(world.steward, "list_steward_memory_requests", {})["requests"] == []
    with pytest.raises(StateGraphError, match="memory_request_not_pending"):
        execute(world.steward, "complete_steward_memory_request",
                {"request_key": "k2", "request_id": request_id, "result": {}}, allow_write=True)


# --- the dashboard's 清空媒体 (design-steward 7.8.12 "保留 30 天") ----------------------------------

def test_media_clear_is_one_owner_request_the_steward_host_runs(world, client):
    assert "steward_media_clear" not in STEWARD_WRITE_ACTIONS
    r = client.post("/api/steward/media/clear?request_key=click-1", headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]
    assert r.json() == {"request_id": request_id, "status": "pending"}
    again = client.post("/api/steward/media/clear?request_key=click-1", headers=OWNER_HEADERS)
    assert again.json() == r.json(), "the same click is one request"
    # The State command is the same door, for the owner session alone.
    other = execute(world.owner, "steward_media_clear", {"request_key": "click-2"}, allow_write=True)
    assert other["request_id"] != request_id and other["status"] == "pending"
    with world.db.get_connection() as conn:
        events = [json.loads(e[0]) for e in conn.execute(
            "SELECT payload_json FROM state_events WHERE project_id=? AND "
            "event_type='steward_memory_request_created'", (HOME,))]
    assert {(e["request_id"], e["kind"]) for e in events} == {(request_id, "media_clear"),
                                                              (other["request_id"], "media_clear")}
    listed = execute(world.steward, "list_steward_memory_requests", {})["requests"]
    assert listed == [{"request_id": request_id, "kind": "media_clear", "payload": {}},
                      {"request_id": other["request_id"], "kind": "media_clear", "payload": {}}]
    execute(world.steward, "complete_steward_memory_request",
            {"request_key": "m", "request_id": request_id, "result": {"cleared": True, "removed": 3}},
            allow_write=True)
    r = client.get(f"/api/steward/memory-requests/{request_id}", headers=OWNER_HEADERS)
    assert r.json() == {"request_id": request_id, "kind": "media_clear", "status": "done",
                        "result": {"cleared": True, "removed": 3}}


def test_media_clear_is_the_owner_sessions_alone(world, client):
    for service, code in ((world.steward, "steward_read_only"), (world.director, "owner_session_required"),
                          (world.anonymous, "owner_session_required")):
        with pytest.raises(StateGraphError, match=code):
            execute(service, "steward_media_clear", {"request_key": "k"}, allow_write=True)
    for headers, code in ((token(world, "director"), "owner_session_required"),
                          (token(world, "steward"), "steward_read_only")):
        r = client.post("/api/steward/media/clear", headers=headers)
        assert r.status_code == 403 and code in r.text, r.text
    assert client.post("/api/steward/media/clear").status_code == 403
    # Its one door: the general memory-request route does not take the kind.
    assert create(client, kind="media_clear").status_code == 422
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_steward_memory_requests").fetchone()[0] == 0


def test_a_media_clear_the_host_did_not_take_in_two_minutes_lapses(world):
    stale = world.owner.steward.request_media_clear("k1")["request_id"]
    fresh_memory = world.owner.steward.create_memory_request("profile_read", {})["request_id"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_memory_requests SET created_at=?", (old,))
        conn.commit()
    fresh = world.owner.steward.request_media_clear("k2")["request_id"]
    listed = [r["request_id"] for r in execute(world.steward, "list_steward_memory_requests", {})["requests"]]
    assert listed == [fresh_memory, fresh], "an old memory request still runs; an old media clear does not"
    assert world.owner.steward.read_memory_request(stale) == {
        "request_id": stale, "kind": "media_clear", "status": "done", "result": {"refused": "expired"}}


def test_an_older_memory_requests_table_is_migrated_for_media_clear(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old = SCHEMA.replace(",'purge','media_clear')", ",'purge')", 1)
    assert old != SCHEMA
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
        conn.execute("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,created_at,"
                     "created_by) VALUES('memreq-a','forget','{\"ids\": [\"x\"]}','pending','t','owner:o')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,created_at,"
                         "created_by) VALUES('memreq-b','media_clear','{}','pending','t','owner:o')")
    StateService(StateDatabase(path), actor="fixture", project_read_trusted=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT request_id,kind,payload_json FROM state_steward_memory_requests").fetchall() \
            == [("memreq-a", "forget", '{"ids": ["x"]}')]
        migrated = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_steward_memory_requests'").fetchone()[0]
        assert "'media_clear'" in migrated
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_steward_memory_requests_status'").fetchone()
        conn.execute("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,created_at,"
                     "created_by) VALUES('memreq-b','media_clear','{}','pending','t','owner:o')")
        conn.commit()
    StateService(StateDatabase(path), actor="fixture", project_read_trusted=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_steward_memory_requests").fetchone()[0] == 2
