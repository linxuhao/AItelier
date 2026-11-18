"""The State project-archive capability (iss-174f3100129b457f), on its three criteria.

Two writes (`archive_project`, `unarchive_project`), one archive state (the
`state_project_archive` row), one filtered list (`list_projects`, and the same
predicate in the trusted paged catalog). Archiving keeps EVERY row: it is a
listing decision recorded beside visibility, never a delete and never a
publication change.

Each case below is executed against a real isolated SQLite database and the
product's own write/read paths (`core.state_commands.execute`, and the real
`/api/state` router over a TestClient where a refusal has to be OBSERVED rather
than asserted about a mock):

a) `test_archive_hides_and_restores_the_owner_list` - the trusted list stops
   returning the project after one archive write, every row survives, the
   project still resolves, and the unarchive write restores the earlier list.
b) `test_a_driver_credential_is_refused_by_the_archive_door` and
   `test_the_anonymous_list_is_untouched_by_archiving` - the door belongs to the
   owner, the refusal names the action, and the public surface is byte-identical.
c) `test_the_change_migrates_a_database_written_by_the_previous_code` - a
   database built by the PREVIOUS code (the base commit's own tree, extracted
   with `git archive` and run as a subprocess) keeps every row count, every
   project row and the exact project list after the new code applies its
   additive column, twice.
d) `test_the_catalog_reports_which_rows_are_archived` - the project catalog
   ACTION and the `/api/state/projects` ROUTE report the archive fact on the
   rows of a page that asked for archived rows (`include_archived=true`), on no
   row of a page that did not, and never to an anonymous caller.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, state_graph_routers as routes
from core.db_manager import DBManager
from core.state_claims import ClaimError
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, execute, is_public_read
from core.state_graph import StateGraphError, initialize_graph_schema
from core.state_service import StateService

REPO = Path(__file__).resolve().parents[2]
# "the PREVIOUS code": the commit this capability is based on. Its own tree
# writes the database case (c) migrates.
BASE_SHA = "e45a3984d508a17825a09456300975aa7f85ca97"
ADMIN_TOKEN = "off-tunnel-owner-token"
OWNER = "owner:fixture@local"
REPORT_BYTES = b'{"status":"candidate","settled":true,"usable":true}\n'

NODE = {"key": "fixture.one", "goal": "Deliver fixture.one",
        "acceptance": [{"id": "proof", "kind": "test", "description": "Executed proof"}]}

COUNTED = ("state_projects", "state_nodes", "state_attempts", "state_evidence")


def write(service, action, **arguments):
    """The product's write path (the same function the REST/MCP doors call)."""
    return execute(service, action, arguments, allow_write=True)


def read(service, action, **arguments):
    """The product's read path."""
    return execute(service, action, arguments)


def counts(path) -> dict:
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in COUNTED}


def project_rows(path) -> list:
    with sqlite3.connect(path) as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT project_id,title,source_project_id,created_at FROM state_projects "
            "ORDER BY project_id")]


def access_columns(path) -> list:
    with sqlite3.connect(path) as conn:
        return sorted(row[1] for row in conn.execute("PRAGMA table_info(state_project_access)"))


def access_project_ids(path) -> list:
    with sqlite3.connect(path) as conn:
        return sorted(row[0] for row in conn.execute(
            "SELECT project_id FROM state_project_access"))


def listed(service) -> list:
    return [project["project_id"] for project in read(service, "list_projects")]


