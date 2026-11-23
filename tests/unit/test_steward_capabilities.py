"""The steward's basic-capabilities table, read through its HTTP surface.

Every verdict on the wire comes from a fact: the steward's own route input (the same
reading the driver's route-input surface serves), a usable stored connection's EFFECTIVE
tool set (the same list the gateway serves), the host PDF reader installed here, or
nothing at all. The tests change those facts and watch the SAME row change; nothing here
reads a hardcoded provider table.

The connection URL resolves to an invented public address so registration never reaches
the network; the roles and route tables are small real files in the fixture's tmp dir,
with the steward's own route named in them.
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
from core import dsh_driver
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
OTHER_CONN = "unusablemedia"
URL = "https://agentmcp.example.test/mcp"
STEWARD_PROJECT = "aitelier-home"
TOOLS = [
    {"name": "transcribe_audio", "description": "Transcribe audio"},
    {"name": "generate_speech", "description": "Speak text"},
    {"name": "ocr_image", "description": "Read text in an image"},
    {"name": "ocr_pdf", "description": "Read text in a PDF"},
    {"name": "generate_image", "description": "Draw a picture"},
]


def resolver(host, port):
    """A fixed, distinct answer per host: the documented test range is globally
    routable, and never matches the addresses reserved for the backend's services."""
    address = "93.184.216.40" if host == "agentmcp.example.test" else "93.184.216.41"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


@pytest.fixture
def world(tmp_path, monkeypatch, state_db_copy):
    monkeypatch.setattr(sc, "RESOLVER", resolver)
    monkeypatch.setenv("AITELIER_STEWARD_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("AITELIER_MEDIA_PYTHON", raising=False)
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    DriverRegistry(db, PEPPER).seed("capabilities-test-cli-token")
    # The steward's own driver row: no model override, so roles.json's `steward` route
    # (and its child route) is the one the deployment actually completes on.
    with db.get_connection() as conn:
        conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,enabled,phase,provider,model,session_id,updated_at) "
                     "VALUES(?,?,1,'running','aitelier','','session-steward','t')",
                     (STEWARD_PROJECT, "drv-steward-test",))
        conn.commit()
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


def add_connection(world, conn_id, *, secret=True):
    execute(world.owner, "steward_connection_upsert",
            {"conn_id": conn_id, "label": "Test media tools " + conn_id, "url": URL,
             "visibility": "public", "enabled": True, "auth_kind": "header",
             "header_name": "X-Test-Token"}, allow_write=True)
    sc.record_discovery(world.owner.store, conn_id, TOOLS)
    if secret:
        execute(world.owner, "steward_connection_secret_set",
                {"conn_id": conn_id, "value": "invented-secret"}, allow_write=True)
    return conn_id


@pytest.fixture
def connection(world):
    return add_connection(world, CONN_ID)


@pytest.fixture
def deployment_facts(tmp_path, monkeypatch):
    """A real roles.json and route table. The steward's route is `steward` in both,
    so an image-capable `driver` route cannot accidentally stand in for it."""
    routes_path = tmp_path / "model_routes.json"
    roles_path = tmp_path / "roles.json"

    def write_routes(*, steward_image=True, other_image=True, measured=True):
        doc = {"steward": ["probe/steward-1"], "driver": ["probe/driver-1"],
               "worker": ["probe/worker-1"]}
        if measured:
            doc["_input"] = {
                "probe/steward-1": ["text", "image"] if steward_image else ["text"],
                "probe/driver-1": ["text", "image"] if other_image else ["text"],
                "probe/worker-1": ["text"],
            }
        routes_path.write_text(json.dumps(doc), encoding="utf-8")
        mr.reset_cache()

    def write_roles(*, child_route="steward"):
        roles = dsh_driver.load_roles()
        roles["steward"]["route"] = "steward"
        roles["steward"]["child_route"] = child_route
        roles["director"]["route"] = "driver"
        roles["worker"]["route"] = "worker"
        roles_path.write_text(json.dumps(roles), encoding="utf-8")

    write_roles()
    write_routes()
    monkeypatch.setattr(router, "_routes", lambda: mr.get_routes(routes_path))
    monkeypatch.setattr(router, "_roles", lambda: dsh_driver.load_roles(roles_path))
    yield SimpleNamespace(write_routes=write_routes, write_roles=write_roles)
    mr.reset_cache()


def tick(world, names, conn_id=CONN_ID):
    """The owner's ticks, as PUT /connections/{id}/tools writes them."""
    shown = {t["raw_name"]: t["schema_sha256"]
             for t in execute(world.owner, "list_steward_connection_tools",
                              {"conn_id": conn_id})["tools"]}
    execute(world.owner, "steward_connection_tools_set", {"conn_id": conn_id, "tools": [
        {"raw_name": name, "allowed": True, "schema_sha256": shown[name]} for name in names]},
        allow_write=True)


def untick(world, name, conn_id=CONN_ID):
    execute(world.owner, "steward_connection_tools_set", {"conn_id": conn_id,
            "tools": [{"raw_name": name, "allowed": False}]}, allow_write=True)


def rows(client):
    answer = client.get("/api/steward/capabilities")
    assert answer.status_code == 200, answer.text
    return {row["id"]: row for row in answer.json()["capabilities"]}


def verdicts(table):
    return {cap_id: row["provider"] for cap_id, row in table.items()}


def no_name(row):
    return row["conn_id"] is None and row["tool"] is None and row["detail"] is None


