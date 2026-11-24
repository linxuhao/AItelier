"""Step 24.4: the owner's basic-capabilities routes (design-steward 7.8.24 "三", "四", "八",
tests "十四").

The fixed list, the route rules (one test per refusal code), who may write, the row version,
the order kept, what the steward is given (`basics` on list_steward_connections), how a pin
that stops resolving is reported (never rebound), and the connection side's
`pinned_private_only`. The writes go through the State commands and the owner's HTTP routes."""
from __future__ import annotations

import itertools
import json
import re

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, state_steward_capabilities as caps
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_graph import StateConflict, StateGraphError, canonical
from tests.unit.test_steward_connections import (OWNER, add, discover, listed, resolver, shown,  # noqa: F401
                                                 world)

_keys = itertools.count()
TOOLS = [
    {"name": "web_search", "description": "Search the web", "inputSchema": {"type": "object"}},
    {"name": "ocr_pdf", "description": "Read a PDF", "inputSchema": {"type": "object"}},
    {"name": "paint_v2", "description": "根据文字描述生成图片", "inputSchema": {"type": "object"}},
    {"name": "generate_image", "description": "Draw", "inputSchema": {"type": "object"}},
    {"name": "actor_tts", "description": "Speak as an actor", "inputSchema": {"type": "object"}},
    {"name": "video_render_shot", "description": "Render a shot", "inputSchema": {"type": "object"}},
    {"name": "notes_search", "description": "Search notes", "inputSchema": {"type": "object"}},
    {"name": "run", "description": "Run Python", "inputSchema": {"type": "object"}},
    {"name": "send_message", "description": "Send a mail", "inputSchema": {"type": "object"}},
]


def key(prefix="cap"):
    return f"{prefix}-{next(_keys)}"


def ready(world, conn_id="media", visibility="public", names=("paint_v2",), tools=TOOLS, **kw):
    """A usable connection (auth none) listing `tools`, with `names` allowed."""
    add(world, conn_id=conn_id, visibility=visibility, url=kw.pop("url", "http://mcp_server:9003/mcp"),
        label=kw.pop("label", conn_id.title()), **kw)
    discover(world, conn_id, tools=tools)
    if names:
        allow(world, conn_id, list(names))
    return conn_id


def allow(world, conn_id, names, allowed=True):
    hashes = shown(world, conn_id)
    execute(world.owner, "steward_connection_tools_set", {"conn_id": conn_id, "tools": [
        {"raw_name": n, "allowed": allowed, **({"schema_sha256": hashes[n]} if allowed else {})} for n in names]},
        allow_write=True)


def conn(conn_id, raw_name):
    return {"kind": "conn", "conn_id": conn_id, "raw_name": raw_name}


def put(world, cap_id, route, expect=0, service=None, request_key=None):
    return execute(service or world.owner, "steward_capability_set", {
        "request_key": request_key or key(), "cap_id": cap_id, "expect_version": expect, "route": route},
        allow_write=True)


def unset(world, cap_id, expect, service=None):
    return execute(service or world.owner, "steward_capability_set", {
        "request_key": key(), "cap_id": cap_id, "expect_version": expect, "unset": True}, allow_write=True)


def clear(world, service=None):
    return execute(service or world.owner, "steward_capabilities_clear", {"request_key": key()}, allow_write=True)


def stored(world):
    with world.db.get_connection() as c:
        return {r[0]: (json.loads(r[1]), r[2], r[3]) for r in c.execute(
            "SELECT cap_id,route_json,version,set_via FROM state_steward_capabilities")}


def basics(world, service=None):
    return listed(world, service).get("basics")


def table(world):
    return world.owner.steward_capabilities.rows()


def kinds(route):
    return [(e["kind"], e.get("tool") or e.get("raw_name")) for e in route]


# --- the fixed list ----------------------------------------------------------------------------

IDS = ["vision", "ocr", "pdf", "office_read", "office_write", "transcribe", "tts", "draw", "music", "sfx", "video",
       "search", "fetch", "email", "calendar", "cloud_files", "memory", "code"]
FIELDS = {"id", "label", "tool", "native_fact", "allows", "desc", "host_tools", "host_check", "native_tools",
          "private_only", "missing_hint"}
STOPWORDS = {"a", "an", "the", "to", "of", "from", "and", "or", "in", "on", "for", "with", "by", "as", "at", "is", "it"}


