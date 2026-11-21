"""Step 24.1a: the steward's own MCP connections and the backend's aggregating gateway
(design-steward 7.8.4, 7.8.5, 7.8.10a, tests 7.8.18).

The registry (core/state_steward_connections.py) is exercised through the State
commands; the gateway (api/steward_gateway.py) through a REAL uvicorn server on
loopback - the GET notification stream is an endless event stream, which only a real
server streams - with a fake upstream MCP server behind an ASGI transport."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api import authz
from api import steward_gateway as gw
from api.dependencies import get_db_manager
from api.main import app
from core import state_steward_connections as sc
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, StateNotFound
from core.state_service import StateService

HOME = "aitelier-home"
OWNER = "owner:o@example.com"
PEPPER = "steward-connections-pepper-" * 3
# What the fake resolver answers: the connection hosts on the docker network, and the
# reserved services on theirs.
ADDRESSES = {"mcp_server": "172.30.0.5", "postkeep": "172.30.0.6", "steward-pwmcp": "172.40.0.2",
             "aitelier": "172.30.0.2", "dev_mcp": "172.30.0.9", "zvec-grep": "172.24.0.4",
             "aitelier-zg": "172.24.0.4", "alias-of-backend": "172.30.0.2", "localhost": "127.0.0.1",
             "lanhost": "192.168.1.119", "example.com": "93.184.216.34", "good.example": "93.184.216.35"}


def resolver(host, port):
    try:
        address = ADDRESSES.get(host) or str(__import__("ipaddress").ip_address(host.strip("[]")))
    except ValueError:
        raise socket.gaierror(host) from None
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]


TOOLS = [
    {"name": "web_search", "description": "Search the web", "inputSchema": {"type": "object", "properties": {
        "query": {"type": "string"}}, "required": ["query"]}, "annotations": {"readOnlyHint": True}},
    {"name": "ocr_image", "description": "Read text in an image", "inputSchema": {"type": "object", "properties": {
        "image_file_id": {"type": "string"}, "image_url": {"type": "string"}}},
     "outputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
    {"name": "send_message", "description": "Send a mail", "inputSchema": {"type": "object", "properties": {
        "to": {"type": "string"}, "body": {"type": "string"}}}},
]


class FakeUpstream:
    """A small streamable-HTTP MCP server: counts initialize, records requests (never
    used for anything but assertions), can expire its sessions and answer calls as SSE."""

    def __init__(self, tools=None):
        self.tools = json.loads(json.dumps(tools or TOOLS))
        self.initialized = 0
        self.methods: list[str] = []
        self.headers: list[dict] = []
        self.sessions: set[str] = set()
        self.sse = False
        self.expire_next_call = False
        self.app = Starlette(routes=[Route("/mcp", self.post, methods=["POST"]),
                                     Route("/mcp", self.other, methods=["GET", "DELETE"])])

    async def other(self, request):
        return Response(status_code=405 if request.method == "GET" else 200)

    async def post(self, request: Request):
        body = json.loads(await request.body())
        self.headers.append(dict(request.headers))
        method, mid = body.get("method"), body.get("id")
        self.methods.append(method)
        if method == "initialize":
            self.initialized += 1
            session = f"s{self.initialized}"
            self.sessions.add(session)
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake", "version": "1"}, "instructions": "fake instructions"}},
                headers={"mcp-session-id": session})
        if request.headers.get("mcp-session-id") not in self.sessions:
            return Response(status_code=404)
        if mid is None:
            return Response(status_code=202)
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"tools": self.tools}})
        if method == "tools/call":
            if self.expire_next_call:
                self.expire_next_call = False
                self.sessions.clear()
                return Response(status_code=404)
            result = {"content": [{"type": "text", "text": json.dumps({"echo": body["params"]})}]}
            answer = {"jsonrpc": "2.0", "id": mid, "result": result}
            if self.sse:
                return Response("event: message\ndata: " + json.dumps(answer) + "\n\n", media_type="text/event-stream")
            return JSONResponse(answer)
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "RESOLVER", resolver)
    monkeypatch.setenv("AITELIER_STEWARD_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("AITELIER_STEWARD_HOST_ADDRESSES", "192.168.1.119,100.106.5.46,172.24.0.1")
    monkeypatch.delenv("AITELIER_STEWARD_GATEWAY_SOURCES", raising=False)
    monkeypatch.setattr(sc, "TOOLS_CHANGED_HOOKS", [])
    monkeypatch.setattr(sc, "PROBER", None)
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("steward-connections-owner-cli")
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

    owner = StateService(db, actor=OWNER, project_read_trusted=True)
    return type("World", (), dict(db=db, registry=registry, runtime=runtime, tokens=tokens, owner=owner,
                                  steward_row=steward, steward=as_driver(steward), director=as_driver(director),
                                  tmp=tmp_path))


def add(world, conn_id="media", url="http://mcp_server:9003/mcp", visibility="public", enabled=True, **kw):
    return execute(world.owner, "steward_connection_upsert", {"conn_id": conn_id, "label": kw.pop("label", conn_id.title()),
                   "url": url, "visibility": visibility, "enabled": enabled, **kw}, allow_write=True)


def discover(world, conn_id="media", tools=None, instructions=None):
    return sc.record_discovery(world.owner.store, conn_id, tools if tools is not None else TOOLS, instructions)


def allow(world, conn_id="media", **flags):
    names = flags.pop("names", None) or [t["name"] for t in TOOLS]
    return execute(world.owner, "steward_connection_tools_set", {"conn_id": conn_id, "tools": [
        {"raw_name": n, "allowed": True, **flags} for n in names]}, allow_write=True)


def listed(world, service=None):
    return execute(service or world.owner, "list_steward_connections", {})


# --- registration ---------------------------------------------------------------------------

@pytest.mark.parametrize("conn_id", ["mail-pub", "mail_pub", "Mailpub", "m", "a" * 21, "1mail", ""])
def test_conn_id_rule(world, conn_id):
    with pytest.raises(StateGraphError, match="invalid_conn_id|String should have"):
        add(world, conn_id=conn_id)


def test_conn_id_accepts_the_rule(world):
    for conn_id in ("ab", "mailpub2", "a" * 20):
        assert add(world, conn_id=conn_id, label="L" + conn_id)["conn_id"] == conn_id


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/mcp", "https://0.0.0.0:9/mcp", "https://[::1]:7676/mcp", "http://aitelier:4444/mcp",
    "http://dev_mcp:7676/mcp", "http://zvec-grep:7998/mcp", "http://aitelier-zg:7998/mcp",
    "https://172.30.0.2/mcp", "https://172.30.0.9:7676/", "http://alias-of-backend:4444/mcp",
    "http://localhost:8931/mcp", "https://192.168.1.119:8931/mcp", "https://100.106.5.46:7999/mcp",
    "https://172.24.0.1:1/mcp", "https://lanhost:8999/mcp"])
def test_reserved_endpoints_are_refused_by_the_resolved_address(world, url):
    with pytest.raises(StateGraphError, match="reserved_endpoint|invalid_url"):
        add(world, url=url)


def test_an_ip_written_with_http_is_not_a_container_name(world):
    with pytest.raises(StateGraphError, match="invalid_url"):
        add(world, url="http://172.30.0.2:4444/mcp")
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        add(world, url="https://172.30.0.2:4444/mcp")


@pytest.mark.parametrize("url", ["https://u:p@good.example/mcp", "https://good.example/mcp?x=1",
                                 "https://good.example/mcp#f", "ftp://good.example/", "https://exämple.com/mcp",
                                 "http://good.example/mcp"])
def test_url_shapes_are_refused(world, url):
    with pytest.raises(StateGraphError, match="invalid_url"):
        add(world, url=url)


def test_the_stewards_own_browser_is_not_reserved(world):
    assert add(world, conn_id="browser", url="http://steward-pwmcp:8921/mcp", visibility="private")["conn_id"] == "browser"


def test_discovery_stores_canonical_definitions_and_prefills(world):
    add(world)
    discover(world)
    tools = {t["raw_name"]: t for t in execute(world.owner, "list_steward_connection_tools", {"conn_id": "media"})["tools"]}
    with world.db.get_connection() as conn:
        rows = {r["raw_name"]: r for r in conn.execute("SELECT * FROM state_steward_connection_tools")}
    for name, row in rows.items():
        assert hashlib.sha256(row["definition_json"].encode()).hexdigest() == row["schema_sha256"]
        assert "outputSchema" not in json.loads(row["definition_json"])
        assert row["egress"] == 1 and row["allowed"] == 0, name
    # Outward is prefilled unless readOnlyHint is true; allowed never is.
    assert {n: t["outward"] for n, t in tools.items()} == {"web_search": False, "ocr_image": False, "send_message": True}
    # A public connection's url parameters are blocked by default.
    assert tools["ocr_image"]["blocked_args"] == ["image_url"] and tools["web_search"]["blocked_args"] == []
    assert {t["state"] for t in tools.values()} == {"new"}


def test_a_private_connection_blocks_no_url_by_default(world):
    add(world, conn_id="webfetch", visibility="private")
    discover(world, "webfetch", [{"name": "web_fetch", "inputSchema": {"type": "object", "properties": {"url": {}}}}])
    assert execute(world.owner, "list_steward_connection_tools", {"conn_id": "webfetch"})["tools"][0]["blocked_args"] == []


def test_an_annotation_change_is_a_changed_description(world):
    add(world)
    discover(world)
    allow(world)
    assert "mcp__sw__media__web_search" in listed(world)["effective_tools"]
    changed = json.loads(json.dumps(TOOLS))
    changed[0]["annotations"] = {"readOnlyHint": False}
    discover(world, tools=changed)
    assert "mcp__sw__media__web_search" not in listed(world)["effective_tools"]
    states = {t["raw_name"]: t["state"] for t in execute(world.owner, "list_steward_connection_tools",
                                                         {"conn_id": "media"})["tools"]}
    assert states["web_search"] == "changed" and states["send_message"] == "allowed"


def test_a_new_address_marks_every_tool_egress_again(world):
    add(world)
    discover(world)
    execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
        {"raw_name": "ocr_image", "egress": False}]}, allow_write=True)
    before = listed(world)["connections"][0]["config_version"]
    add(world, label="Media")  # same address: nothing refilled
    assert {t["raw_name"]: t["egress"] for t in execute(world.owner, "list_steward_connection_tools",
                                                       {"conn_id": "media"})["tools"]}["ocr_image"] is False
    add(world, url="https://good.example/mcp", label="Media")
    after = listed(world)["connections"][0]
    assert all(t["egress"] for t in execute(world.owner, "list_steward_connection_tools", {"conn_id": "media"})["tools"])
    assert after["config_version"] > before and after["generation"] == 2


def test_oauth_arrives_with_24_1b(world):
    with pytest.raises(StateGraphError, match="auth_kind_unavailable"):
        add(world, auth_kind="oauth")


@pytest.mark.parametrize("header", ["Host", "Cookie", "X-AItelier-Driver-Token", "Mcp-Session-Id", "Bad Header"])
def test_forbidden_header_names(world, header):
    with pytest.raises(StateGraphError, match="invalid_header_name"):
        add(world, auth_kind="header", header_name=header)


def test_labels_are_plain_and_unique(world):
    add(world, label="Family mail")
    for label in ("「x」", 'a"b', "a\nb", "a‮b", "a​b", "x" * 41, "Family mail"):
        with pytest.raises(StateGraphError):
            add(world, conn_id="other", label=label)


# --- secrets ----------------------------------------------------------------------------------

def test_a_header_secret_is_a_0600_file_and_never_in_state(world):
    add(world, conn_id="docs", url="https://good.example/mcp", auth_kind="header", visibility="private")
    assert listed(world)["connections"][0]["secret"] == "unset"
    secret = "Bearer canary-secret-value-123"
    execute(world.owner, "steward_connection_secret_set", {"conn_id": "docs", "value": secret}, allow_write=True)
    path = world.tmp / "secrets" / "docs.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    answer = listed(world)
    assert answer["connections"][0]["secret"] == "set" and secret not in json.dumps(answer)
    for file in world.tmp.glob("state.sqlite*"):
        assert secret.encode() not in file.read_bytes()
    execute(world.owner, "steward_connection_delete", {"conn_id": "docs"}, allow_write=True)
    assert not path.exists()


def test_startup_removes_secret_files_whose_connection_is_gone(world):
    add(world, conn_id="docs", url="https://good.example/mcp", auth_kind="header", visibility="private")
    execute(world.owner, "steward_connection_secret_set", {"conn_id": "docs", "value": "v1"}, allow_write=True)
    sc.write_secret("gone", {"header_value": "left behind"})
    with world.db.get_connection() as conn:
        assert sc.prune_orphan_secrets(conn) == ["gone"]
    assert sorted(p.name for p in (world.tmp / "secrets").iterdir()) == ["docs.json"]


# --- who may write ----------------------------------------------------------------------------

OWNER_WRITES = {
    "steward_connection_upsert": {"conn_id": "media", "label": "M", "url": "http://mcp_server:9003/mcp"},
    "steward_connection_delete": {"conn_id": "media"},
    "steward_connection_tools_set": {"conn_id": "media", "tools": [{"raw_name": "x", "allowed": True}]},
    "steward_connection_secret_set": {"conn_id": "media", "value": "v"},
    "steward_connection_probe": {"conn_id": "media"},
}


@pytest.mark.parametrize("action", sorted(OWNER_WRITES))
def test_the_connection_writes_are_the_owner_sessions_alone(world, action):
    with pytest.raises(StateGraphError, match="owner_session_required"):
        execute(world.director, action, OWNER_WRITES[action], allow_write=True)
    with pytest.raises(StateGraphError, match=f"steward_read_only: {action}"):
        execute(world.steward, action, OWNER_WRITES[action], allow_write=True)


def test_the_steward_reads_its_connections_and_writes_only_its_three(world):
    add(world)
    discover(world)
    allow(world, names=["web_search"])
    answer = listed(world, world.steward)
    assert answer["effective_tools"] == ["mcp__sw__media__web_search"]
    assert answer["connections"][0]["tools"][0] == {"name": "mcp__sw__media__web_search", "raw_name": "web_search",
                                                    "gw_name": "web_search", "egress": True, "outward": False,
                                                    "blocked_args": [], "daily_cap": None}
    with pytest.raises(StateGraphError, match="owner_or_steward_required"):
        listed(world, world.director)
    assert {"set_steward_turn_audience", "steward_tools_resync_due", "steward_connection_refresh"} <= STEWARD_WRITE_ACTIONS
    # The turn audience the gateway reads is step 23's one record (core.state_groups), the
    # same command the group turns write: owner or group, nothing else.
    assert execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"},
                   allow_write=True) == {"recorded": True, "turn": "t1", "audience": "owner"}
    for bad in ("everyone", "group:wechat:room1", "owner "):
        with pytest.raises(StateGraphError):
            execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": bad}, allow_write=True)
    with pytest.raises(StateGraphError, match="steward_role_required"):
        execute(world.director, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"}, allow_write=True)


def test_an_anonymous_reader_is_refused_both_reads(world):
    anonymous = StateService(world.db, actor="anon", project_read_trusted=False)
    for action, args in (("list_steward_connections", {}), ("list_steward_connection_tools", {"conn_id": "media"})):
        with pytest.raises(Exception):
            execute(anonymous, action, args)


def test_refresh_writes_discovery_fields_only_and_once_a_minute(world, monkeypatch):
    add(world)
    discover(world)
    allow(world, names=["web_search"])
    execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
        {"raw_name": "ocr_image", "egress": False}]}, allow_write=True)
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_connections SET last_probe_at=NULL")
        conn.commit()
    keep = "allowed,outward,egress,blocked_args,allowed_sha256,daily_cap"
    with world.db.get_connection() as conn:
        before = [tuple(r) for r in conn.execute(f"SELECT raw_name,{keep} FROM state_steward_connection_tools ORDER BY raw_name")]
        cbefore = tuple(conn.execute("SELECT visibility,enabled,config_version FROM state_steward_connections").fetchone())
    grown = TOOLS + [{"name": "generate_image", "inputSchema": {"type": "object", "properties": {"prompt": {}}}}]
    monkeypatch.setattr(sc, "PROBER", lambda row: {"tools": grown, "instructions": "x"})
    answer = execute(world.steward, "steward_connection_refresh", {"conn_id": "media"}, allow_write=True)
    assert {t["raw_name"] for t in answer["tools"]} == {"web_search", "ocr_image", "send_message", "generate_image"}
    with world.db.get_connection() as conn:
        after = [tuple(r) for r in conn.execute(f"SELECT raw_name,{keep} FROM state_steward_connection_tools "
                                                "WHERE raw_name<>'generate_image' ORDER BY raw_name")]
        cafter = tuple(conn.execute("SELECT visibility,enabled,config_version FROM state_steward_connections").fetchone())
    assert after == before and cafter == cbefore
    new = next(t for t in answer["tools"] if t["raw_name"] == "generate_image")
    assert (new["allowed"], new["egress"], new["outward"]) == (False, True, True)
    with pytest.raises(StateGraphError, match="refresh_too_soon"):
        execute(world.steward, "steward_connection_refresh", {"conn_id": "media"}, allow_write=True)


# --- names --------------------------------------------------------------------------------------

def test_gw_names():
    assert sc.gw_name("media", "web_search") == "web_search"
    fits = "x" * (64 - len("mcp__sw__media__"))
    assert sc.gw_name("media", fits) == fits
    for raw in ("files.list", "a/b", "读取邮件", fits + "y", "bad name!" * 10):
        name = sc.gw_name("media", raw)
        public = "mcp__sw__media__" + name
        assert len(public) <= 64 and sc.CLEAN_NAME.match(public), raw
        assert name.endswith("_" + hashlib.sha256(raw.encode()).hexdigest()[:8])
        assert sc.gw_name("media", raw) == name
    assert sc.split_name("media__a__b") == ("media", "a__b")
    assert sc.split_name("Media__x") is None and sc.split_name("nounderscore") is None


def test_gateway_names_are_never_renamed_by_the_installed_dsh_mcp_client(tmp_path):
    """Cross-check with dsh-mcp-client's own publicToolName (not exported: evaluated from
    the installed library's source), for clean, 64-char, shortened and odd names."""
    lib = Path("/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js")
    node = shutil.which("node")
    if not lib.exists() or node is None:
        pytest.skip("needs the installed dsh-mcp-client and node")
    conn = "abcdefghijklmnopqrst"
    raws = ["web_search", "x" * (64 - len("mcp__sw__" + conn + "__")), "x" * 70, "files.list", "读取", "a-b", "a b/c"]
    names = [f"{conn}__{sc.gw_name(conn, raw)}" for raw in raws] + [f"media__{sc.gw_name('media', r)}" for r in raws]
    script = tmp_path / "check.mjs"
    script.write_text(f"""
import {{ readFileSync }} from 'node:fs';
import {{ createHash }} from 'node:crypto';
const src = readFileSync({json.dumps(str(lib))}, 'utf8');
const start = src.indexOf('const MAX_PUBLIC_NAME_LENGTH');
const fn = src.indexOf('function publicToolName');
const end = src.indexOf('\\n}}', fn) + 2;
const body = src.slice(start, src.indexOf('const IMAGE_MEDIA_TYPES')) + src.slice(fn, end) + '; return publicToolName;';
const publicToolName = new Function('createHash', body)(createHash);
const names = {json.dumps(names)};
const bad = names.filter(n => publicToolName('sw', n) !== 'mcp__sw__' + n);
console.log(JSON.stringify(bad));
""")
    out = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_a_name_collision_keeps_the_first_seen_tool(world):
    add(world)
    long_a = "q" * 60 + "a"
    long_b = "q" * 60 + "b"
    discover(world, tools=[{"name": long_a}, {"name": "ok"}])
    with world.db.get_connection() as conn:  # force the same gateway name for the later one
        conn.execute("INSERT INTO state_steward_connection_tools(conn_id,raw_name,gw_name,definition_json,schema_sha256,"
                     "allowed,allowed_sha256,outward,egress,blocked_args,first_seen_at,last_seen_at) "
                     "SELECT conn_id,?,gw_name,definition_json,schema_sha256,0,NULL,outward,egress,blocked_args,"
                     "'2099-01-01',last_seen_at FROM state_steward_connection_tools WHERE raw_name=?", (long_b, long_a))
        conn.commit()
    allow(world, names=[long_a, long_b, "ok"])
    for _ in range(3):
        discover(world, tools=[{"name": long_a}, {"name": "ok"}])
        effective = listed(world)["effective_tools"]
        assert len(effective) == len(set(effective)) == 2
        states = {t["raw_name"]: t["state"] for t in execute(world.owner, "list_steward_connection_tools",
                                                             {"conn_id": "media"})["tools"]}
        assert states[long_b] == "collision" and states[long_a] == "allowed"