def set_model_override(world, model):
    with world.db.get_connection() as conn:
        conn.execute("UPDATE dsh_drivers SET model=? WHERE project_id=?", (model, STEWARD_PROJECT))
        conn.commit()


# --- the three behaviours from the acceptance criterion --------------------------------------

def test_a_ticked_connection_tool_is_mcp_and_names_that_connection_and_tool(client, world, connection,
                                                                            deployment_facts):
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
        assert no_name(table[cap_id])
    assert table["vision"]["provider"] == "native"


def test_no_connection_names_nobody(client, world, deployment_facts):
    table = rows(client)
    assert len(table) == 18
    # The owner's list is fixed and translation is not on it ("everyone does that").
    assert "translate" not in table
    # The only rows with a deployment fact in this fixture are vision and pdf.
    assert [cap_id for cap_id, row in table.items() if row["provider"] == "native"] == ["vision", "pdf"]
    for cap_id, row in table.items():
        if cap_id not in ("vision", "pdf"):
            assert row["provider"] == "missing", cap_id
            assert no_name(row)
    for cap_id in ("transcribe", "tts", "ocr"):
        assert no_name(table[cap_id])


def test_unticking_one_tool_flips_only_that_row(client, world, connection, deployment_facts):
    tick(world, ["transcribe_audio", "generate_speech"])
    before = rows(client)
    assert before["transcribe"]["provider"] == "mcp"
    assert before["tts"]["provider"] == "mcp"

    untick(world, "transcribe_audio")
    after = rows(client)
    assert after["transcribe"]["provider"] == "missing"
    assert no_name(after["transcribe"])
    # The tick we did not touch keeps its verdict and its names.
    assert after["tts"] == before["tts"]
    for cap_id in before.keys() - {"transcribe"}:
        assert verdicts(after)[cap_id] == verdicts(before)[cap_id]


def test_removing_the_one_deployment_fact_flips_only_vision(client, world, connection, deployment_facts):
    tick(world, ["transcribe_audio"])
    before = rows(client)
    assert before["vision"]["provider"] == "native"
    assert before["vision"]["detail"].startswith("steward route:")
    assert "steward" in before["vision"]["detail"]
    assert before["transcribe"]["provider"] == "mcp"

    deployment_facts.write_routes(steward_image=False)  # same roles: only the steward route goes text-only
    after = rows(client)
    assert after["vision"]["provider"] == "missing"
    assert no_name(after["vision"])
    for cap_id in before.keys() - {"vision"}:
        assert verdicts(after)[cap_id] == verdicts(before)[cap_id], cap_id
        assert after[cap_id] == before[cap_id], cap_id


# --- the two review blockers ---------------------------------------------------------------

def test_another_image_route_cannot_make_the_text_only_steward_native(client, world, deployment_facts):
    """Red on a build that loops over every route: driver takes images, steward does not."""
    deployment_facts.write_routes(steward_image=False, other_image=True)
    table = rows(client)
    assert table["vision"]["provider"] == "missing"
    assert no_name(table["vision"])

    # The owner's per-project override IS part of that resolution: when both the root
    # override and the child route name an image-capable route, the row becomes native.
    deployment_facts.write_roles(child_route="driver")
    set_model_override(world, "driver")
    assert rows(client)["vision"]["provider"] == "native"


def test_a_ticked_tool_on_an_unusable_connection_names_nobody(client, world, connection, deployment_facts):
    """Red on a build that reads raw `allowed` ticks instead of the effective set."""
    add_connection(world, OTHER_CONN, secret=False)
    tick(world, ["generate_speech"], conn_id=OTHER_CONN)
    tick(world, ["transcribe_audio"], conn_id=CONN_ID)
    table = rows(client)
    # The usable connection's tool is still served; the unusable one's is not.
    assert table["transcribe"]["provider"] == "mcp"
    assert table["tts"]["provider"] == "missing"
    assert no_name(table["tts"])


# --- catalog-wide fail-closed and dual-fact rows ---------------------------------------------

def test_a_capability_with_no_fact_and_no_tool_stays_missing(client, world, connection, deployment_facts):
    before = rows(client)
    assert before["calendar"]["provider"] == "missing"
    assert no_name(before["calendar"])

    tick(world, ["generate_image"])  # only the draw row changes
    after = rows(client)
    assert after["draw"]["provider"] == "mcp"
    assert after["draw"]["tool"] == "generate_image"
    for cap_id in before.keys() - {"draw"}:
        assert verdicts(after)[cap_id] == verdicts(before)[cap_id], cap_id
        assert after[cap_id] == before[cap_id], cap_id


def test_a_dual_fact_row_prefers_native_then_falls_to_the_tool(client, world, connection,
                                                               deployment_facts, monkeypatch):
    tick(world, ["ocr_pdf"])  # pdf carries BOTH: the host reader and ocr_pdf
    before = rows(client)
    assert before["pdf"]["provider"] == "native"
    assert before["pdf"]["detail"] == "pdf_read.py"
    assert before["pdf"]["tool"] is None

    # Remove the one host fact: the same row now has a real effective mcp tool.
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", "/nonexistent/reader-python")
    after = rows(client)
    assert after["pdf"]["provider"] == "mcp"
    assert after["pdf"]["conn_id"] == CONN_ID
    assert after["pdf"]["tool"] == "ocr_pdf"
    assert after["pdf"]["detail"] == f"{CONN_ID}: ocr_pdf"
