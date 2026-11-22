"""Plugin step 13: the steward proposes, its owner decides, the backend executes the
version the owner approved (design-steward 5).

The HTTP tests run the real app with the write gate ON and test mode OFF: the
owner is a Cloudflare Access writer email, drivers authenticate with their real
tokens through the registry."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, state_graph
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService
import sqlite3

from core.state_steward import INTERRUPTED, OLD_KIND_CHECK, SCHEMA, ProposalConflict
from tests.support import state_template

HOME = "aitelier-home"
OWNER = "owner@example.com"
PEPPER = "steward-proposal-pepper-" * 3
REPO = Path(__file__).resolve().parents[2]


def node(key):
    return {"key": key, "goal": f"goal {key}",
            "acceptance": [{"id": "a", "kind": "test", "description": "criterion"}]}


CREATE = {"project_id": "gamma", "title": "Gamma", "goal": "a new project", "seed_nodes": [node("n1"), node("n2")]}


@pytest.fixture
def world(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("steward-proposal-owner-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", HOME):
        seeder.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    owner = StateService(db, actor="owner:" + OWNER, project_read_trusted=True)
    return type("World", (), dict(db=db, registry=registry, runtime=runtime, steward_row=steward,
                                  director_row=director, tokens=tokens, as_driver=as_driver, owner=owner,
                                  steward=as_driver(steward), director=as_driver(director)))


def propose(world, kind="create_project", payload=None, key="k1", **extra):
    return execute(world.steward, "create_steward_proposal",
                   {"request_key": key, "kind": kind, "payload": payload or CREATE,
                    "reason": "because", **extra}, allow_write=True)


def row(world, proposal_id):
    with world.db.get_connection() as conn:
        return dict(conn.execute("SELECT * FROM state_steward_proposals WHERE proposal_id=?",
                                 (proposal_id,)).fetchone())


def projects(world):
    with world.db.get_connection() as conn:
        return {r[0] for r in conn.execute("SELECT project_id FROM state_projects")}


def steward_inbox(world):
    return execute(world.steward, "list_driver_messages", {})["messages"]


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


OWNER_HEADERS = {"X-Test-Email": OWNER}


def approve(client, proposal_id, version=1, headers=OWNER_HEADERS, **extra):
    return client.post(f"/api/steward/proposals/{proposal_id}/approve",
                       json={"expected_version": version, **extra}, headers=headers)


# --- the steward's commands -------------------------------------------------------------

def test_the_steward_records_an_open_proposal_and_nothing_else(world):
    out = propose(world)
    assert out == {"proposal_id": out["proposal_id"], "version": 1, "status": "open"}
    assert propose(world) == out, "same request_key, same proposal: a replay"
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        propose(world, payload={**CREATE, "title": "Other"})
    assert "gamma" not in projects(world)
    listed = execute(world.owner, "list_steward_proposals", {})["proposals"]
    assert [p["proposal_id"] for p in listed] == [out["proposal_id"]]
    assert listed[0]["payload"]["seed_nodes"][0]["key"] == "n1" and listed[0]["result"] is None


def test_only_the_steward_proposes_and_withdraws(world):
    assert "complete_steward_memory_request" in STEWARD_WRITE_ACTIONS
    for service in (world.director, world.owner):
        with pytest.raises(StateGraphError, match="steward_role_required"):
            execute(service, "create_steward_proposal", {"project_id": HOME, "request_key": "k",
                    "kind": "start_driver", "payload": {"project_id": "alpha"}, "reason": "r"}, allow_write=True)
    pid = propose(world)["proposal_id"]
    with pytest.raises(StateGraphError, match="steward_role_required"):
        execute(world.director, "withdraw_steward_proposal",
                {"request_key": "w", "proposal_id": pid, "reason": "r"}, allow_write=True)
    # The steward's own project only, never another one it names.
    with pytest.raises(StateGraphError, match="steward_project_required"):
        execute(world.steward, "withdraw_steward_proposal",
                {"project_id": "alpha", "request_key": "w", "proposal_id": pid, "reason": "r"}, allow_write=True)


@pytest.mark.parametrize("kind, payload, error", [
    ("create_project", {**CREATE, "seed_nodes": [{"node_key": "n1", "title": "t"}]}, "invalid_payload"),
    ("create_project", {**CREATE, "project_id": "alpha"}, "project_exists"),
    ("start_driver", {"project_id": "nope"}, "unknown_project"),
    ("stop_driver", {"project_id": "alpha", "impact": {}}, "invalid_payload"),
])
def test_payloads_are_validated_per_kind(world, kind, payload, error):
    with pytest.raises(StateGraphError, match=error):
        propose(world, kind=kind, payload=payload)


def test_supersede_and_withdraw_own_open_proposals(world):
    first = propose(world)["proposal_id"]
    second = propose(world, key="k2", payload={**CREATE, "title": "Gamma 2"}, supersedes=first)["proposal_id"]
    assert row(world, first)["status"] == "superseded"
    assert json.loads(row(world, first)["result_json"]) == {"superseded_by": second}
    with pytest.raises(StateGraphError):
        propose(world, key="k3", supersedes=first)
    out = execute(world.steward, "withdraw_steward_proposal",
                  {"request_key": "w1", "proposal_id": second, "reason": "not needed"}, allow_write=True)
    assert out["status"] == "withdrawn" and row(world, second)["status"] == "withdrawn"
    assert execute(world.steward, "withdraw_steward_proposal", {"request_key": "w1", "proposal_id": second,
                   "reason": "not needed"}, allow_write=True) == out, "a replay"
    assert execute(world.owner, "list_steward_proposals", {})["proposals"] == []
    assert len(execute(world.owner, "list_steward_proposals", {"status": "all"})["proposals"]) == 2


def test_stop_driver_records_what_it_would_interrupt(world):
    world.runtime.update("alpha", enabled=1, phase="running", subagents_json=json.dumps(["child-1"]))
    pid = propose(world, kind="stop_driver", payload={"project_id": "alpha"})["proposal_id"]
    payload = json.loads(row(world, pid)["payload_json"])
    assert payload["impact"]["running_subagents"] == [{"session_id": "child-1"}]


# --- approval: once, the version shown, owner only ---------------------------------------

def test_concurrent_approvals_execute_once(world, monkeypatch):
    pid = propose(world)["proposal_id"]
    calls = []
    original = state_graph.StateGraphStore._create_project

    def counted(self, conn, *args):
        calls.append(args[0])
        return original(self, conn, *args)
    monkeypatch.setattr(state_graph.StateGraphStore, "_create_project", counted)
    barrier, outcomes = threading.Barrier(4), []

    def one():
        owner = StateService(world.db, actor="owner:" + OWNER, project_read_trusted=True)
        barrier.wait()
        try:
            owner.steward.begin_approval(pid, 1)
            outcomes.append(owner.steward.execute_create_project(pid)["status"])
        except ProposalConflict as exc:
            outcomes.append("conflict:" + exc.row["status"])
    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("done") == 1 and len(outcomes) == 4, outcomes
    assert all(o.startswith("conflict:") for o in outcomes if o != "done"), outcomes
    assert calls == ["gamma"], "the project was created exactly once"


def test_approve_over_http_executes_once_and_wakes_the_steward(world, client):
    pid = propose(world)["proposal_id"]
    r = approve(client, pid)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done" and done["decided_by"] == "owner:" + OWNER
    assert done["result"] == {"project_id": "gamma", "created_nodes": ["n1", "n2"]}
    again = approve(client, pid)
    assert again.status_code == 409 and again.json()["detail"]["status"] == "done", again.text
    again = approve(client, pid, version=done["version"])
    assert again.status_code == 409, "a done proposal is never approved again"
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_events WHERE project_id='gamma' "
                            "AND event_type='project_created'").fetchone()[0] == 1
    wakes = [m for m in steward_inbox(world) if pid in m["subject"]]
    assert len(wakes) == 1 and "done" in wakes[0]["body"] and "gamma" in wakes[0]["body"]


def test_a_version_mismatch_is_refused_with_the_current_row(world, client):
    pid = propose(world)["proposal_id"]
    r = approve(client, pid, version=7)
    assert r.status_code == 409 and r.json()["detail"]["version"] == 1, r.text
    assert row(world, pid)["status"] == "open" and "gamma" not in projects(world)


def test_declined_or_withdrawn_proposals_cannot_be_approved(world, client):
    declined = propose(world)["proposal_id"]
    r = client.post(f"/api/steward/proposals/{declined}/decline", json={"expected_version": 1, "reason": "no"},
                    headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "declined", r.text
    withdrawn = propose(world, key="k2", payload={**CREATE, "project_id": "delta"})["proposal_id"]
    execute(world.steward, "withdraw_steward_proposal",
            {"request_key": "w", "proposal_id": withdrawn, "reason": "r"}, allow_write=True)
    for pid in (declined, withdrawn):
        current = row(world, pid)
        for version in (1, current["version"]):
            r = approve(client, pid, version=version)
            assert r.status_code == 409 and r.json()["detail"]["status"] == current["status"], r.text
    assert not {"gamma", "delta"} & projects(world)
    assert any(declined in m["subject"] and "declined" in m["body"] for m in steward_inbox(world))


def test_a_driver_token_cannot_approve_or_decline(world, client):
    pid = propose(world)["proposal_id"]
    for who, code in (("director", "owner_session_required"), ("steward", "steward_read_only")):
        headers = {"X-AItelier-Driver-Token": world.tokens[who]}
        for action in ("approve", "decline"):
            r = client.post(f"/api/steward/proposals/{pid}/{action}", json={"expected_version": 1},
                            headers=headers)
            assert r.status_code == 403 and code in r.text, (who, action, r.text)
    # Nor anonymous, nor a signed-in non-writer.
    assert approve(client, pid, headers={}).status_code == 403
    assert approve(client, pid, headers={"X-Test-Email": "stranger@example.com"}).status_code == 403
    assert row(world, pid)["status"] == "open" and "gamma" not in projects(world)


def test_seed_nodes_over_budget_leave_no_project_and_tell_the_steward(world, client, monkeypatch):
    monkeypatch.setattr(state_graph, "MAX_NODES", 1)
    pid = propose(world)["proposal_id"]
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "failed", r.text
    error = r.json()["result"]["error"]
    assert "at most 1 active nodes" in error
    assert "gamma" not in projects(world), "the project was rolled back with its nodes"
    wakes = [m for m in steward_inbox(world) if pid in m["subject"]]
    assert len(wakes) == 1 and "failed" in wakes[0]["body"] and "at most 1 active nodes" in wakes[0]["body"]


def test_an_existing_project_fails_the_proposal_and_is_left_alone(world, client):
    pid = propose(world)["proposal_id"]
    world.owner.store.create_project("gamma", "Someone else's")
    r = approve(client, pid)
    assert r.json()["status"] == "failed" and "已被占用" in r.json()["result"]["error"], r.text
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_nodes WHERE project_id='gamma'").fetchone()[0] == 0


_CRASH = """
import os, sys
from core import state_graph
from core.state_database import StateDatabase
from core.state_service import StateService
original = state_graph.StateGraphStore._create_project
def crash(self, conn, *args):
    original(self, conn, *args)
    os._exit(17)   # killed between creating the project and adding its nodes