def test_effective_set_and_hash(world):
    add(world)
    discover(world)
    allow(world)
    one = listed(world)
    assert one["effective_tools"] == sorted(one["effective_tools"])
    # Visibility is not in the set: changing it changes nothing here.
    add(world, visibility="private", label="Media")
    assert listed(world)["effective_hash"] == one["effective_hash"]
    add(world, enabled=False, label="Media")
    assert listed(world)["effective_tools"] == []
    add(world, enabled=True, label="Media")
    assert listed(world)["effective_hash"] == one["effective_hash"]


# --- the gateway, over a real server -------------------------------------------------------------

class Server:
    def __init__(self):
        self.server = None

    def start(self):
        import uvicorn
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def call(self, coroutine_fn, *args):
        return asyncio.run_coroutine_threadsafe(coroutine_fn(*args), self.loop).result(30)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def server(world, upstream, monkeypatch):
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(gw, "transport_for", lambda address: httpx.ASGITransport(app=upstream.app))
    monkeypatch.setattr(gw, "COALESCE_SECONDS", 0.3)
    monkeypatch.setattr(gw, "KEEPALIVE_SECONDS", 0.5)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    old_mode = getattr(app.state, "_test_mode", False)
    app.state._test_mode = True
    app.state.dsh_drivers = world.runtime
    for name in ("steward_gateway", "_steward_gateway_schema"):
        if hasattr(app.state, name):
            delattr(app.state, name)
    running = Server()
    running.start()
    running.call(_start_gateway)
    try:
        yield running
    finally:
        found = getattr(app.state, "steward_gateway", None)
        if found is not None:
            running.call(found.stop)
            delattr(app.state, "steward_gateway")
        running.stop()
        app.dependency_overrides.pop(get_db_manager, None)
        app.state._test_mode = old_mode
        if hasattr(app.state, "dsh_drivers"):
            delattr(app.state, "dsh_drivers")