def catalogued(service, **arguments) -> list:
    return [project["project_id"]
            for project in read(service, "project_catalog", **arguments)["projects"]]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway AITELIER_HOME holding two projects, one attempt and one evidence row.

    `live` is opened (so the anonymous listing has a row that must stay
    byte-identical) and `root-fixture` stands in for the owner's unused
    root/private fixture projects: private, with a node and an attempt.
    """
    home = tmp_path / "aitelier-home"
    home.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(home))
    db = DBManager(str(home / "aitelier.db"))
    owner = StateService(db, actor=OWNER, is_admin=True, project_read_trusted=True)
    for project_id, title in (("live", "Live project"), ("root-fixture", "Root fixture")):
        write(owner, "create_project", project_id=project_id, title=title)
        write(owner, "add_nodes", project_id=project_id, nodes=[NODE])
    write(owner, "open_project", project_id="live")
    attempt = write(owner, "start_external_attempt", project_id="root-fixture",
                    node_key="fixture.one", expected_revision=1, harness="dsh-native-subagent",
                    external_id="archive-fixture", request_key="archive-fixture")
    report = home / "report.json"
    report.write_bytes(REPORT_BYTES)
    owner.report_external_attempt(attempt["attempt_id"], "done", 0, attempt["context_hash"],
                                  "candidate", str(report),
                                  hashlib.sha256(REPORT_BYTES).hexdigest(), True, "a" * 64, "sha256")
    owner.attempts.record_evidence(attempt["attempt_id"], "e1", "proof", "fail", "a" * 64,
                                   "r.md", "0" * 64, "fixture-verifier", "fixture detail")
    return SimpleNamespace(db=db, owner=owner, home=home, attempt=attempt,
                           path=db.db_path, archived="root-fixture")


# ── (a) archive hides and restores the owner's list, deleting nothing ────────
def test_archive_hides_and_restores_the_owner_list(world):
    owner, pid = world.owner, world.archived
    before_list = read(owner, "list_projects")
    before_catalog = read(owner, "project_catalog")
    before_counts = counts(world.path)
    before_rows = project_rows(world.path)
    before_access = access_project_ids(world.path)
    assert [p["project_id"] for p in before_list] == ["live", pid]
    assert pid in catalogued(owner)

    archived = write(owner, "archive_project", project_id=pid,
                     reason="Unused root fixture: no work since 2026-10-27")
    assert archived["archived"] is True and archived["changed"] is True
    assert archived["reason"] == "Unused root fixture: no work since 2026-10-27"
    assert archived["actor"] == OWNER and archived["updated_at"]

    during_list = read(owner, "list_projects")
    assert [p["project_id"] for p in during_list] == ["live"]
    # exactly the earlier list minus that project, row for row
    assert during_list == [p for p in before_list if p["project_id"] != pid]
    assert pid not in catalogued(owner)
    # the archived project is still reachable on purpose, never deleted
    assert pid in catalogued(owner, include_archived=True)

    # every row survived: the four history tables, the project row, the access rows
    assert counts(world.path) == before_counts
    assert project_rows(world.path) == before_rows
    assert set(before_access) <= set(access_project_ids(world.path))
    assert owner.store.is_project_archived(pid) is True
    # the project still resolves through every read that is not a list
    assert owner.store.get_project(pid)["title"] == "Root fixture"
    assert read(owner, "project_overview", project_id=pid)["project"]["project_id"] == pid
    assert [n["node_key"] for n in read(owner, "get_graph", project_id=pid)["nodes"]] == ["fixture.one"]
    attempts = read(owner, "project_attempts", project_id=pid)["attempts"]
    assert [a["attempt_id"] for a in attempts] == [world.attempt["attempt_id"]]
    assert read(owner, "events", project_id=pid)[-1]["event_type"] == "project_archived"

    # re-archiving is a recorded no-op: nothing changes and no event is appended
    events = read(owner, "events", project_id=pid)
    again = write(owner, "archive_project", project_id=pid)
    assert again["changed"] is False and again["archived"] is True
    assert read(owner, "events", project_id=pid) == events

    restored = write(owner, "unarchive_project", project_id=pid)
    assert restored["archived"] is False and restored["changed"] is True
    # EXACTLY the earlier list: same ids, same order, same rows
    assert read(owner, "list_projects") == before_list
    assert read(owner, "project_catalog") == before_catalog
    assert counts(world.path) == before_counts and project_rows(world.path) == before_rows
    assert owner.store.is_project_archived(pid) is False
    assert read(owner, "events", project_id=pid)[-1]["event_type"] == "project_unarchived"
    assert write(owner, "unarchive_project", project_id=pid)["changed"] is False


# ── (b) the door belongs to the owner ───────────────────────────────────────
def driver_service(db):
    """A registered-driver credential: trusted to read, NOT an admin."""
    return StateService(db, actor="driver:fixture-driver", driver_id="fixture-driver",
                        project_read_trusted=True)


def anonymous_service(db):
    return StateService(db, actor="anonymous", project_read_trusted=False)


def owner_service(db):
    return StateService(db, actor=OWNER, is_admin=True, project_read_trusted=True)


def client_for(service, monkeypatch) -> TestClient:
    """The REAL `/api/state` router with the write gate armed and the admin token set.

    The service is injected where the router resolves it, which is exactly where
    `api/state_graph_routers.get_service` derives `is_admin` from the raw
    credential: a request the transport lets through still meets the owner's door.
    """
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: service
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    return TestClient(app)


OWNER_HEADERS = {"X-AItelier-Admin-Token": ADMIN_TOKEN}


def test_a_driver_credential_is_refused_by_the_archive_door(world, monkeypatch):
    pid = world.archived
    before = read(world.owner, "list_projects")

    client = client_for(driver_service(world.db), monkeypatch)
    refused = client.post("/api/state/commands/archive_project",
                          json={"project_id": pid, "reason": "hide it from the owner"},
                          headers=OWNER_HEADERS)
    # an OBSERVED refusal body, not a silent no-op, and it NAMES the action
    assert refused.status_code == 409, refused.text
    assert "admin_required" in refused.json()["detail"]
    assert "archive_project" in refused.json()["detail"]
    refused_undo = client.post("/api/state/commands/unarchive_project",
                               json={"project_id": pid}, headers=OWNER_HEADERS)
    assert refused_undo.status_code == 409, refused_undo.text
    assert "unarchive_project" in refused_undo.json()["detail"]
    # the refused write wrote nothing: the project is still listed, nothing archived
    assert read(world.owner, "list_projects") == before
    assert pid in catalogued(world.owner)
    assert world.owner.store.is_project_archived(pid) is False

    # the same refusal through the command chokepoint (every transport passes it)
    with pytest.raises(ClaimError, match="admin_required") as excinfo:
        write(driver_service(world.db), "archive_project", project_id=pid)
    assert "archive_project" in str(excinfo.value)
    with pytest.raises(ClaimError, match="admin_required") as excinfo:
        write(driver_service(world.db), "unarchive_project", project_id=pid)
    assert "unarchive_project" in str(excinfo.value)

    # not on the anonymous/public READ surface: the action is not a read, the
    # read door refuses it, and execute refuses the mutation on that surface
    assert "archive_project" in WRITE_REQUESTS and "archive_project" not in READ_REQUESTS
    assert is_public_read("archive_project") is False and is_public_read("unarchive_project") is False
    anonymous = client_for(anonymous_service(world.db), monkeypatch)
    assert anonymous.post("/api/state/query/archive_project",
                          json={"project_id": pid}).status_code == 403
    assert anonymous.post("/api/state/query/unarchive_project",
                          json={"project_id": pid}).status_code == 403
    with pytest.raises(StateGraphError, match="not available on the read surface"):
        execute(anonymous_service(world.db), "archive_project", {"project_id": pid})

    # the owner's admin credential IS accepted, through the same router
    owner = client_for(owner_service(world.db), monkeypatch)
    accepted = owner.post("/api/state/commands/archive_project",
                          json={"project_id": pid, "reason": "Unused fixture"},
                          headers=OWNER_HEADERS)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["archived"] is True
    assert pid not in [p["project_id"] for p in
                       owner.get("/api/state/projects").json()["projects"]]
    undone = owner.post("/api/state/commands/unarchive_project", json={"project_id": pid},
                        headers=OWNER_HEADERS)
    assert undone.status_code == 200 and undone.json()["archived"] is False
    assert read(world.owner, "list_projects") == before


def test_the_anonymous_list_is_untouched_by_archiving(world, monkeypatch):
    """Archiving is not unpublishing: the public listing must not move a byte."""
    pid = world.archived
    anonymous = client_for(anonymous_service(world.db), monkeypatch)
    public_before = anonymous.get("/api/state/projects")
    assert public_before.status_code == 200
    assert [p["project_id"] for p in public_before.json()["projects"]] == ["live"]

    write(world.owner, "archive_project", project_id="live", reason="Owner hid it")
    public_after = anonymous.get("/api/state/projects")
    assert (public_before.status_code, public_before.content) == \
           (public_after.status_code, public_after.content)
    # a PUBLIC project archived by the owner stays public: withdrawing it is
    # `close_project`'s recorded job, and archiving never opens or closes one
    assert "live" in [p["project_id"] for p in public_after.json()["projects"]]
    assert world.owner.store.is_project_public("live") is True
    # ...while the owner's own list no longer shows it
    assert "live" not in listed(world.owner)
    assert pid in listed(world.owner)
    write(world.owner, "unarchive_project", project_id="live")
    assert "live" in listed(world.owner)
    assert (public_before.status_code, public_before.content) == \
           (anonymous.get("/api/state/projects").status_code,
            anonymous.get("/api/state/projects").content)


# ── (d) the catalog says which rows it was asked to hide ─────────────────────
def test_the_catalog_reports_which_rows_are_archived(world, monkeypatch):
    """catalog-reports-which-rows-are-archived, through the action and the route.

    The archive fact is reported by the READ that lifts the archive filter, on
    every row it returns (literally true or false, never a missing key), and by
    no other answer: the default page carries no fact at all, and the anonymous
    page - which never applies the filter - is not told what the owner hid.
    """
    owner, pid = world.owner, world.archived
    write(owner, "archive_project", project_id=pid, reason="Unused fixture")

    # the ACTION: the default page omits the archived project, the page that ASKS
    # returns it carrying the fact - on that row and on no other
    default = read(owner, "project_catalog")["projects"]
    asked = read(owner, "project_catalog", include_archived=True)["projects"]
    assert [p["project_id"] for p in default] == ["live"]
    assert [p["project_id"] for p in asked] == ["live", pid]
    assert {p["project_id"]: p["archived"] for p in asked} == {"live": False, pid: True}
    assert all(type(p["archived"]) is bool for p in asked)
    # a page that did not ask keeps the bytes it always had: no fact, no key
    assert all("archived" not in p for p in default)

    # the ROUTE returns the same two answers, row for row
    owner_client = client_for(owner_service(world.db), monkeypatch)
    route_default = owner_client.get("/api/state/projects")
    route_asked = owner_client.get("/api/state/projects?include_archived=true")
    assert route_default.status_code == 200, route_default.text
    assert route_asked.status_code == 200, route_asked.text
    assert route_default.json()["projects"] == default
    assert route_asked.json()["projects"] == asked

    # ...and the anonymous answer is untouched by the fact and by the argument:
    # archiving is not unpublishing, and which listings the owner hid is theirs
    anonymous = client_for(anonymous_service(world.db), monkeypatch)
    public_default = anonymous.get("/api/state/projects")
    public_asked = anonymous.get("/api/state/projects?include_archived=true")
    assert (public_default.status_code, public_default.content) == \
           (public_asked.status_code, public_asked.content)
    assert [p["project_id"] for p in public_asked.json()["projects"]] == ["live"]
    assert all("archived" not in p for p in public_asked.json()["projects"])


# ── (c) the change preserves a database written by the previous code ────────
# The previous code's own tree builds the database, writes the rows through its
# own StateService and answers the project list. It is a separate PROCESS with
# the extracted tree first on sys.path, and it reports `core.__file__` so this
# file can prove the PRE-CHANGE code ran (a subprocess that silently imported
# the changed tree would make this criterion vacuous).
PREVIOUS_CODE = r'''
import hashlib, json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import core
from core.db_manager import DBManager
from core.state_commands import execute
from core.state_service import StateService

tree, path = sys.argv[1], sys.argv[2]
assert core.__file__.startswith(tree + "/"), core.__file__
node = {"key": "fixture.one", "goal": "Deliver fixture.one",
        "acceptance": [{"id": "proof", "kind": "test", "description": "Executed proof"}]}
writer = StateService(DBManager(path), actor="owner:fixture@local", is_admin=True,
                      project_read_trusted=True)
def write(action, **arguments):
    return execute(writer, action, arguments, allow_write=True)
for project_id, title in (("live", "Live project"), ("root-fixture", "Root fixture")):
    write("create_project", project_id=project_id, title=title)
    write("add_nodes", project_id=project_id, nodes=[node])
write("open_project", project_id="live")
attempt = write("start_external_attempt", project_id="root-fixture", node_key="fixture.one",
                expected_revision=1, harness="dsh-native-subagent", external_id="previous-code",
                request_key="previous-code")
body = b'{"status":"candidate","settled":true,"usable":true}\n'
report = sys.argv[3]
open(report, "wb").write(body)
writer.report_external_attempt(attempt["attempt_id"], "done", 0, attempt["context_hash"],
                              "candidate", report, hashlib.sha256(body).hexdigest(), True,
                              "a" * 64, "sha256")
writer.attempts.record_evidence(attempt["attempt_id"], "e1", "proof", "fail", "a" * 64,
                               "r.md", "0" * 64, "fixture-verifier", "fixture detail")
print(json.dumps({
    "core_file": core.__file__,
    "list": execute(writer, "list_projects", {}),
    "catalog": execute(writer, "project_catalog", {}),
    "tables": (lambda con: sorted(row[0] for row in
                                  con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                                  if row[0].startswith("state_")))(sqlite3.connect(path)),
    "access_columns": (lambda con: sorted(row[1] for row in
                                          con.execute("PRAGMA table_info(state_project_access)"))
                       )(sqlite3.connect(path)),
}, sort_keys=True))
'''


def previous_code_result(tmp_path) -> dict:
    """Extract the base commit's tree and let IT write a database."""
    tree = tmp_path / "previous-code"
    tree.mkdir()
    archive = subprocess.run(["git", "archive", "--format=tar", BASE_SHA], cwd=REPO,
                             capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        try:
            tar.extractall(tree, filter="data")
        except TypeError:            # Python < 3.12 has no extraction filter
            tar.extractall(tree)
    script = tmp_path / "write_previous_code.py"
    script.write_text(PREVIOUS_CODE, encoding="utf-8")
    database = tmp_path / "previous-code.db"
    done = subprocess.run([sys.executable, str(script), str(tree), str(database),
                           str(tmp_path / "previous-report.json")],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    result = json.loads(done.stdout)
    assert result["core_file"].startswith(str(tree)), result["core_file"]
    return {"database": database, "result": result}


def test_the_change_migrates_a_database_written_by_the_previous_code(tmp_path):
    built = previous_code_result(tmp_path)
    path, previous = built["database"], built["result"]
    # the previous code really is previous: it has no archive table at all, and
    # the access table is the shape it always had
    assert "state_project_archive" not in previous["tables"]
    assert previous["access_columns"] == ["changed_at", "changed_by", "opened_at",
                                          "opened_by", "project_id", "visibility"]
    before_counts = counts(path)
    before_rows = project_rows(path)
    assert before_counts["state_projects"] == 2
    assert before_counts["state_nodes"] == 2
    assert before_counts["state_attempts"] == 1
    assert before_counts["state_evidence"] == 1
    assert [p["project_id"] for p in previous["list"]] == ["live", "root-fixture"]
    assert [p["project_id"] for p in previous["catalog"]["projects"]] == ["live", "root-fixture"]

    # the changed code opens that database through its own service: the additive
    # table lands on the migration every construction point already runs
    changed = StateService(DBManager(str(path)), actor=OWNER, is_admin=True,
                           project_read_trusted=True)
    assert counts(path) == before_counts
    assert project_rows(path) == before_rows
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_project_archive").fetchone()[0] == 0
        # ...and NOTHING else about the previous schema moved: the additive table
        # is the whole migration (besides the tables later steps add on their own:
        # step 19's owner chat channels, core/state_channels.py)
        tables = sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'") if row[0].startswith("state_"))
        later = {"state_owner_channels", "state_owner_channel_codes", "state_message_sources"}
        assert sorted(set(tables) - {"state_project_archive"} - later) == previous["tables"]
        assert access_columns(path) == previous["access_columns"]
    # the list served afterwards equals the list served before, exactly
    assert read(changed, "list_projects") == previous["list"]
    assert read(changed, "project_catalog") == previous["catalog"]

    # applying the same migration a second time is idempotent
    initialize_graph_schema(DBManager(str(path)))
    assert counts(path) == before_counts
    assert project_rows(path) == before_rows
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_project_archive").fetchone()[0] == 0
    assert access_columns(path) == previous["access_columns"]
    reopened = StateService(DBManager(str(path)), actor=OWNER, is_admin=True,
                            project_read_trusted=True)
    assert read(reopened, "list_projects") == previous["list"]
    assert read(reopened, "project_catalog") == previous["catalog"]
    # the migrated database accepts the new capability itself
    assert write(reopened, "archive_project", project_id="root-fixture")["changed"] is True
    assert counts(path) == before_counts
    assert write(reopened, "unarchive_project", project_id="root-fixture")["changed"] is True
    assert read(reopened, "list_projects") == previous["list"]
