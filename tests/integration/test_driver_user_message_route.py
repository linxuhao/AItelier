"""The dashboard read surface: GET /api/dsh-drivers/{project_id}/messages.

The user must be able to read what their driver told them without opening a run
trace, a session log or the database, and without the sending driver being
online. The route takes the same authenticated-reader verdict as the
neighbouring driver routes and reads persisted rows only.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz
from api.dsh_driver_routers import router
from api.state_graph_routers import get_service
from core.driver_presence import declare
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    seed.create_project("beta", "Beta")
    seed.store.add_nodes("alpha", [{"key": "a", **NODE}])
    registry = DriverRegistry(db, "user-message-route-pepper-not-a-secret")
    registry.register("ada", "Ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {"db": db,
            "driver": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "reader": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def client(world, monkeypatch, *, reader=True):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_service] = lambda: world["reader"]
    monkeypatch.setattr(authz, "write_denial_reason",
                        lambda _: authz.WRITE_DENIED_NOT_AUTHENTICATED)
    if reader:
        app.dependency_overrides[authz.require_reader] = lambda: None
    return TestClient(app)


def send(world, **kw):
    return execute(world["driver"], "send_user_message",
                   {"request_key": "hello", "project_id": "alpha", "subject": "Progress",
                    "body": "Half the nodes are done.", **kw}, allow_write=True)


def test_the_route_needs_the_reader_credential(world, monkeypatch):
    send(world)
    with client(world, monkeypatch, reader=False) as anonymous:
        assert anonymous.get("/api/dsh-drivers/alpha/messages").status_code == 403
    with client(world, monkeypatch) as reader:
        assert reader.get("/api/dsh-drivers/alpha/messages").status_code == 200


def test_the_user_reads_sender_and_time_newest_first_without_the_run_log(world, monkeypatch):
    first = send(world, request_key="one", subject="First report", body="one")
    second = send(world, request_key="two", subject="Second report", body="two")
    # The writer stops: nothing in this read consults its presence or runtime.
    declare(world["db"], "ada", "offline", reason="stopped for the day", actor="driver:ada")

    with client(world, monkeypatch) as reader:
        response = reader.get("/api/dsh-drivers/alpha/messages")
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["project_id"] == "alpha" and page["matched_total"] == 2
        assert [m["subject"] for m in page["messages"]] == ["Second report", "First report"]
        newest = page["messages"][0]
        assert newest["message_id"] == second["message_id"]
        assert newest["sender_driver_id"] == "ada" and newest["created_at"] == second["created_at"]
        assert newest["body"] == "two"
        # A later visit answers the same durable record.
        assert reader.get("/api/dsh-drivers/alpha/messages").json() == page
        # A project with no driver messages is empty, not an error.
        empty = reader.get("/api/dsh-drivers/beta/messages")
        assert empty.status_code == 200 and empty.json()["messages"] == []
        # Unknown project and an out-of-range limit are refused, not answered.
        assert reader.get("/api/dsh-drivers/nonexistent/messages").status_code == 404
        assert reader.get("/api/dsh-drivers/alpha/messages?limit=201").status_code == 422
    assert first["message_id"] != second["message_id"]


def test_the_route_reads_the_same_store_the_driver_wrote(world, monkeypatch):
    """A message written by a SEPARATE process is what the dashboard serves."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    script = """
import json, sys
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
db = sys.argv[1]
service = StateService(StateDatabase(db), actor="driver:ada", driver_id="ada", project_read_trusted=True)
print(json.dumps(execute(service, "send_user_message", {"request_key": "elsewhere",
    "project_id": "alpha", "subject": "Written elsewhere", "body": "still here"},
    allow_write=True)))
"""
    proc = subprocess.run([sys.executable, "-c", script, str(world["db"].db_path)],
                          cwd=repo, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    written = json.loads(proc.stdout.strip().splitlines()[-1])
    with client(world, monkeypatch) as reader:
        page = reader.get("/api/dsh-drivers/alpha/messages").json()
    assert [m["message_id"] for m in page["messages"]] == [written["message_id"]]
    assert page["messages"][0]["body"] == "still here"