async def _start_gateway():
    gw.gateway(app)


def headers(world, who="steward", session=None, **extra):
    out = {"X-AItelier-Driver-Token": world.tokens[who], "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    if session:
        out["Mcp-Session-Id"] = session
    out.update(extra)
    return out


def rpc(server, world, method, params=None, mid=1, session="sess-a", who="steward", raw=None, **extra):
    body = raw if raw is not None else {"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params is not None else {})}
    response = httpx.post(server.url + gw.PATH, json=body, headers=headers(world, who, session, **extra), timeout=30)
    return response


def answers_of(response):
    """Every answer (a message with an id), JSON or event stream."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    else:
        data = response.json()
        messages = data if isinstance(data, list) else [data]
    return [m for m in messages if "id" in m]


def body_of(response):
    """A JSON answer, or the last message of an event-stream answer (with the notices before it)."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return messages[-1], messages[:-1]
    return response.json(), []


def ready(world, conn_id="media", visibility="public", names=None, **kw):
    add(world, conn_id=conn_id, visibility=visibility, **kw)
    discover(world, conn_id)
    allow(world, conn_id, names=names or ["web_search", "ocr_image"])
    execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"}, allow_write=True)


def test_only_a_steward_token_enters(world, server, monkeypatch):
    for method in ("POST", "GET", "DELETE"):
        for extra in ({"X-AItelier-Driver-Token": world.tokens["director"]}, {}):
            r = httpx.request(method, server.url + gw.PATH, headers={**extra, "Mcp-Session-Id": "x"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "ping"} if method == "POST" else None)
            assert r.status_code == 403, (method, extra, r.text)
        via_cf = httpx.request(method, server.url + gw.PATH, headers=headers(world, session="x", **{"Cf-Ray": "abc"}),
                               json={"jsonrpc": "2.0", "id": 1, "method": "ping"} if method == "POST" else None)
        assert via_cf.status_code == 403
    # A source address that is not the DSH host's (a container on cloudflare_edge).
    monkeypatch.setenv("AITELIER_STEWARD_GATEWAY_SOURCES", "172.24.0.1")
    assert rpc(server, world, "ping").status_code == 403
    monkeypatch.setenv("AITELIER_STEWARD_GATEWAY_SOURCES", "127.0.0.1")
    assert body_of(rpc(server, world, "ping"))[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    # The feature off (no registry): nobody, whatever the gate says.
    monkeypatch.setattr(authz, "driver_registry", lambda: None)
    assert rpc(server, world, "ping").status_code == 403


def test_initialize_and_the_methods_it_does_not_serve(world, server, upstream):
    r = rpc(server, world, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            session=None)
    result = r.json()["result"]
    assert result["capabilities"] == {"tools": {"listChanged": True}} and "instructions" not in result
    assert result["protocolVersion"] == "2025-06-18" and r.headers["mcp-session-id"]
    for method in ("server/discover", "resources/list", "prompts/list", "completion/complete", "logging/setLevel"):
        r = rpc(server, world, method, {})
        assert r.status_code == 200 and body_of(r)[0]["error"]["code"] == -32601, method
    r = httpx.post(server.url + gw.PATH, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                   headers=headers(world, session="sess-a"))
    assert r.status_code == 202 and r.content == b""
    assert upstream.methods == []


def test_tools_list_serves_the_allowed_definitions_and_never_upstream(world, server, upstream):
    ready(world)
    tools = rpc(server, world, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["media__ocr_image", "media__web_search"]
    assert all("outputSchema" not in t for t in tools)
    with world.db.get_connection() as conn:
        stored = {json.loads(r[0])["name"]: json.loads(r[0]) for r in conn.execute(
            "SELECT definition_json FROM state_steward_connection_tools")}
    assert tools[1]["description"] == stored["web_search"]["description"]
    assert tools[1]["inputSchema"] == stored["web_search"]["inputSchema"]
    assert upstream.methods == []
    # The upstream changes a description; until it is read again the steward sees the old one.
    upstream.tools[0]["description"] = "Search the web!"
    assert rpc(server, world, "tools/list").json()["result"]["tools"][1]["description"] == "Search the web"
    # Read again: the changed tool leaves the list and a call is refused.
    discover(world, tools=upstream.tools)
    assert [t["name"] for t in rpc(server, world, "tools/list").json()["result"]["tools"]] == ["media__ocr_image"]
    refused = rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}}).json()
    assert refused["error"]["data"]["code"] == "tool_not_allowed"


def test_tools_call_judgement_in_order(world, server, upstream):
    ready(world)
    def call(name, arguments, **kw):
        return body_of(rpc(server, world, "tools/call", {"name": name, "arguments": arguments}, **kw))[0]
    ok = call("media__web_search", {"query": "weather"})
    assert json.loads(ok["result"]["content"][0]["text"])["echo"] == {"name": "web_search", "arguments": {"query": "weather"}}
    # A tool that declares an outputSchema and answers with text only is passed on (the
    # steward never sees an outputSchema; the gateway does not judge one either).
    assert "result" in call("media__ocr_image", {"image_file_id": "f"})
    upstream.methods.clear()
    assert call("media__send_message", {"to": "a"})["error"]["data"]["code"] == "tool_not_allowed"
    assert call("media__nope", {})["error"]["data"]["code"] == "tool_not_allowed"
    assert call("other__web_search", {})["error"]["data"]["code"] == "tool_not_allowed"
    assert call("media__ocr_image", {"image_url": "http://mcp_server:9003/v1/x"})["error"]["data"]["code"] == "argument_blocked"
    allow(world, names=["send_message"])
    assert call("media__send_message", {"to": "a"})["error"]["data"]["code"] == "confirmation_required"
    assert "tools/call" not in upstream.methods
    # A private connection outside an owner turn (or with no audience recorded).
    add(world, visibility="private", label="Media")
    execute(world.steward, "set_steward_turn_audience",
            {"turn": "t2", "audience": "group", "group": {"channel": "wechat", "group_ref": "room1"}}, allow_write=True)
    assert call("media__web_search", {"query": "x"})["error"]["data"]["code"] == "not_visible_in_this_turn"
    with world.db.get_connection() as conn:
        conn.execute("DELETE FROM state_steward_turn_audience")
        conn.commit()
    assert call("media__web_search", {"query": "x"})["error"]["data"]["code"] == "not_visible_in_this_turn"
    assert "tools/call" not in upstream.methods
    # Disabled, or a header connection without its secret: unavailable, out of the list.
    add(world, enabled=False, label="Media")
    assert call("media__web_search", {"query": "x"})["error"]["data"]["code"] == "connection_unavailable"
    add(world, conn_id="docs", url="https://good.example/mcp", auth_kind="header", visibility="public")
    discover(world, "docs")
    allow(world, "docs", names=["web_search"])
    assert rpc(server, world, "tools/list").json()["result"]["tools"] == []
    assert call("docs__web_search", {"query": "x"})["error"]["data"]["code"] == "connection_unavailable"
    assert "tools/call" not in upstream.methods


def test_the_daily_cap_counts_the_owners_day(world, server, monkeypatch):
    ready(world)
    execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
        {"raw_name": "web_search", "daily_cap": 2}]}, allow_write=True)
    call = lambda: body_of(rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}}))[0]
    assert "result" in call() and "result" in call()
    assert call()["error"]["data"]["code"] == "daily_cap_reached"
    # The day is the owner's time zone: 23:30 UTC on one day is the next day in Paris.
    from datetime import datetime, timezone
    monkeypatch.setenv("AITELIER_OWNER_TZ", "Europe/Paris")
    late = datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)
    assert sc._local_day(late) == "2026-11-22"
    with world.db.get_connection() as conn:
        assert sc.calls_today(conn, "media", "web_search", late) == 0


