"""The steward's basic-capabilities table, read through its HTTP surface.

Every verdict on the wire has to come from a fact: a tool the owner ticked on a stored
MCP connection (GET /api/steward/connections -> the connection's tools), or a deployment
fact the backend can read itself (the measured image input of the model routes the driver's
model bridge uses). The tests change those facts and watch the SAME row change; nothing
here reads a hardcoded provider table.

The connection URL resolves to an invented public address so registration never reaches
the network; the route table is a small real `model_routes.json` in the fixture's tmp dir.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.steward_routers as router
from api.main import app
from api.state_graph_routers import get_service
from core import model_routes as mr
from core import state_steward_connections as sc
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

OWNER = "owner:owner@example.test"
PEPPER = "capabilities-test-pepper-" * 3
CONN_ID = "agentmedia"
URL = "https://agentmcp.example.test/mcp"
TOOLS = [
    {"name": "transcribe_audio", "description": "Transcribe audio"},
    {"name": "generate_speech", "description": "Speak text"},
    {"name": "ocr_image", "description": "Read text in an image"},
]


def resolver(host, port):
    """A fixed, distinct answer per host: the documented test range is globally
    routable, and never matches the addresses reserved for the backend's services."""
    address = "93.184.216.40" if host == "agentmcp.example.test" else "93.184.216.41"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


@pytest.fixture
def world(tmp_path, monkeypatch, state_db_copy):
    monkeypatch.setattr(sc, "RESOLVER", resolver)
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    DriverRegistry(db, PEPPER).seed("capabilities-test-cli-token")
    owner = StateService(db, actor=OWNER, project_read_trusted=True)
    return SimpleNamespace(db=db, owner=owner)


@pytest.fixture
def client(world):
    app.state._test_mode = True
    app.dependency_overrides[get_service] = lambda: world.owner
    try:
        with TestClient(app) as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_service, None)
        app.state._test_mode = False


@pytest.fixture
def connection(world):
    execute(world.owner, "steward_connection_upsert",
            {"conn_id": CONN_ID, "label": "Test media tools", "url": URL,
             "visibility": "public", "enabled": True}, allow_write=True)
    sc.record_discovery(world.owner.store, CONN_ID, TOOLS)
    return CONN_ID


@pytest.fixture
def route_input_file(tmp_path, monkeypatch):
    """A real routes file: the image fact is present while `_input` measures an
    image-capable candidate, and absent once that one fact is removed."""
    path = tmp_path / "model_routes.json"

    def write(*, image: bool):
        path.write_text(json.dumps({"visionprobe": ["probe/see-1"],
                                    "_input": {"probe/see-1": ["text", "image"] if image else ["text"]}}),
                        encoding="utf-8")
        mr.reset_cache()

    write(image=True)
    monkeypatch.setattr(router, "_routes", lambda: mr.get_routes(path))
    mr.reset_cache()
    yield write
    mr.reset_cache()


def tick(world, names):
    """The owner's ticks, as PUT /connections/{id}/tools writes them."""
    shown = {t["raw_name"]: t["schema_sha256"]
             for t in execute(world.owner, "list_steward_connection_tools",
                              {"conn_id": CONN_ID})["tools"]}
    execute(world.owner, "steward_connection_tools_set", {"conn_id": CONN_ID, "tools": [
        {"raw_name": name, "allowed": True, "schema_sha256": shown[name]} for name in names]},
        allow_write=True)


def untick(world, name):
    execute(world.owner, "steward_connection_tools_set", {"conn_id": CONN_ID,
            "tools": [{"raw_name": name, "allowed": False}]}, allow_write=True)


def rows(client):
    answer = client.get("/api/steward/capabilities")
    assert answer.status_code == 200, answer.text
    return {row["id"]: row for row in answer.json()["capabilities"]}


def verdicts(table):
    return {cap_id: row["provider"] for cap_id, row in table.items()}


def test_a_ticked_connection_tool_is_mcp_and_names_that_connection_and_tool(client, world, connection,
                                                                            route_input_file):
    tick(world, ["transcribe_audio"])
    table = rows(client)
    transcribe = table["transcribe"]
    assert transcribe["provider"] == "mcp"
    assert transcribe["conn_id"] == CONN_ID
    assert transcribe["tool"] == "transcribe_audio"
    assert CONN_ID in transcribe["detail"] and "transcribe_audio" in transcribe["detail"]
    # Ticking one tool does not carry the other rows with it.
    for cap_id in ("tts", "ocr"):
        assert table[cap_id]["provider"] == "missing"
        assert table[cap_id]["conn_id"] is None and table[cap_id]["tool"] is None
    assert table["vision"]["provider"] == "native"


def test_no_connection_names_nobody(client, world, route_input_file):
    table = rows(client)
    for cap_id in ("transcribe", "tts", "ocr"):
        assert table[cap_id]["provider"] == "missing"
        assert table[cap_id]["conn_id"] is None and table[cap_id]["tool"] is None
        assert table[cap_id]["detail"] is None


def test_unticking_one_tool_flips_only_that_row(client, world, connection, route_input_file):
    tick(world, ["transcribe_audio", "generate_speech"])
    before = rows(client)
    assert before["transcribe"]["provider"] == "mcp"
    assert before["tts"]["provider"] == "mcp"

    untick(world, "transcribe_audio")
    after = rows(client)
    assert after["transcribe"]["provider"] == "missing"
    assert after["transcribe"]["conn_id"] is None
    assert after["transcribe"]["tool"] is None
    assert after["transcribe"]["detail"] is None
    # The tick we did not touch keeps its verdict and its names.
    assert after["tts"] == before["tts"]
    for cap_id in before.keys() - {"transcribe"}:
        assert verdicts(after)[cap_id] == verdicts(before)[cap_id]


def test_removing_the_one_deployment_fact_flips_only_vision(client, world, connection, route_input_file):
    tick(world, ["transcribe_audio"])
    before = rows(client)
    assert before["vision"]["provider"] == "native"
    assert before["vision"]["detail"] == "model_routes:visionprobe"
    assert before["transcribe"]["provider"] == "mcp"

    route_input_file(image=False)  # same file, no measured image input
    after = rows(client)
    assert after["vision"]["provider"] == "missing"
    assert after["vision"]["detail"] is None
    assert after["vision"]["conn_id"] is None
    for cap_id in before.keys() - {"vision"}:
        assert verdicts(after)[cap_id] == verdicts(before)[cap_id], cap_id
        assert after[cap_id] == before[cap_id], cap_id