state_graph.StateGraphStore._create_project = crash
owner = StateService(StateDatabase(sys.argv[1]), actor="owner:x@example.com", project_read_trusted=True)
owner.steward.execute_create_project(sys.argv[2])
"""


def test_a_crash_between_project_and_nodes_leaves_no_project_then_recovery_settles_it(world):
    pid = propose(world)["proposal_id"]
    world.owner.steward.begin_approval(pid, 1)
    done = subprocess.run([sys.executable, "-c", _CRASH, world.db.db_path, pid], cwd=REPO,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 17, done.stderr[-2000:]
    assert "gamma" not in projects(world), "the killed transaction left no project"
    assert row(world, pid)["status"] == "executing"
    # The startup scan: re-executed under "only if the project does not exist".
    StateService(world.db, actor="steward-recovery", project_read_trusted=True).steward.recover(world.runtime)
    settled = row(world, pid)
    assert settled["status"] == "done"
    with world.db.get_connection() as conn:
        assert {r[0] for r in conn.execute("SELECT node_key FROM state_nodes WHERE project_id='gamma'")} == {"n1", "n2"}
    assert any(pid in m["subject"] for m in steward_inbox(world))


def test_recovery_judges_driver_proposals_by_the_drivers_phase(world):
    start = propose(world, kind="start_driver", payload={"project_id": "alpha"})["proposal_id"]
    stop = propose(world, key="k2", kind="stop_driver", payload={"project_id": "alpha"})["proposal_id"]
    for pid in (start, stop):
        world.owner.steward.begin_approval(pid, 1)
    world.runtime.update("alpha", enabled=1, phase="running")
    StateService(world.db, actor="steward-recovery", project_read_trusted=True).steward.recover(world.runtime)
    assert row(world, start)["status"] == "done"
    assert row(world, stop)["status"] == "failed"
    assert json.loads(row(world, stop)["result_json"])["error"] == INTERRUPTED


# --- start / stop -------------------------------------------------------------------------

def test_start_driver_runs_the_dsh_start_entry(world, client, monkeypatch):
    calls = []

    async def start(project_id, *, provider="aitelier", model=None, actor):
        calls.append((project_id, provider, model, actor))
        world.runtime.update(project_id, enabled=1, phase="starting")
        return world.runtime.public(world.runtime.get(project_id))
    monkeypatch.setattr(world.runtime, "start", start)
    pid = propose(world, kind="start_driver", payload={"project_id": "alpha"})["proposal_id"]
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    assert calls == [("alpha", "aitelier", None, "owner:" + OWNER)]
    assert r.json()["result"]["driver"]["phase"] == "starting"


def test_a_failed_start_fails_the_proposal(world, client, monkeypatch):
    async def start(project_id, **kwargs):
        raise ValueError("no route")
    monkeypatch.setattr(world.runtime, "start", start)
    pid = propose(world, kind="start_driver", payload={"project_id": "alpha"})["proposal_id"]
    r = approve(client, pid)
    assert r.json()["status"] == "failed" and "no route" in r.json()["result"]["error"]


def test_stop_driver_with_live_work_needs_the_owners_confirmation(world, client, monkeypatch):
    stopped = []

    async def stop(project_id, **kwargs):
        stopped.append(project_id)
        world.runtime.update(project_id, enabled=0, phase="stopped", subagents_json="[]")
        return world.runtime.public(world.runtime.get(project_id))
    monkeypatch.setattr(world.runtime, "stop", stop)
    world.runtime.update("alpha", enabled=1, phase="running", subagents_json=json.dumps(["child-1"]))
    pid = propose(world, kind="stop_driver", payload={"project_id": "alpha"})["proposal_id"]
    r = client.get(f"/api/steward/proposals/{pid}/impact", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json()["running_subagents"] == [{"session_id": "child-1"}], r.text
    r = client.get(f"/api/steward/proposals/{pid}/impact",
                   headers={"X-AItelier-Driver-Token": world.tokens["steward"]})
    assert r.status_code == 200, "the steward reads the impact too"
    r = client.get(f"/api/steward/proposals/{pid}/impact",
                   headers={"X-AItelier-Driver-Token": world.tokens["director"]})
    assert r.status_code == 403
    r = approve(client, pid)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "confirm_interrupt_required", r.text
    assert r.json()["detail"]["impact"]["running_subagents"] == [{"session_id": "child-1"}]
    assert row(world, pid)["status"] == "open" and stopped == []
    r = approve(client, pid, confirm_interrupt=True)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    result = r.json()["result"]
    assert result["confirm_interrupt"] is True and result["impact"]["running_subagents"]
    assert stopped == ["alpha"]


def test_stop_driver_without_live_work_needs_no_confirmation(world, client, monkeypatch):
    async def stop(project_id, **kwargs):
        return world.runtime.public(world.runtime.get(project_id))
    monkeypatch.setattr(world.runtime, "stop", stop)
    pid = propose(world, kind="stop_driver", payload={"project_id": "alpha"})["proposal_id"]
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text


# --- the wake survives a restart ----------------------------------------------------------

def test_an_undelivered_result_still_arrives_after_a_restart(world, tmp_path):
    pid = propose(world)["proposal_id"]
    # The steward's driver row is gone when the result is written: nobody to wake.
    with world.db.get_connection() as conn:
        saved = dict(conn.execute("SELECT * FROM dsh_drivers WHERE project_id=?", (HOME,)).fetchone())
        conn.execute("DELETE FROM dsh_drivers WHERE project_id=?", (HOME,))
        conn.commit()
    world.owner.steward.decline(pid, 1, "not now")
    assert row(world, pid)["notified_at"] is None
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO dsh_drivers(" + ",".join(saved) + ") VALUES(" + ",".join("?" * len(saved)) + ")",
                     tuple(saved.values()))
        conn.commit()
    # A restart: a fresh handle on the same file, then the startup scan.
    db = StateDatabase(world.db.db_path)
    StateService(db, actor="steward-recovery", project_read_trusted=True).steward.recover(None)
    assert row(world, pid)["notified_at"]
    steward = StateService(db, actor="driver:" + world.steward_row["driver_id"],
                           driver_id=world.steward_row["driver_id"], project_read_trusted=True)
    wakes = [m for m in execute(steward, "list_driver_messages", {})["messages"] if pid in m["subject"]]
    assert len(wakes) == 1 and "declined" in wakes[0]["body"] and wakes[0]["status"] == "unread"
    # Delivered once: a second scan does not repeat it.
    StateService(db, actor="steward-recovery", project_read_trusted=True).steward.recover(None)
    assert len([m for m in execute(steward, "list_driver_messages", {})["messages"] if pid in m["subject"]]) == 1


def test_a_delivered_result_is_still_unread_after_a_restart(world):
    pid = propose(world)["proposal_id"]
    world.owner.steward.decline(pid, 1, "not now")
    steward = StateService(StateDatabase(world.db.db_path), actor="driver:" + world.steward_row["driver_id"],
                           driver_id=world.steward_row["driver_id"], project_read_trusted=True)
    result = execute(steward, "wait_for_state_change", {"project_id": HOME, "include_driver_inbox": True,
                                                        "timeout_seconds": 0})
    import asyncio
    result = asyncio.run(result) if hasattr(result, "__await__") else result
    assert any(pid in m["subject"] for m in result["driver_inbox"]), result


# --- archive / unarchive (owner 2026-11-18): proposed by the steward, executed as the owner --

def archived(world, project_id="alpha"):
    with world.db.get_connection() as conn:
        found = conn.execute("SELECT * FROM state_project_archive WHERE project_id=?", (project_id,)).fetchone()
    return dict(found) if found else None


def test_archive_proposals_are_refused_unless_they_would_change_something(world):
    archive = {"project_id": "alpha"}
    with pytest.raises(StateGraphError, match="invalid_project"):
        propose(world, kind="archive_project", payload={"project_id": HOME})
    with pytest.raises(StateGraphError, match="invalid_project"):
        propose(world, kind="unarchive_project", payload={"project_id": HOME})
    with pytest.raises(StateGraphError, match="unknown_project"):
        propose(world, kind="archive_project", payload={"project_id": "nope"})
    with pytest.raises(StateGraphError, match="not_archived"):
        propose(world, kind="unarchive_project", payload=archive)
    with pytest.raises(StateGraphError, match="invalid_payload"):
        propose(world, kind="archive_project", payload={**archive, "reason": "x" * 4001})
    with pytest.raises(StateGraphError, match="invalid_payload"):
        propose(world, kind="archive_project", payload={**archive, "extra": 1})
    with pytest.raises(StateGraphError, match="archive reason must be nonempty"):
        propose(world, kind="archive_project", payload={**archive, "reason": "  "})
    world.owner.store.set_project_archive("alpha", True, "owner:" + OWNER)
    with pytest.raises(StateGraphError, match="already_archived"):
        propose(world, kind="archive_project", payload=archive)
    out = propose(world, kind="unarchive_project", payload={**archive, "reason": " back "})
    assert json.loads(row(world, out["proposal_id"])["payload_json"]) == {"project_id": "alpha", "reason": "back"}
    assert archived(world)["archived"] == 1, "a proposal executes nothing"


def test_approving_archive_then_unarchive_writes_it_as_the_owner_and_wakes_the_steward(world, client):
    pid = propose(world, kind="archive_project", payload={"project_id": "alpha", "reason": "finished"})["proposal_id"]
    r = approve(client, pid)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done" and done["decided_by"] == "owner:" + OWNER
    assert done["result"] == {"archived": True, "project_id": "alpha"}
    record = archived(world)
    assert record["archived"] == 1 and record["actor"] == "owner:" + OWNER and record["reason"] == "finished"
    assert world.owner.store.is_project_archived("alpha")
    assert approve(client, pid, version=done["version"]).status_code == 409, "never executed twice"
    back = propose(world, key="k2", kind="unarchive_project", payload={"project_id": "alpha"})["proposal_id"]
    r = approve(client, back)
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    assert r.json()["result"] == {"archived": False, "project_id": "alpha"}
    assert not world.owner.store.is_project_archived("alpha")
    assert archived(world)["actor"] == "owner:" + OWNER and archived(world)["reason"] == ""
    with world.db.get_connection() as conn:
        events = [r[0] for r in conn.execute("SELECT event_type FROM state_events WHERE project_id='alpha' "
                                             "AND event_type LIKE 'project_%archived' ORDER BY seq")]
    assert events == ["project_archived", "project_unarchived"]
    for proposal in (pid, back):
        wakes = [m for m in steward_inbox(world) if proposal in m["subject"]]
        assert len(wakes) == 1 and "done" in wakes[0]["body"] and "alpha" in wakes[0]["body"], proposal


@pytest.mark.parametrize("kind, before, error", [("archive_project", False, "already_archived"),
                                                 ("unarchive_project", True, "not_archived")])
def test_an_archive_state_changed_since_the_proposal_fails_it(world, client, kind, before, error):
    if before:
        world.owner.store.set_project_archive("alpha", True, "owner:" + OWNER, "first")
    pid = propose(world, kind=kind, payload={"project_id": "alpha"})["proposal_id"]
    world.owner.store.set_project_archive("alpha", not before, "owner:" + OWNER, "meanwhile")
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "failed", r.text
    assert error in r.json()["result"]["error"]
    assert archived(world)["reason"] == "meanwhile", "the owner's own decision is left alone"
    wakes = [m for m in steward_inbox(world) if pid in m["subject"]]
    assert len(wakes) == 1 and "failed" in wakes[0]["body"] and error in wakes[0]["body"]


def test_declined_or_withdrawn_archive_proposals_change_nothing(world, client):
    declined = propose(world, kind="archive_project", payload={"project_id": "alpha"})["proposal_id"]
    r = client.post(f"/api/steward/proposals/{declined}/decline", json={"expected_version": 1, "reason": "keep it"},
                    headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "declined", r.text
    withdrawn = propose(world, key="k2", kind="archive_project", payload={"project_id": "alpha"})["proposal_id"]
    execute(world.steward, "withdraw_steward_proposal",
            {"request_key": "w", "proposal_id": withdrawn, "reason": "r"}, allow_write=True)
    assert row(world, withdrawn)["status"] == "withdrawn"
    for pid in (declined, withdrawn):
        assert approve(client, pid, version=row(world, pid)["version"]).status_code == 409
    assert archived(world) is None
    assert any(declined in m["subject"] and "declined" in m["body"] for m in steward_inbox(world))


def test_recovery_settles_an_executing_archive_proposal_by_the_projects_state(world):
    world.owner.store.create_project("beta", "Beta")
    matched = propose(world, kind="archive_project", payload={"project_id": "alpha"})["proposal_id"]
    rerun = propose(world, key="k2", kind="archive_project", payload={"project_id": "beta"})["proposal_id"]
    for pid in (matched, rerun):
        world.owner.steward.begin_approval(pid, 1)
    world.owner.store.set_project_archive("alpha", True, "owner:" + OWNER, "already")
    StateService(world.db, actor="steward-recovery", project_read_trusted=True).steward.recover(world.runtime)
    assert row(world, matched)["status"] == "done"
    assert json.loads(row(world, matched)["result_json"]) == {"archived": True, "project_id": "alpha",
                                                              "recovered": True}
    assert archived(world)["reason"] == "already", "a state that already matches is not written again"
    assert row(world, rerun)["status"] == "done"
    assert json.loads(row(world, rerun)["result_json"]) == {"archived": True, "project_id": "beta"}
    assert archived(world, "beta")["actor"] == "owner:" + OWNER, "re-executed as the owner who approved it"
    assert all(any(pid in m["subject"] for m in steward_inbox(world)) for pid in (matched, rerun))


def test_a_proposals_table_with_the_old_kind_check_is_migrated_once(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old_schema = SCHEMA.replace(SCHEMA[SCHEMA.index("CHECK(kind IN ("):SCHEMA.index("),\n payload_json") + 1],
                                OLD_KIND_CHECK, 1)
    assert OLD_KIND_CHECK in old_schema and "archive_project" not in old_schema.split(";")[0]
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO state_steward_proposals VALUES('prop-a',3,'stop_driver',?,?,'done','driver:s',"
                     "'2026-11-01T00:00:00+00:00',NULL,'2026-11-02','owner:o',?,'2026-11-02')",
                     ('{"project_id":"alpha"}', "why \u00e9\n", '{"driver":{}}'))
        conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                     "created_by,created_at) VALUES('prop-b',1,'create_project','{}','r','open','driver:s','2026-11-03')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                         "created_by,created_at) VALUES('x',1,'archive_project','{}','r','open','s','t')")
        before = conn.execute("SELECT * FROM state_steward_proposals ORDER BY proposal_id").fetchall()
    db = StateDatabase(path)
    StateService(db, actor="fixture", project_read_trusted=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM state_steward_proposals ORDER BY proposal_id").fetchall() == before
        table = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_steward_proposals'").fetchone()[0]
        assert OLD_KIND_CHECK not in table and "'unarchive_project'" in table
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                            "AND name='state_steward_proposals_status'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%old_kinds%'").fetchone()
        for kind in ("archive_project", "unarchive_project"):
            conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                         "created_by,created_at) VALUES(?,1,?,'{}','r','open','s','t')", ("new-" + kind, kind))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                         "created_by,created_at) VALUES('y',1,'deploy','{}','r','open','s','t')")
        conn.commit()
        rows = conn.execute("SELECT * FROM state_steward_proposals ORDER BY proposal_id").fetchall()
    # Migrated already: a second start leaves the table (and its rows) exactly as they are.
    StateService(StateDatabase(path), actor="fixture", project_read_trusted=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='state_steward_proposals'").fetchone()[0] == table
        assert conn.execute("SELECT * FROM state_steward_proposals ORDER BY proposal_id").fetchall() == rows