def test_a_batch_refuses_only_its_bad_call(world, server):
    ready(world)
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "media__web_search", "arguments": {"query": "a"}}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "media__send_message", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
    by_id = {a["id"]: a for a in answers_of(rpc(server, world, None, raw=batch))}
    assert "result" in by_id[1] and by_id[2]["error"]["data"]["code"] == "tool_not_allowed" and by_id[3]["result"] == {}


def test_one_upstream_session_reopened_only_on_change(world, server, upstream):
    ready(world)
    for _ in range(3):
        rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}})
    execute(world.owner, "steward_connection_probe", {"conn_id": "media"}, allow_write=True)
    assert upstream.initialized == 1
    add(world, url="https://good.example/mcp", label="Media")  # a new address: generation 2
    allow(world, names=["web_search"])
    rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}})
    rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "y"}})
    assert upstream.initialized == 2


def test_an_sse_answer_is_collected_and_an_expired_session_retried_once(world, server, upstream):
    ready(world)
    upstream.sse = True
    first = body_of(rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}}))[0]
    assert "result" in first
    upstream.expire_next_call = True
    again = body_of(rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "y"}}))[0]
    assert "result" in again and upstream.initialized == 2


def test_the_header_name_and_value_go_upstream_and_the_token_never(world, server, upstream):
    add(world, conn_id="docs", url="https://good.example/mcp", auth_kind="header", header_name="X-Api-Key",
        visibility="public")
    execute(world.owner, "steward_connection_secret_set", {"conn_id": "docs", "value": "k-123"}, allow_write=True)
    discover(world, "docs")
    allow(world, "docs", names=["web_search"])
    rpc(server, world, "tools/call", {"name": "docs__web_search", "arguments": {"query": "x"}})
    assert upstream.headers and all(h.get("x-api-key") == "k-123" for h in upstream.headers)
    assert all(world.tokens["steward"] not in json.dumps(h) for h in upstream.headers)
    assert all("x-aitelier-driver-token" not in h for h in upstream.headers)