def test_the_list_is_fixed_and_each_entry_has_exactly_its_fields():
    assert [cap["id"] for cap in caps.CAPABILITIES] == IDS
    for cap in caps.CAPABILITIES:
        assert set(cap) == FIELDS, cap["id"]
        assert set(cap["allows"]) <= {"native", "host", "conn", "missing"}, cap["id"]
    by = caps.BY_ID
    assert set(by["memory"]["allows"]) == {"native", "conn"}
    assert set(by["vision"]["allows"]) == {"native", "missing"}
    assert [c["id"] for c in caps.CAPABILITIES if c["private_only"]] == ["memory"]
    assert [c["id"] for c in caps.CAPABILITIES if c["host_tools"]] == ["office_read", "office_write", "code"]
    assert by["code"]["host_tools"] == ("calculate",) and by["office_read"]["host_check"] == "make_document"
    assert (by["ocr"]["native_fact"], by["memory"]["native_fact"]) == ("steward_image_route", "plugin_builtin")
    assert by["calendar"]["label"] == "日历" and "Reminders" in by["calendar"]["missing_hint"]


def test_each_desc_is_keywords_search_tools_can_use():
    for cap in caps.CAPABILITIES:
        words = re.findall(r"[^\W_]+", cap["desc"])  # searchTools' /[\p{L}\p{N}]+/u
        assert words, cap["id"]
        for word in words:
            assert len(word) >= 3 and word.lower() not in STOPWORDS, (cap["id"], word)


def test_the_shared_state_tools():
    for name in ("actor_tts", "create_character", "create_object", "delete_subject", "delete_actor"):
        assert caps.SHARED_STATE_TOOLS.match(name), name
    # video_* stays an ordinary tool (owner 2026-11-24, decision 33).
    for name in ("video_render_shot", "video_project_create", "generate_image", "list_actors", "actor_ttsx"):
        assert not caps.SHARED_STATE_TOOLS.match(name), name


# --- the route rules: one test per refusal code ----------------------------------------------

@pytest.mark.parametrize("cap_id, route, code", [
    ("draw", [{"kind": "missing"}, conn("media", "paint_v2")], "entry_not_allowed"),
    ("pdf", [conn("media", "ocr_pdf"), {"kind": "native"}], "entry_not_allowed"),
    ("pdf", [{"kind": "native"}, {"kind": "native"}], "entry_not_allowed"),
    ("memory", [conn("notes", "notes_search")], "entry_not_allowed"),
    ("memory", [{"kind": "missing"}], "entry_not_allowed"),
    ("vision", [{"kind": "native"}, conn("media", "paint_v2")], "entry_not_allowed"),
    ("draw", [{"kind": "teleport"}], "entry_not_allowed"),
    ("draw", [{"kind": "conn", "conn_id": "media"}], "entry_not_allowed"),
    ("draw", [], "route_empty"),
    ("email", [conn("media", n) for n in ("web_search", "ocr_pdf", "paint_v2", "generate_image", "actor_tts",
                                          "video_render_shot", "notes_search", "run", "send_message")],
     "too_many_entries"),
    ("draw", [conn("media", "paint_v2"), conn("media", "paint_v2")], "duplicate_entry"),
    ("code", [{"kind": "host", "tool": "calculate"}, {"kind": "host", "tool": "calculate"}], "duplicate_entry"),
    ("draw", [{"kind": "host", "tool": "calculate"}], "host_tool_not_allowed"),
    ("code", [{"kind": "host", "tool": "make_document"}], "host_tool_not_allowed"),
    ("draw", [conn("nosuch", "paint_v2")], "unknown_connection"),
    ("draw", [{**conn("media", "paint_v2"), "conn_uid": "not-the-current-one"}], "unknown_connection"),
    ("draw", [conn("media", "paint_v3")], "unknown_tool"),
    ("translate", [{"kind": "native"}], "unknown_capability"),
])
def test_a_route_is_refused_with_its_code_and_the_stored_row_is_untouched(world, cap_id, route, code):
    ready(world, names=())
    ready(world, conn_id="notes", visibility="private", names=())
    first = put(world, "draw", [{"kind": "missing"}])
    with pytest.raises(StateGraphError) as refused:
        put(world, cap_id, route, expect=1 if cap_id == "draw" else 0)
    assert str(refused.value).split(":")[0] == code
    assert stored(world) == {"draw": ([{"kind": "missing"}], 1, "dashboard")}
    assert first["setting"]["version"] == 1


