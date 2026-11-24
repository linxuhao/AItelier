"""The steward's basic-capabilities table, read through its HTTP surface.

Every verdict on the wire comes from a fact: the steward's own route input (the same
reading the driver's route-input surface serves), a usable stored connection's EFFECTIVE
tool set (the same list the gateway serves), the host PDF reader installed here - as
reported by the media interpreter that runs it, never by this process - or nothing at
all. The tests change those facts and watch the SAME row change; nothing here
reads a hardcoded provider table.

The connection URL resolves to an invented public address so registration never reaches
the network; the roles and route tables are small real files in the fixture's tmp dir,
with the steward's own route named in them.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
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
    assert table["tts"]["provider"] == "missing"
    assert no_name(table["tts"])
    # Step 24.4: reading text in an image is the model's own (the owner), like vision.
    assert table["ocr"]["provider"] == "native"
    assert table["vision"]["provider"] == "native"


def test_no_connection_names_nobody(client, world, deployment_facts):
    table = rows(client)
    assert len(table) == 18
    # The owner's list is fixed and translation is not on it ("everyone does that").
    assert "translate" not in table
    # The only rows with a deployment fact in this fixture are vision, ocr, pdf and memory.
    assert [cap_id for cap_id, row in table.items() if row["provider"] == "native"] == ["vision", "ocr", "pdf",
                                                                                         "memory"]
    for cap_id, row in table.items():
        if cap_id not in ("vision", "ocr", "pdf", "memory"):
            assert row["provider"] == "missing", cap_id
            assert no_name(row)
    for cap_id in ("transcribe", "tts"):
        assert no_name(table[cap_id])
    # Step 24.4: ocr is native now - its detail is the steward route, and it names no connection.
    assert table["ocr"]["conn_id"] is None and table["ocr"]["tool"] is None


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
    # Step 24.4: ocr reads the same fact (the model sees the image), so it flips with vision.
    assert before["ocr"]["provider"] == "native" and after["ocr"]["provider"] == "missing"
    for cap_id in before.keys() - {"vision", "ocr"}:
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


# --- the pdf fact is the MEDIA interpreter's answer (iss-800539c24a4e4022) ----------------------
# Measured on 7c7a66cf0d, in aitelier:latest: with AITELIER_MEDIA_PYTHON=/bin/false the panel
# still said "pdf_read.py", and so did a host whose reader interpreter has no pypdfium2 - the
# check read THIS process's modules, not the interpreter pdf_read.py runs under. The reader's
# interpreter is the one that must answer, and every failure to answer is "unavailable".

def probe(python):
    """The fact's own probe, run by hand: the test measures its witnesses, not its verdicts."""
    import subprocess
    return subprocess.run([str(python), "-c", router._PDF_PROBE], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=60, check=False).returncode


def interpreter_without_the_wheel():
    """A real python on this host that cannot import the reader's wheel.

    Measured in aitelier:latest: /usr/bin/python3 exits 3 (no pypdfium2 in its site-packages)
    while the default /usr/local/bin/python exits 0. No candidate is a failure, not a skip:
    this is the unavailable direction and skipping it would leave the defect unwitnessed.
    """
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        if Path(candidate).is_file() and probe(candidate) != 0:
            return candidate
    pytest.fail("no python on this host without pypdfium2 to witness the unavailable direction")


def test_the_pdf_fact_is_unavailable_when_the_readers_interpreter_lacks_the_wheel(
        client, world, deployment_facts, monkeypatch):
    """A real interpreter that answers no: the row is `missing`, not `native`."""
    python = interpreter_without_the_wheel()
    assert probe(python) == 3, (python, probe(python))  # the measured witness: find_spec is None
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", python)
    assert router._native_fact("pdf_reader", world.owner) is None
    table = rows(client)
    assert table["pdf"]["provider"] == "missing"
    assert no_name(table["pdf"])


def test_a_media_interpreter_that_refuses_is_unavailable(client, world, deployment_facts, monkeypatch):
    """The exact base-defect probe: /bin/false exits 1, so the reader cannot be claimed."""
    assert probe("/bin/false") != 0
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", "/bin/false")
    assert router._native_fact("pdf_reader", world.owner) is None
    assert rows(client)["pdf"]["provider"] == "missing"