def test_the_body_limit(world, server, monkeypatch):
    monkeypatch.setattr(gw, "MAX_BODY", 1000)
    r = rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x" * 2000}})
    assert r.status_code == 413


def test_an_unknown_session_is_answered_not_404(world, server):
    ready(world)
    r = rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}}, session="never-seen")
    assert r.status_code == 200
    answer, notices = body_of(r)
    assert "result" in answer and notices == [gw.LIST_CHANGED]  # a session that missed changes is told first
    assert httpx.delete(server.url + gw.PATH, headers=headers(world, session="never-seen")).status_code == 200


def test_logs_and_answers_carry_no_token_arguments_or_results(world, server, caplog):
    ready(world)
    caplog.set_level(logging.DEBUG)
    canary = "canary-argument-7f3a"
    rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": canary}})
    rpc(server, world, "tools/call", {"name": "media__send_message", "arguments": {"to": canary}})
    text = caplog.text
    assert canary not in text and world.tokens["steward"] not in text
    with world.db.get_connection() as conn:
        ledger = [dict(r) for r in conn.execute("SELECT * FROM state_steward_tool_calls")]
    assert ledger and canary not in json.dumps(ledger)
    refused = rpc(server, world, "tools/call", {"name": "media__send_message", "arguments": {"to": canary}}).text
    assert canary not in refused