def test_routes_that_save(world):
    ready(world, names=())
    ready(world, conn_id="mailme", visibility="private", names=())
    put(world, "pdf", [{"kind": "native"}, conn("media", "ocr_pdf")])
    put(world, "email", [conn("media", "send_message"), conn("mailme", "send_message")])
    put(world, "code", [{"kind": "host", "tool": "calculate"}, conn("media", "run")])
    put(world, "draw", [conn("media", "paint_v2")])  # any name: no capability lists paint_v2
    put(world, "office_read", [{"kind": "host", "tool": "read_document"}])
    put(world, "calendar", [{"kind": "missing"}])
    put(world, "memory", [{"kind": "native"}, conn("mailme", "notes_search")])
    # Pinning is not allowing: none of these tools is allowed, and they all saved.
    # One tool may serve two jobs.
    put(world, "ocr", [conn("media", "ocr_pdf")])
    # Decision 33 (owner 2026-11-24): a video tool on a PUBLIC connection is an ordinary pin.
    put(world, "video", [conn("media", "video_render_shot")])
    uid = {c["conn_id"]: c["conn_uid"] for c in listed(world)["connections"]}
    assert stored(world)["email"][0] == [{"kind": "conn", "conn_id": "media", "conn_uid": uid["media"],
                                          "raw_name": "send_message"},
                                         {"kind": "conn", "conn_id": "mailme", "conn_uid": uid["mailme"],
                                          "raw_name": "send_message"}]


def test_a_private_only_job_refuses_a_public_connection(world):
    """Mutation-sensitive: only the job's private_only refuses this (notes_search is no
    shared-state tool)."""
    ready(world, names=())
    ready(world, conn_id="notes", visibility="private", names=())
    with pytest.raises(StateGraphError, match="^pin_must_be_private"):
        put(world, "memory", [{"kind": "native"}, conn("media", "notes_search")])
    assert put(world, "memory", [{"kind": "native"}, conn("notes", "notes_search")])["setting"]["version"] == 1


def test_a_shared_state_tool_refuses_a_public_connection_in_any_job(world):
    """Mutation-sensitive: only SHARED_STATE_TOOLS refuses this (tts is not private_only)."""
    ready(world, names=())
    ready(world, conn_id="actors", visibility="private", names=())
    with pytest.raises(StateGraphError, match="^pin_must_be_private"):
        put(world, "tts", [conn("media", "actor_tts")])
    assert put(world, "tts", [conn("actors", "actor_tts")])["setting"]["version"] == 1


# --- who writes, the version, the request key -------------------------------------------------

def test_the_writes_are_the_owner_sessions_alone(world):
    ready(world)
    assert not {"steward_capability_set", "steward_capabilities_clear"} & STEWARD_WRITE_ACTIONS
    for service, code in ((world.steward, "steward_read_only"), (world.director, "owner_session_required")):
        with pytest.raises(StateGraphError, match=code):
            put(world, "draw", [conn("media", "paint_v2")], service=service)
        with pytest.raises(StateGraphError, match=code):
            unset(world, "draw", 0, service=service)
        with pytest.raises(StateGraphError, match=code):
            clear(world, service=service)
    with pytest.raises(StateGraphError, match="owner_session_required"):
        world.director.steward_capabilities.rows()
    assert stored(world) == {}


def test_a_stale_version_is_refused_and_the_first_writer_keeps_the_row(world):
    ready(world, names=())
    first = put(world, "draw", [conn("media", "paint_v2")], expect=0)
    with pytest.raises(StateConflict, match="version_conflict"):
        put(world, "draw", [conn("media", "generate_image")], expect=0)
    with pytest.raises(StateConflict, match="version_conflict"):
        unset(world, "draw", 0)
    assert stored(world)["draw"] == (first["setting"]["route"], 1, "dashboard")
    second = put(world, "draw", [conn("media", "generate_image")], expect=1)
    assert second["setting"]["version"] == 2


def test_a_request_key_answers_the_same_result_and_another_content_is_refused(world):
    ready(world, names=())
    once = put(world, "draw", [conn("media", "paint_v2")], request_key="same-key")
    assert put(world, "draw", [conn("media", "paint_v2")], request_key="same-key") == once
    assert stored(world)["draw"][1] == 1
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        put(world, "draw", [conn("media", "generate_image")], request_key="same-key")