def test_the_default_interpreter_with_the_wheel_still_reports_the_reader(
        client, world, deployment_facts, monkeypatch):
    """The available direction: the reader's interpreter answers yes, so the fact stands."""
    assert probe(sys.executable) == 0, sys.executable  # this container's python has the wheel
    monkeypatch.delenv("AITELIER_MEDIA_PYTHON", raising=False)
    assert router._native_fact("pdf_reader", world.owner) == "pdf_read.py"
    assert rows(client)["pdf"]["detail"] == "pdf_read.py"
    # Naming that same interpreter explicitly is the same answer, not a second rule.
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", sys.executable)
    assert router._native_fact("pdf_reader", world.owner) == "pdf_read.py"


def test_a_missing_or_non_executable_media_python_is_unavailable_not_an_exception(
        client, world, deployment_facts, monkeypatch, tmp_path):
    """A path that cannot be run is a fact about the host, never a 500 from the panel."""
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", str(tmp_path / "no-such-python"))
    assert router._native_fact("pdf_reader", world.owner) is None
    assert rows(client)["pdf"]["provider"] == "missing"

    plain = tmp_path / "not-executable-python"
    plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    plain.chmod(0o644)
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", str(plain))
    assert router._native_fact("pdf_reader", world.owner) is None
    assert rows(client)["pdf"]["provider"] == "missing"


def test_a_wedged_media_interpreter_is_unavailable_within_the_probe_bound(
        client, world, deployment_facts, monkeypatch, tmp_path):
    """A panel request must not hang behind an interpreter that never answers."""
    import time
    wedged = tmp_path / "wedged-python"
    wedged.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    wedged.chmod(0o755)
    monkeypatch.setenv("AITELIER_MEDIA_PYTHON", str(wedged))
    started = time.monotonic()
    assert router._native_fact("pdf_reader", world.owner) is None
    returned = time.monotonic() - started
    assert returned < router._PDF_PROBE_TIMEOUT_SECONDS + 2.0, returned
    assert rows(client)["pdf"]["provider"] == "missing"


# --- step 24.4: the settable routes (design-steward 7.8.24) --------------------------------------

SIX = ("id", "label", "provider", "conn_id", "tool", "detail")
# The director's read-only table exactly as main had it before step 24.4, and its read: the
# empty-table answer is compared with it field by field, on the same facts.
PRE_24_4 = (
    ("vision", "看图 (Vision)", None, "steward_image_route"), ("ocr", "图片转文字 (OCR)", "ocr_image", None),
    ("pdf", "读 PDF", "ocr_pdf", "pdf_reader"), ("office_read", "读 Word/Excel/PPT", None, None),
    ("office_write", "生成 Office 文档", None, None), ("transcribe", "语音转文字", "transcribe_audio", None),
    ("tts", "发语音条", "generate_speech", None), ("draw", "画图", "generate_image", None),
    ("music", "生成音乐", "generate_music", None), ("sfx", "音效", "gen_sfx", None), ("video", "视频", None, None),
    ("search", "联网搜索", "web_search", None), ("fetch", "读网页", "web_fetch", None), ("email", "邮件", None, None),
    ("calendar", "日历/提醒", None, None), ("cloud_files", "网盘", None, None),
    ("memory", "记忆与历史对话", None, None), ("code", "计算/跑代码", None, None))
MEDIA_TOOLS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "steward_media_tools.json")
                         .read_text(encoding="utf-8"))["tools"]


def pre_change_rows(world):
    listed = router._listed_connections(world.owner)
    out = {}
    for cap_id, label, tool_name, native_fact in PRE_24_4:
        fact = router._native_fact(native_fact, world.owner) if native_fact else None
        found = router._mcp_tool(listed, tool_name) if not fact and tool_name else None
        conn_id, tool = found or (None, None)
        out[cap_id] = {"id": cap_id, "label": label, "provider": "native" if fact else "mcp" if tool else "missing",
                       "conn_id": conn_id, "tool": tool, "detail": fact or (f"{conn_id}: {tool}" if tool else None)}
    return out


def six(row):
    return {key: row[key] for key in SIX}