# --- notifications ------------------------------------------------------------------------------

class Listener:
    """One GET stream read in a thread: every line, as it arrives."""

    def __init__(self, server, world, session):
        self.lines: list[str] = []
        self.status = None
        self.closed = threading.Event()
        self.client = httpx.Client(timeout=httpx.Timeout(5, read=None))
        self.thread = threading.Thread(target=self._run, args=(server, world, session), daemon=True)
        self.thread.start()

    def _run(self, server, world, session):
        try:
            with self.client.stream("GET", server.url + gw.PATH, headers=headers(world, session=session)) as r:
                self.status = r.status_code
                for line in r.iter_lines():
                    self.lines.append(line)
        except Exception:  # noqa: BLE001 - closed by the test
            pass
        finally:
            self.closed.set()

    def notices(self):
        return [line for line in self.lines if line.startswith("data: ")]

    def wait(self, predicate, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate(self):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self.client.close()


def test_the_stream_starts_with_retry_and_announces_each_change_once(world, server):
    ready(world)
    rpc(server, world, "tools/list", session="sess-a")
    listener = Listener(server, world, "sess-a")
    try:
        assert listener.wait(lambda l: l.lines[:1] == ["retry: 30000"])
        time.sleep(0.3)
        assert listener.notices() == []  # its list is current: nothing on connect
        allow(world, names=["send_message"])
        assert listener.wait(lambda l: len(l.notices()) == 1)
        rpc(server, world, "tools/list", session="sess-a")  # what the client does on the notice
        # Several changes inside the coalescing second: one notice.
        execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
            {"raw_name": "send_message", "allowed": False}]}, allow_write=True)
        allow(world, names=["send_message"])
        execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
            {"raw_name": "send_message", "allowed": False}]}, allow_write=True)
        assert listener.wait(lambda l: len(l.notices()) == 2)
        time.sleep(0.4)
        assert len(listener.notices()) == 2
        rpc(server, world, "tools/list", session="sess-a")
        # Visibility alone is not a change of the list.
        add(world, visibility="private", label="Media")
        time.sleep(0.5)
        assert len(listener.notices()) == 2
        # Disabled then re-allowed with a new description within the second: still one notice.
        changed = json.loads(json.dumps(TOOLS))
        changed[0]["description"] = "Search the web, again"
        add(world, enabled=False, label="Media")
        discover(world, tools=changed)
        allow(world, names=["web_search"])
        add(world, enabled=True, label="Media")
        assert listener.wait(lambda l: len(l.notices()) == 3)
        assert json.loads(listener.notices()[0][6:]) == gw.LIST_CHANGED
        assert listener.wait(lambda l: ": keepalive" in l.lines)
    finally:
        listener.close()