def test_a_route_reads_back_in_its_order_everywhere(world):
    ready(world, names=("send_message",))
    ready(world, conn_id="mailme", visibility="private", names=("send_message",))
    put(world, "email", [conn("mailme", "send_message"), conn("media", "send_message")])
    assert [e["conn_id"] for e in stored(world)["email"][0]] == ["mailme", "media"]
    assert [e["conn_id"] for e in table(world)["email"]["setting"]["route"]] == ["mailme", "media"]
    rows = basics(world, world.steward)["rows"]
    assert rows == [{"id": "email", "route": [
        {"kind": "conn", "visibility": "private", "pinned_name": "mcp__sw__mailme__send_message", "available": True},
        {"kind": "conn", "visibility": "public", "pinned_name": "mcp__sw__media__send_message", "available": True}]}]


# --- unset, clear, a job no longer on the list ------------------------------------------------

def test_unset_and_clear_go_back_to_the_empty_table_byte_for_byte(world):
    ready(world)
    empty = canonical(listed(world))
    assert "basics" not in listed(world)
    put(world, "draw", [conn("media", "paint_v2")])
    put(world, "calendar", [{"kind": "missing"}])
    assert [r["id"] for r in basics(world)["rows"]] == ["draw", "calendar"]
    unset(world, "draw", 1)
    assert [r["id"] for r in basics(world)["rows"]] == ["calendar"]
    assert table(world)["draw"]["setting"] is None
    assert clear(world) == {"cleared": 1}
    assert canonical(listed(world)) == empty
    # A job the list no longer has (written straight into the database) is ignored, then cleared.
    with world.db.get_connection() as c:
        c.execute("INSERT INTO state_steward_capabilities VALUES('translate','[{\"kind\": \"native\"}]',1,"
                  "'dashboard',NULL,'t')")
        c.commit()
    assert canonical(listed(world)) == empty and "translate" not in table(world)
    clear(world)
    assert stored(world) == {}


def test_a_set_removes_a_job_no_longer_on_the_list(world):
    ready(world)
    with world.db.get_connection() as c:
        c.execute("INSERT INTO state_steward_capabilities VALUES('translate','[]',1,'dashboard',NULL,'t')")
        c.commit()
    put(world, "draw", [conn("media", "paint_v2")])
    assert set(stored(world)) == {"draw"}


# --- status: a pin that stops resolving is told, never rebound ---------------------------------

def codes(world, cap_id):
    return [e["reason"] for e in table(world)[cap_id]["setting"]["route"]]


def test_each_status_code_and_the_dot(world):
    ready(world, names=("paint_v2", "generate_image"))
    route = [conn("media", "paint_v2"), conn("media", "generate_image")]
    put(world, "draw", route)
    assert table(world)["draw"]["status"] == {"dot": "green", "reasons": ["ok", "ok"]}
    allow(world, "media", ["paint_v2"], allowed=False)
    assert table(world)["draw"]["status"] == {"dot": "yellow", "reasons": ["fallback_in_use", "tool_not_allowed", "ok"]}
    allow(world, "media", ["generate_image"], allowed=False)
    assert table(world)["draw"]["status"]["dot"] == "red"
    allow(world, "media", ["paint_v2", "generate_image"])
    # A definition changed upstream since it was allowed.
    changed = [dict(t, description="Draw better") if t["name"] == "paint_v2" else t for t in TOOLS]
    discover(world, "media", tools=changed)
    assert codes(world, "draw") == ["tool_changed", "ok"]
    allow(world, "media", ["paint_v2"])
    # The connection's last error is health: yellow, still available to the steward.
    with world.db.get_connection() as c:
        c.execute("UPDATE state_steward_connections SET last_error='probe_failed:X' WHERE conn_id='media'")
        c.commit()
    assert table(world)["draw"]["status"] == {"dot": "yellow", "reasons": ["connection_error", "connection_error"]}
    assert [e["available"] for e in basics(world)["rows"][0]["route"]] == [True, True]
    add(world, conn_id="media", visibility="public", enabled=False, url="http://mcp_server:9003/mcp", label="Media")
    assert codes(world, "draw") == ["connection_off", "connection_off"]
    assert [e["available"] for e in basics(world)["rows"][0]["route"]] == [False, False]
    assert [e["pinned_name"] for e in basics(world)["rows"][0]["route"]] == [None, None]
    # Native, host and missing entries are the driver status's and the missing mark.
    put(world, "pdf", [{"kind": "native"}, conn("media", "ocr_pdf")])
    put(world, "code", [{"kind": "host", "tool": "calculate"}])
    put(world, "calendar", [{"kind": "missing"}])
    rows = table(world)
    assert rows["pdf"]["status"] == {"dot": "grey", "reasons": ["see_driver_status", "tool_not_allowed"]}
    assert rows["code"]["status"] == {"dot": "grey", "reasons": ["see_driver_status"]}
    assert rows["calendar"]["status"] == {"dot": "grey", "reasons": ["missing"]}
    assert rows["music"]["status"] == {"dot": "grey", "reasons": ["not_set"]}