def test_an_empty_table_answers_the_six_fields_as_before_and_adds_no_basics(client, world, connection,
                                                                            deployment_facts):
    """c1: no row set - every row's six detection fields are the pre-24.4 read's, except the
    three the design changes (ocr and memory are native, calendar's fallback label)."""
    tick(world, ["transcribe_audio", "ocr_image", "generate_image"])
    table, before = rows(client), pre_change_rows(world)
    assert list(table) == [cap_id for cap_id, *_ in PRE_24_4]
    for cap_id, row in table.items():
        if cap_id not in ("ocr", "memory", "calendar"):
            assert six(row) == before[cap_id], cap_id
        assert row["setting"] is None and row["proposal"] is None, cap_id
        assert row["status"] == {"dot": "grey", "reasons": ["not_set"]}, cap_id
        assert set(row["spec"]) == {"allows", "host_tools", "private_only"}, cap_id
    # The designed differences, and nothing else in those rows.
    assert before["ocr"]["provider"] == "mcp" and table["ocr"]["provider"] == "native"
    assert table["ocr"]["detail"].startswith("steward route:") and table["ocr"]["conn_id"] is None
    assert before["memory"]["provider"] == "missing"
    assert six(table["memory"]) == {**before["memory"], "provider": "native", "detail": "plugin"}
    assert six(table["calendar"]) == {**before["calendar"], "label": "日历"}
    # The steward's connection read is today's: no basics field at all.
    listed = execute(world.owner, "list_steward_connections", {})
    assert set(listed) == {"connections", "effective_tools", "effective_hash", "restart"}


def test_any_connection_tool_can_be_set_as_a_route_and_detection_is_unchanged(client, world, connection,
                                                                              deployment_facts):
    """c2: a usable tool no capability lists (paint_v2, found only by its description) set
    as draw's route; the row names that connection and tool, its detection is the same."""
    sc.record_discovery(world.owner.store, CONN_ID, MEDIA_TOOLS)
    tick(world, ["paint_v2"])
    before = rows(client)
    assert before["draw"]["provider"] == "missing" and before["draw"]["setting"] is None
    answer = client.put("/api/steward/capabilities/draw", json={
        "request_key": "c2-draw", "expect_version": 0,
        "route": [{"kind": "conn", "conn_id": CONN_ID, "raw_name": "paint_v2"}]})
    assert answer.status_code == 200, answer.text
    after = rows(client)
    route = after["draw"]["setting"]["route"]
    assert [(e["kind"], e["conn_id"], e["raw_name"], e["reason"], e["visibility"]) for e in route] == \
        [("conn", CONN_ID, "paint_v2", "ok", "public")]
    assert after["draw"]["setting"]["version"] == 1 and after["draw"]["setting"]["set_via"] == "dashboard"
    assert after["draw"]["status"] == {"dot": "green", "reasons": ["ok"]}
    # Detection never reads the setting: every row's six fields are what they were.
    for cap_id in before:
        assert six(after[cap_id]) == six(before[cap_id]), cap_id
    assert after["draw"]["provider"] == "missing" and no_name(after["draw"])


def test_a_pin_that_stops_resolving_shows_its_reason_beside_the_live_detection(client, world, connection,
                                                                              deployment_facts):
    """c4: the pinned tool unticked, then its connection deleted: the entry carries the reason,
    detection (the grey suggestion) is computed as always, and the stored route never moves."""
    sc.record_discovery(world.owner.store, CONN_ID, MEDIA_TOOLS)
    tick(world, ["paint_v2", "generate_image"])
    assert client.put("/api/steward/capabilities/draw", json={
        "request_key": "c4-draw", "expect_version": 0,
        "route": [{"kind": "conn", "conn_id": CONN_ID, "raw_name": "paint_v2"}]}).status_code == 200
    pinned = rows(client)["draw"]["setting"]["route"]
    untick(world, "paint_v2")
    draw = rows(client)["draw"]
    assert [e["reason"] for e in draw["setting"]["route"]] == ["tool_not_allowed"]
    assert draw["status"] == {"dot": "red", "reasons": ["tool_not_allowed"]}
    # The suggestion is detection's, live: the ticked generate_image of the same connection.
    assert (draw["provider"], draw["conn_id"], draw["tool"]) == ("mcp", CONN_ID, "generate_image")
    execute(world.owner, "steward_connection_delete", {"conn_id": CONN_ID}, allow_write=True)
    draw = rows(client)["draw"]
    assert [e["reason"] for e in draw["setting"]["route"]] == ["tool_gone"]
    assert draw["provider"] == "missing" and no_name(draw)
    assert [{k: e[k] for k in ("kind", "conn_id", "conn_uid", "raw_name")} for e in draw["setting"]["route"]] == \
        [{k: e[k] for k in ("kind", "conn_id", "conn_uid", "raw_name")} for e in pinned]
    assert draw["setting"]["version"] == 1