def test_a_stale_or_unknown_session_is_told_on_connect(world, server):
    ready(world)
    listener = Listener(server, world, "never-listed")
    try:
        assert listener.wait(lambda l: len(l.notices()) == 1)
    finally:
        listener.close()


def test_streams_are_per_session_and_bounded_per_driver(world, server):
    ready(world)
    for s in ("a", "b", "c", "d"):
        rpc(server, world, "tools/list", session=s)
    first = Listener(server, world, "a")
    replacement = None
    others = []
    try:
        assert first.wait(lambda l: l.lines[:1] == ["retry: 30000"])
        replacement = Listener(server, world, "a")  # the same session replaces, never 409
        assert replacement.wait(lambda l: l.lines[:1] == ["retry: 30000"])
        assert first.closed.wait(5)
        others = [Listener(server, world, s) for s in ("b", "c", "d")]
        assert all(o.wait(lambda l: l.lines[:1] == ["retry: 30000"]) for o in others)
        fifth = httpx.get(server.url + gw.PATH, headers=headers(world, session="e"), timeout=5)
        assert fifth.status_code == 405
        allow(world, names=["send_message"])
        assert all(l.wait(lambda x: len(x.notices()) == 1) for l in [replacement, *others])
    finally:
        for l in [first, replacement, *others]:
            if l:
                l.close()


def test_a_dropped_client_is_deregistered(world, server):
    ready(world)
    listener = Listener(server, world, "gone")
    assert listener.wait(lambda l: l.lines[:1] == ["retry: 30000"])
    gateway = app.state.steward_gateway
    assert "gone" in gateway.streams
    listener.close()
    deadline = time.time() + 3
    while time.time() < deadline and "gone" in gateway.streams:
        time.sleep(0.05)
    assert "gone" not in gateway.streams


def test_an_upstream_list_changed_drops_a_changed_tool(world, server, upstream):
    ready(world)
    rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}})
    rpc(server, world, "tools/list", session="sess-a")
    listener = Listener(server, world, "sess-a")
    try:
        assert listener.wait(lambda l: l.lines[:1] == ["retry: 30000"])
        upstream.tools[0]["description"] = "changed upstream"
        gateway = app.state.steward_gateway
        server.call(gateway.relist, "media")  # what the upstream's notification starts
        assert listener.wait(lambda l: len(l.notices()) == 1)
        assert [t["name"] for t in rpc(server, world, "tools/list").json()["result"]["tools"]] == ["media__ocr_image"]
    finally:
        listener.close()


# --- the fallback marker ------------------------------------------------------------------------

def _drivers(world, running=True, phase="waiting", has_stream=False, enabled=True):
    return [{"driver_id": world.steward_row["driver_id"], "enabled": enabled, "running": running, "phase": phase,
             "has_stream": has_stream}]


def _markers(world):
    with world.db.get_connection() as conn:
        return {r["reason"]: dict(r) for r in conn.execute("SELECT * FROM state_steward_restart_due")}


def _check(world, **kw):
    with world.owner.store.transaction(write=True) as conn:
        return sc.check_stream_markers(conn, _drivers(world, **kw))


def test_no_stream_and_a_change_writes_a_marker_due_in_90_seconds(world):
    ready(world)
    me = world.steward_row["driver_id"]
    with world.owner.store.transaction(write=True) as conn:
        sc.record_served(conn, me, sc.current_hash(conn))
    assert _check(world) == []  # served the current list: nothing to do
    allow(world, names=["send_message"])
    assert _check(world, running=False) == [] and _check(world, phase="starting") == []
    assert _check(world, enabled=False) == [] and _check(world, has_stream=True) == []
    assert _check(world) == [me]
    due = _markers(world)[sc.STREAM_DOWN]
    delta = sc._parse(due["due_at"]) - sc._parse(due["created_at"])
    assert timedelta(seconds=89) < delta <= timedelta(seconds=91)
    assert _check(world) == []  # a marker that is there is never moved
    assert _markers(world)[sc.STREAM_DOWN]["due_at"] == due["due_at"]
    # A resync marker beside it; serving the current list drops only the stream one.
    execute(world.steward, "steward_tools_resync_due", {"effective_hash": "a" * 64}, allow_write=True)
    with world.owner.store.transaction(write=True) as conn:
        sc.record_served(conn, me, sc.current_hash(conn))
    assert set(_markers(world)) == {sc.RESYNC + "a" * 64}