def test_an_unticked_or_vanished_tool_is_reported_and_the_route_never_moves(world):
    """c4: unticked -> tool_not_allowed, the upstream no longer listing it -> tool_gone; the
    stored route is never edited by either."""
    ready(world, names=("paint_v2",))
    put(world, "draw", [conn("media", "paint_v2")])
    route = stored(world)["draw"]
    allow(world, "media", ["paint_v2"], allowed=False)
    assert codes(world, "draw") == ["tool_not_allowed"] and stored(world)["draw"] == route
    discover(world, "media", tools=[t for t in TOOLS if t["name"] != "paint_v2"])
    assert codes(world, "draw") == ["tool_gone"] and stored(world)["draw"] == route
    assert basics(world)["rows"] == [{"id": "draw", "route": [
        {"kind": "conn", "visibility": "public", "pinned_name": None, "available": False}]}]
    # Listed again it is a NEW tool, off until allowed: still not resolving to an allowed tool.
    discover(world, "media", tools=TOOLS)
    assert codes(world, "draw") == ["tool_not_allowed"] and stored(world)["draw"] == route


@pytest.mark.parametrize("how", ["dashboard", "proposal"])
def test_a_connection_deleted_and_added_again_under_its_id_does_not_resolve(world, how):
    """c4: the pin keeps its conn_uid; a connection added again under the same conn_id (from
    the dashboard, or by an approved add proposal; public now, listing the same tool) is
    another connection."""
    ready(world, conn_id="notes", visibility="private", names=("notes_search",))
    put(world, "memory", [{"kind": "native"}, conn("notes", "notes_search")])
    route = stored(world)["memory"]
    execute(world.owner, "steward_connection_delete", {"conn_id": "notes"}, allow_write=True)
    assert codes(world, "memory") == ["see_driver_status", "tool_gone"]
    if how == "dashboard":
        ready(world, conn_id="notes", visibility="public", names=("notes_search",))
    else:
        from tests.unit.test_steward_connection_proposals import approve as approve_change, owner_source
        pid = execute(world.steward, "create_steward_proposal", {
            "request_key": key("add"), "kind": "connection_change", "reason": "r", "payload": {
                "op": "add", "conn_id": "notes", "source": owner_source(world), "label": "Notes",
                "url": "http://mcp_server:9003/mcp", "transport": "streamable-http", "auth_kind": "none",
                "visibility": "public"}}, allow_write=True)["proposal_id"]
        assert approve_change(world, pid)["status"] == "done"
        add(world, conn_id="notes", visibility="public", url="http://mcp_server:9003/mcp", label="Notes")
        discover(world, "notes", tools=TOOLS)
        allow(world, "notes", ["notes_search"])
    assert codes(world, "memory") == ["see_driver_status", "tool_gone"]
    assert stored(world)["memory"] == route
    rows = basics(world, world.steward)["rows"]
    assert rows == [{"id": "memory", "route": [{"kind": "native", "available": True},
                                               {"kind": "conn", "visibility": None, "pinned_name": None,
                                                "available": False}]}]
    # The new public tool is in the effective set (allowed), but no pin names it.
    assert "mcp__sw__notes__notes_search" in listed(world)["effective_tools"]
    assert table(world)["memory"]["setting"]["route"][1]["visibility"] is None


# --- the connection side: a private-only pin keeps its connection private ----------------------

def test_a_connection_with_a_private_only_pin_cannot_be_made_public(world):
    ready(world, conn_id="notes", visibility="private", names=())
    ready(world, conn_id="actors", visibility="private", names=())
    ready(world, conn_id="other", visibility="private", names=(), url="https://good.example/mcp")
    put(world, "memory", [{"kind": "native"}, conn("notes", "notes_search")])
    put(world, "tts", [conn("actors", "actor_tts")])
    put(world, "draw", [conn("other", "paint_v2")])
    for conn_id, url in (("notes", "http://mcp_server:9003/mcp"), ("actors", "http://mcp_server:9003/mcp")):
        with pytest.raises(StateGraphError, match="^pinned_private_only"):
            add(world, conn_id=conn_id, visibility="public", url=url, label=conn_id.title())
        assert next(c for c in listed(world)["connections"] if c["conn_id"] == conn_id)["visibility"] == "private"
    # An ordinary pin does not hold its connection private.
    add(world, conn_id="other", visibility="public", url="https://good.example/mcp", label="Other")
    assert next(c for c in listed(world)["connections"] if c["conn_id"] == "other")["visibility"] == "public"
    # Unpinned, the connection may go public again.
    unset(world, "memory", 1)
    add(world, conn_id="notes", visibility="public", url="http://mcp_server:9003/mcp", label="Notes")


# --- the owner's HTTP routes ------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    email = OWNER.split(":", 1)[1]
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {email})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as http:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            http.owner = {"X-Test-Email": email}
            yield http
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def test_the_routes_are_the_owners_and_refuse_everyone_else(world, client):
    """c3 / c8: anonymous, a stranger, the director's and the steward's credentials are
    refused (403, never 404) before anything is read or written."""
    ready(world)
    body = {"request_key": "h1", "expect_version": 0, "route": [conn("media", "paint_v2")]}
    others = ({}, {"X-Test-Email": "stranger@example.com"},
              {"X-AItelier-Driver-Token": world.tokens["director"]},
              {"X-AItelier-Driver-Token": world.tokens["steward"]})
    for headers in others:
        for method, path, payload in (("PUT", "/api/steward/capabilities/ocr", body),
                                      ("PUT", "/api/steward/capabilities/draw", body),
                                      ("DELETE", "/api/steward/capabilities/draw?expect_version=0", None),
                                      ("DELETE", "/api/steward/capabilities", None),
                                      ("GET", "/api/steward/capabilities", None)):
            answer = client.request(method, path, json=payload, headers=headers)
            assert answer.status_code in (401, 403), (headers, method, path, answer.status_code, answer.text)
    assert stored(world) == {}
    answer = client.put("/api/steward/capabilities/draw", json=body, headers=client.owner)
    assert answer.status_code == 200, answer.text
    assert answer.json()["setting"]["version"] == 1


def test_the_routes_answer_codes_and_keep_the_row(world, client):
    ready(world, names=())
    def put_(route, expect=0):
        return client.put("/api/steward/capabilities/draw", headers=client.owner,
                          json={"request_key": key("h"), "expect_version": expect, "route": route})
    assert put_([conn("media", "paint_v2")]).status_code == 200
    conflict = put_([conn("media", "generate_image")])
    assert conflict.status_code == 409 and "version_conflict" in conflict.text
    unknown = put_([conn("media", "paint_v3")], expect=1)
    assert unknown.status_code == 422 and unknown.json()["detail"].startswith("unknown_tool")
    gone = put_([conn("nosuch", "paint_v2")], expect=1)
    assert gone.status_code == 422 and gone.json()["detail"].startswith("unknown_connection")
    assert stored(world)["draw"][0][0]["raw_name"] == "paint_v2" and stored(world)["draw"][1] == 1
    row = next(r for r in client.get("/api/steward/capabilities", headers=client.owner).json()["capabilities"]
               if r["id"] == "draw")
    assert row["setting"]["version"] == 1 and row["spec"] == {"allows": ["conn", "missing"], "host_tools": [],
                                                              "private_only": False}
    assert client.delete("/api/steward/capabilities/draw?expect_version=2", headers=client.owner).status_code == 409
    assert client.delete("/api/steward/capabilities/draw?expect_version=1", headers=client.owner).json() == \
        {"cap_id": "draw", "setting": None}
    put_([{"kind": "missing"}])
    assert client.delete("/api/steward/capabilities", headers=client.owner).json() == {"cleared": 1}
    assert stored(world) == {}