def test_a_marker_goes_when_the_hash_is_back(world):
    ready(world)
    me = world.steward_row["driver_id"]
    with world.owner.store.transaction(write=True) as conn:
        sc.record_served(conn, me, sc.current_hash(conn))
    allow(world, names=["send_message"])
    _check(world)
    assert sc.STREAM_DOWN in _markers(world)
    execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
        {"raw_name": "send_message", "allowed": False}]}, allow_write=True)
    _check(world)
    assert _markers(world) == {}


def test_one_fallback_restart_per_hash_per_day_then_one_notice(world):
    ready(world)
    me = world.steward_row["driver_id"]
    with world.owner.store.transaction(write=True) as conn:
        digest = sc.current_hash(conn)
        conn.execute("INSERT INTO state_steward_restart_done(driver_id,reasons_json,restarted_at) VALUES(?,?,?)",
                     (me, json.dumps([{"reason": sc.STREAM_DOWN, "effective_hash": digest}]), sc._iso(sc._utcnow())))
    _check(world)
    assert _markers(world) == {}
    for _ in range(2):
        assert execute(world.steward, "steward_tools_resync_due", {"effective_hash": digest},
                       allow_write=True) == {"marker": "limited"}
    with world.db.get_connection() as conn:
        done = conn.execute("SELECT noticed_at FROM state_steward_restart_done").fetchone()
        notices = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages WHERE subject LIKE '%did not sync%'").fetchone()[0]
    assert done[0] is not None and notices == 1
    assert _markers(world) == {}


def test_a_failed_fallback_restart_is_told_once(world):
    me = world.steward_row["driver_id"]
    with world.owner.store.transaction(write=True) as conn:
        conn.execute("INSERT INTO state_steward_restart_done(driver_id,reasons_json,restarted_at,error) VALUES(?,?,?,?)",
                     (me, "[]", sc._iso(sc._utcnow()), "OperationalError"))
    _check(world)
    _check(world)
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_messages WHERE body LIKE '%OperationalError%'").fetchone()[0] == 1


def test_markers_survive_a_backend_restart_and_tools_list_needs_no_upstream(world, server, upstream):
    ready(world)
    allow(world, names=["send_message"])
    _check(world)
    assert sc.STREAM_DOWN in _markers(world)
    gateway = app.state.steward_gateway
    gateway.served.clear()  # what a restart leaves: no memory, the database intact
    assert sc.STREAM_DOWN in _markers(world)
    upstream.app = None  # every upstream gone: tools/list is still answered
    assert rpc(server, world, "tools/list").status_code == 200


# --- the real client: a newly allowed tool without a restart --------------------------------------

CLIENT_SCRIPT = r"""
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
const require = createRequire('/opt/aitelier-dsh/node_modules/.probe.js');
const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
const { default: SystemPrompt } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'));
const { default: Tools } = await import(require.resolve('@deepseek-ai/dsh-tools'));
const mcp = await import(require.resolve('@deepseek-ai/dsh-mcp-client'));
const [url, token] = process.argv.slice(2);
const ctx = new Context();
const prompt = ctx.plugin(SystemPrompt); await prompt.await();
const tools = ctx.plugin(Tools); await tools.await();
ctx.plugin(mcp, { serverName: 'sw', transport: 'streamable-http', url, headers: { 'X-AItelier-Driver-Token': token },
  failOnStartupError: false, toolCallTimeoutMs: 30000, reconnect: { maxAttempts: 1000000 } });
const names = () => ctx.tools.schemas().map(s => s.name).filter(n => n.startsWith('mcp__sw__')).sort();
const say = value => process.stdout.write(JSON.stringify(value) + '\n');
for await (const line of createInterface({ input: process.stdin })) {
  const [command, arg] = JSON.parse(line);
  if (command === 'wait') {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline && JSON.stringify(names()) !== JSON.stringify(arg)) await new Promise(r => setTimeout(r, 50));
    say({ names: names(), pid: process.pid });
  } else if (command === 'call') {
    const result = await ctx.tools.execute({ name: arg, arguments: { query: 'x', image_file_id: 'f' },
      signal: new AbortController().signal, callId: 'c' + Date.now() });
    say({ isError: result.isError, error: result.error?.message ?? null });
  }
}
"""


def test_a_real_dsh_mcp_client_finds_a_newly_allowed_tool_without_a_restart(world, server, tmp_path):
    """The installed dsh-mcp-client against this gateway: allowing a tool registers it in
    the same process (list_changed, then tools/list); taking it back removes it, and a
    call made in between is refused by the gateway."""
    node, lib = shutil.which("node"), Path("/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh-mcp-client")
    if node is None or not lib.exists():
        pytest.skip("needs node and the installed dsh-mcp-client")
    ready(world, names=["web_search"])
    script = tmp_path / "client.mjs"
    script.write_text(CLIENT_SCRIPT)
    process = subprocess.Popen([node, str(script), server.url + gw.PATH, world.tokens["steward"]], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def ask(command, arg):
        process.stdin.write(json.dumps([command, arg]) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    try:
        first = ask("wait", ["mcp__sw__media__web_search"])
        assert first["names"] == ["mcp__sw__media__web_search"]
        allow(world, names=["ocr_image"])
        second = ask("wait", ["mcp__sw__media__ocr_image", "mcp__sw__media__web_search"])
        assert second["names"] == ["mcp__sw__media__ocr_image", "mcp__sw__media__web_search"]
        assert second["pid"] == first["pid"], "the same process: no restart"
        called = ask("call", "mcp__sw__media__ocr_image")
        assert called["isError"] is False, called
        execute(world.owner, "steward_connection_tools_set", {"conn_id": "media", "tools": [
            {"raw_name": "web_search", "allowed": False}]}, allow_write=True)
        # Refused at once by the gateway, before the client re-lists.
        refused = body_of(rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "x"}}))[0]
        assert refused["error"]["data"]["code"] == "tool_not_allowed"
        assert ask("wait", ["mcp__sw__media__ocr_image"])["names"] == ["mcp__sw__media__ocr_image"]
    finally:
        process.kill()
        process.wait(10)
