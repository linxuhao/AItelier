"""Step 24.4c: the narrow dev-mcp opening (design-steward 7.8.24 "九" 5, tests "十四" 24.4c,
risk (y), decision 30).

dev-mcp is a shell on the host with the State database mounted read-write, so this is the
largest trust boundary the steward has. The refusals come first: a group turn never reaches
it; nobody but the owner's dashboard registers it, and only private and only under the
written name `dev_mcp` (an alias, a numeric address, a tunnel name, OAuth, a proposal: all
refused); a server whose own name does not fit its row is refused; its write tools never
run without a confirmation; its subagent_* tools are never allowed or served; and the
gateway forwards a call only for a tool the owner pinned on the basic capability `code`."""
from __future__ import annotations

import inspect
import json

import httpx
import pytest
from starlette.responses import JSONResponse

from api import steward_gateway as gw
from core import state_steward_capabilities as caps
from core import state_steward_connection_proposals as ccp
from core import state_steward_connections as sc
from core import steward_oauth
from core.state_commands import execute
from core.state_graph import StateGraphError
from tests.unit import test_steward_connections as tsc
from tests.unit.test_steward_capability_proposals import approve as approve_route
from tests.unit.test_steward_capability_proposals import propose as propose_route
from tests.unit.test_steward_capability_routes import key, put
from tests.unit.test_steward_connection_proposals import approve as approve_change
from tests.unit.test_steward_connection_proposals import owner_source, version
from tests.unit.test_steward_connections import (FakeUpstream, add, body_of, discover, listed, rpc,  # noqa: F401
                                                 server, shown, world)

DEV_URL = "http://dev_mcp:7676/mcp"
DEV_ADDRESS = tsc.ADDRESSES["dev_mcp"]
SUBAGENTS = ("subagent_start", "subagent_get", "subagent_list", "subagent_cancel", "subagent_message",
             "subagent_messages")


def _tool(name, read_only=None):
    tool = {"name": name, "description": f"dev-mcp {name}", "inputSchema": {"type": "object"}}
    if read_only is not None:
        tool["annotations"] = {"readOnlyHint": read_only}
    return tool


# dev-mcp's own tool list, with hints a server could send: bash claims to be
# read-only, which must not matter.
DEV_TOOLS = [_tool("list_dir", True), _tool("read_file", True), _tool("search", True), _tool("apply_patch"),
             _tool("bash", True)] + [_tool(name) for name in SUBAGENTS]


class NamedUpstream(FakeUpstream):
    """The fake MCP server, answering `initialize` with the server name `name`."""

    def __init__(self, name, tools):
        super().__init__(tools)
        self.name = name

    async def post(self, request):
        response = await super().post(request)
        if self.methods and self.methods[-1] == "initialize" and response.status_code == 200:
            data = json.loads(response.body)
            data["result"]["serverInfo"]["name"] = self.name
            return JSONResponse(data, headers={"mcp-session-id": response.headers["mcp-session-id"]})
        return response


@pytest.fixture
def upstream():
    return NamedUpstream(sc.DEV_MCP_SERVER_NAME, DEV_TOOLS)


def dev(world, conn_id="dev", visibility="private", enabled=True, url=DEV_URL):
    return add(world, conn_id=conn_id, url=url, visibility=visibility, enabled=enabled, label=conn_id.title())


def tools_set(world, items, conn_id="dev"):
    return execute(world.owner, "steward_connection_tools_set", {"conn_id": conn_id, "tools": items},
                   allow_write=True)


def allow(world, names, conn_id="dev", **flags):
    hashes = shown(world, conn_id)
    return tools_set(world, [{"raw_name": n, "allowed": True, "schema_sha256": hashes[n], **flags} for n in names],
                     conn_id)


def rows(world, conn_id="dev"):
    with world.db.get_connection() as conn:
        return {r["raw_name"]: dict(r) for r in conn.execute(
            "SELECT * FROM state_steward_connection_tools WHERE conn_id=?", (conn_id,))}


def sql(world, statement, *params):
    with world.db.get_connection() as conn:
        conn.execute(statement, params)
        conn.commit()


def ready_dev(world, names=("list_dir", "read_file", "bash", "apply_patch")):
    dev(world)
    discover(world, "dev", tools=DEV_TOOLS)
    allow(world, list(names))
    return listed(world)["connections"][0]["conn_uid"]


def pin(world, *names, conn_id="dev", expect=0, host=False):
    route = ([{"kind": "host", "tool": "calculate"}] if host else []) + [
        {"kind": "conn", "conn_id": conn_id, "raw_name": n} for n in names]
    return put(world, "code", route, expect=expect)


def owner_turn(world, turn="t1"):
    execute(world.steward, "set_steward_turn_audience", {"turn": turn, "audience": "owner"}, allow_write=True)


def group_turn(world, turn="t2"):
    execute(world.steward, "set_steward_turn_audience",
            {"turn": turn, "audience": "group", "group": {"channel": "wechat", "group_ref": "room1"}},
            allow_write=True)


# --- registration: only the owner's dashboard, only private, only the written name ------------

def test_a_public_dev_mcp_is_refused_and_a_private_one_never_made_public(world):
    with pytest.raises(StateGraphError, match="private_only"):
        dev(world, visibility="public")
    assert listed(world)["connections"] == []
    dev(world)
    with pytest.raises(StateGraphError, match="private_only"):
        dev(world, visibility="public")
    # A public connection moved onto dev_mcp is the same refusal.
    add(world, conn_id="media", visibility="public")
    with pytest.raises(StateGraphError, match="private_only"):
        add(world, conn_id="media", url="http://DEV_MCP:7676/mcp", visibility="public")
    assert {c["conn_id"]: (c["visibility"], c["url"]) for c in listed(world)["connections"]} == {
        "dev": ("private", DEV_URL), "media": ("public", "http://mcp_server:9003/mcp")}


def test_the_owners_dashboard_registers_a_private_dev_mcp(world):
    assert dev(world)["conn_id"] == "dev"
    assert listed(world)["connections"][0]["visibility"] == "private"


def test_nobody_but_the_owner_session_registers_it(world):
    payload = {"conn_id": "dev", "label": "Dev", "url": DEV_URL, "visibility": "private"}
    with pytest.raises(StateGraphError, match="steward_read_only"):
        execute(world.steward, "steward_connection_upsert", payload, allow_write=True)
    with pytest.raises(StateGraphError, match="owner_session_required"):
        execute(world.director, "steward_connection_upsert", payload, allow_write=True)
    assert listed(world)["connections"] == []


def _propose_add(world, url=DEV_URL):
    return execute(world.steward, "create_steward_proposal", {
        "request_key": key("dm"), "kind": "connection_change", "reason": "the owner asked",
        "payload": {"op": "add", "conn_id": "dev", "source": owner_source(world), "label": "Dev", "url": url,
                    "transport": "streamable-http", "auth_kind": "none", "visibility": "private"}},
        allow_write=True)


def test_a_proposal_cannot_add_it(world, monkeypatch):
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        _propose_add(world)
    # Not resolvable at that moment (dev_mcp down): the proposal is refused all the same.
    monkeypatch.delitem(tsc.ADDRESSES, "dev_mcp")
    with pytest.raises(StateGraphError, match="needs_dashboard: dev_mcp"):
        _propose_add(world)
    assert listed(world)["connections"] == []


def test_no_proposal_touches_a_registered_dev_mcp(world):
    ready_dev(world)
    hashes = shown(world, "dev")
    for op, fields in (("update", {"label": "Dev2"}), ("update", {"enabled": False}), ("remove", {}),
                       ("reenable", {"tools": [{"raw_name": "bash", "schema_sha256": hashes["bash"],
                                                "allowed": True}]})):
        with pytest.raises(StateGraphError, match="needs_dashboard: dev_mcp"):
            execute(world.steward, "create_steward_proposal", {
                "request_key": key("dm"), "kind": "connection_change", "reason": "r",
                "payload": {"op": op, "conn_id": "dev", "source": owner_source(world), **version(world, "dev"),
                            **fields}}, allow_write=True)


def test_a_connection_moved_onto_dev_mcp_after_a_proposal_fails_it_at_approval(world):
    add(world, conn_id="box", visibility="private")
    pid = execute(world.steward, "create_steward_proposal", {
        "request_key": key("dm"), "kind": "connection_change", "reason": "r",
        "payload": {"op": "update", "conn_id": "box", "source": owner_source(world), "label": "Box2",
                    **version(world, "box")}}, allow_write=True)["proposal_id"]
    add(world, conn_id="box", url=DEV_URL, visibility="private", label="Box")
    done = approve_change(world, pid)
    assert done["status"] == "failed" and done["result"]["error"] == "needs_dashboard: dev_mcp"
    assert listed(world)["connections"][0]["label"] == "Box"


@pytest.mark.parametrize("url", ["http://devalias:7676/mcp", f"https://{DEV_ADDRESS}:7676/mcp",
                                 "https://dev.tunnel.example/mcp", "http://aitelier:4444/mcp",
                                 "http://zvec-grep:7998/mcp", "http://aitelier-zg:7998/mcp"])
def test_an_alias_an_address_a_tunnel_or_another_reserved_service_stays_refused(world, monkeypatch, url):
    monkeypatch.setitem(tsc.ADDRESSES, "devalias", DEV_ADDRESS)
    monkeypatch.setitem(tsc.ADDRESSES, "dev.tunnel.example", "93.184.216.40")
    monkeypatch.setenv("AITELIER_DEV_MCP_HOSTS", "dev.tunnel.example, other.tunnel.example")
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        add(world, conn_id="box", url=url, visibility="private")
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        sc.checked_address(url, dev_mcp_ok=True)
    assert listed(world)["connections"] == []


def test_a_tunnel_name_is_refused_even_when_it_does_not_resolve(world, monkeypatch):
    monkeypatch.setenv("AITELIER_DEV_MCP_HOSTS", "dev.tunnel.example")
    for require in (True, False):
        with pytest.raises(StateGraphError, match="reserved_endpoint"):
            sc.checked_address("https://DEV.tunnel.example./mcp", require=require, dev_mcp_ok=True)


def test_without_the_flag_dev_mcp_is_reserved_as_before(world):
    """The rollback: code that never passes dev_mcp_ok refuses the row (next session open)."""
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        sc.checked_address(DEV_URL)
    assert sc.checked_address(DEV_URL, dev_mcp_ok=True) == DEV_ADDRESS


def test_dev_mcp_sharing_an_address_with_another_reserved_service_is_refused(world, monkeypatch):
    monkeypatch.setitem(tsc.ADDRESSES, "dev_mcp", tsc.ADDRESSES["aitelier"])
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        sc.checked_address(DEV_URL, dev_mcp_ok=True)


def test_oauth_and_proposals_never_open_it():
    # Only the dashboard's registration and the gateway's own dev_mcp row pass the flag.
    for module in (steward_oauth, ccp):
        assert "dev_mcp_ok" not in inspect.getsource(module)


# --- the route: dev-mcp tools on `code` only, from the dashboard only ---------------------------

def test_dev_mcp_tools_pin_on_code_only_and_only_the_pinnable_ones(world):
    ready_dev(world)
    assert caps.DEV_MCP_PINNABLE == ("list_dir", "read_file", "search", "bash", "apply_patch")
    for cap_id, route in (("search", [{"kind": "conn", "conn_id": "dev", "raw_name": "search"}]),
                          ("code", [{"kind": "conn", "conn_id": "dev", "raw_name": "subagent_start"}]),
                          ("memory", [{"kind": "native"}, {"kind": "conn", "conn_id": "dev", "raw_name": "read_file"}]),
                          ("fetch", [{"kind": "conn", "conn_id": "dev", "raw_name": "bash"}])):
        with pytest.raises(StateGraphError, match="dev_mcp_not_allowed"):
            put(world, cap_id, route)
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_steward_capabilities").fetchone()[0] == 0
    saved = pin(world, "bash", host=True)["setting"]["route"]
    assert [(e["kind"], e.get("tool") or e.get("raw_name")) for e in saved] == [("host", "calculate"), ("conn", "bash")]


def test_a_route_proposal_naming_a_dev_mcp_tool_is_refused(world):
    ready_dev(world)
    with pytest.raises(StateGraphError, match="needs_dashboard: dev_mcp"):
        propose_route(world, "code", ["calculate", "mcp__sw__dev__bash"])


def test_a_route_proposal_is_refused_at_approval_when_its_connection_became_dev_mcp(world):
    add(world, conn_id="box", visibility="private")
    discover(world, "box", tools=DEV_TOOLS)
    allow(world, ["search"], conn_id="box")
    pid = propose_route(world, "search", ["mcp__sw__box__search"])["proposal_id"]
    # The owner moves the address onto dev_mcp: the conn_uid is kept, and dev-mcp has a `search`.
    add(world, conn_id="box", url=DEV_URL, visibility="private", label="Box")
    done = approve_route(world, pid)
    assert done["status"] == "failed" and done["result"] == {"error": "needs_dashboard: dev_mcp"}
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_steward_capabilities").fetchone()[0] == 0


# --- the locked flags --------------------------------------------------------------------------

def test_discovery_ignores_dev_mcps_hints_and_every_new_tool_is_outward(world):
    dev(world)
    discover(world, "dev", tools=DEV_TOOLS)
    assert {n: (r["outward"], r["egress"], r["allowed"]) for n, r in rows(world).items()} == {
        n["name"]: (1, 1, 0) for n in DEV_TOOLS}


@pytest.mark.parametrize("item", [{"raw_name": "bash", "outward": False}, {"raw_name": "bash", "egress": False},
                                  {"raw_name": "apply_patch", "outward": False},
                                  {"raw_name": "apply_patch", "egress": False, "outward": True}])
def test_a_write_tools_outward_and_egress_are_locked(world, item):
    ready_dev(world)
    before = rows(world)
    with pytest.raises(StateGraphError, match="locked: dev_mcp"):
        tools_set(world, [item])
    assert rows(world) == before


@pytest.mark.parametrize("name", SUBAGENTS)
def test_a_subagent_tool_is_never_allowed(world, name):
    ready_dev(world)
    with pytest.raises(StateGraphError, match="locked: dev_mcp"):
        allow(world, [name])
    assert not rows(world)[name]["allowed"]


def test_a_read_tools_outward_is_the_owners_choice(world):
    ready_dev(world)
    tools_set(world, [{"raw_name": "list_dir", "outward": False}, {"raw_name": "bash", "daily_cap": 20}])
    assert (rows(world)["list_dir"]["outward"], rows(world)["bash"]["daily_cap"]) == (0, 20)


def test_flags_loosened_around_the_lock_are_put_back_and_never_served(world):
    """A row changed by another path (a direct write, a connection moved onto dev_mcp) is
    locked again at the next discovery and is never served loosened meanwhile."""
    ready_dev(world)
    hashes = shown(world, "dev")
    sql(world, "UPDATE state_steward_connection_tools SET outward=0, egress=0 WHERE conn_id='dev' AND raw_name='bash'")
    sql(world, "UPDATE state_steward_connection_tools SET allowed=1, allowed_sha256=? WHERE conn_id='dev' "
               "AND raw_name='subagent_start'", hashes["subagent_start"])
    served = {t["raw_name"]: t for t in listed(world)["connections"][0]["tools"]}
    assert "subagent_start" not in served
    assert (served["bash"]["outward"], served["bash"]["egress"]) == (True, True)
    discover(world, "dev", tools=DEV_TOOLS)
    assert (rows(world)["bash"]["outward"], rows(world)["bash"]["egress"], rows(world)["subagent_start"]["allowed"]) \
        == (1, 1, 0)


def test_a_connection_moved_onto_dev_mcp_keeps_no_loosened_flag(world):
    add(world, conn_id="box", visibility="private")
    discover(world, "box", tools=DEV_TOOLS)
    assert rows(world, "box")["bash"]["outward"] == 0  # its readOnlyHint, on an ordinary connection
    allow(world, ["bash"], conn_id="box", outward=False)
    hashes = shown(world, "box")
    tools_set(world, [{"raw_name": "subagent_start", "allowed": True, "schema_sha256": hashes["subagent_start"]}],
              conn_id="box")
    add(world, conn_id="box", url=DEV_URL, visibility="private", label="Box")
    assert (rows(world, "box")["bash"]["outward"], rows(world, "box")["subagent_start"]["allowed"]) == (1, 0)


def test_only_a_private_dev_mcps_pinnable_tools_are_ever_served(world):
    ready_dev(world)
    assert sorted(t["raw_name"] for t in listed(world)["connections"][0]["tools"]) == [
        "apply_patch", "bash", "list_dir", "read_file"]
    sql(world, "UPDATE state_steward_connections SET visibility='public' WHERE conn_id='dev'")  # never by a command
    assert listed(world)["connections"][0]["tools"] == []
    assert not any(name.startswith("mcp__sw__dev__") for name in listed(world)["effective_tools"])


# --- the gateway -------------------------------------------------------------------------------

def call(server, world, name, arguments=None):
    return body_of(rpc(server, world, "tools/call", {"name": name, "arguments": arguments or {}}))[0]


def code_of(answer):
    return answer.get("error", {}).get("data", {}).get("code")


def test_the_gateway_refuses_every_dev_mcp_call_the_code_route_does_not_pin(world, server, upstream):
    ready_dev(world)
    owner_turn(world)
    tools_set(world, [{"raw_name": "list_dir", "outward": False}])
    # Not set.
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    # Set missing.
    v = put(world, "code", [{"kind": "missing"}])["setting"]["version"]
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    # Only calculate.
    v = put(world, "code", [{"kind": "host", "tool": "calculate"}], expect=v)["setting"]["version"]
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    # Another tool of dev-mcp pinned, not this one.
    v = pin(world, "read_file", expect=v)["setting"]["version"]
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    assert "tools/call" not in upstream.methods
    # Pinned: forwarded (a read tool the owner made not outward).
    pin(world, "read_file", "list_dir", expect=v)
    answer = call(server, world, "dev__list_dir", {"path": "/"})
    assert json.loads(answer["result"]["content"][0]["text"])["echo"]["name"] == "list_dir"


def test_a_pin_on_another_conn_uid_lets_nothing_through(world, server, upstream):
    ready_dev(world)
    owner_turn(world)
    tools_set(world, [{"raw_name": "list_dir", "outward": False}])
    pin(world, "list_dir")
    execute(world.owner, "steward_connection_delete", {"conn_id": "dev"}, allow_write=True)
    ready_dev(world)  # the same conn_id and tool, a new conn_uid
    tools_set(world, [{"raw_name": "list_dir", "outward": False}])
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    assert "tools/call" not in upstream.methods


def test_a_write_tool_never_runs_without_a_confirmation(world, server, upstream):
    ready_dev(world)
    owner_turn(world)
    pin(world, "bash", "apply_patch", host=True)
    assert code_of(call(server, world, "dev__bash", {"path": "/", "cmd": "df -h"})) == "confirmation_required"
    # Whatever its stored flags say (a path around the lock): still confirmed first.
    sql(world, "UPDATE state_steward_connection_tools SET outward=0, egress=0 WHERE conn_id='dev'")
    assert code_of(call(server, world, "dev__bash", {"path": "/", "cmd": "df -h"})) == "confirmation_required"
    assert code_of(call(server, world, "dev__apply_patch", {"root": "/", "patch": "x"})) == "confirmation_required"
    assert "tools/call" not in upstream.methods


def test_a_group_turn_never_reaches_it(world, server, upstream):
    ready_dev(world)
    tools_set(world, [{"raw_name": "list_dir", "outward": False}])
    pin(world, "list_dir")
    group_turn(world)
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "not_visible_in_this_turn"
    assert "tools/call" not in upstream.methods


def test_a_subagent_tool_is_refused_even_allowed_and_pinned_behind_the_commands(world, server, upstream):
    uid = ready_dev(world)
    owner_turn(world)
    caps.ensure_schema(world.db)
    hashes = shown(world, "dev")
    sql(world, "UPDATE state_steward_connection_tools SET allowed=1, allowed_sha256=?, outward=0 WHERE conn_id='dev' "
               "AND raw_name='subagent_start'", hashes["subagent_start"])
    sql(world, "INSERT INTO state_steward_capabilities(cap_id,route_json,version,set_via,proposal_id,updated_at) "
               "VALUES('code',?,1,'dashboard',NULL,'2026-11-24T00:00:00Z')",
        json.dumps([{"kind": "conn", "conn_id": "dev", "conn_uid": uid, "raw_name": "subagent_start"}]))
    with world.db.get_connection() as conn:
        assert not caps.code_pin(conn, uid, "subagent_start")
    assert code_of(call(server, world, "dev__subagent_start", {"agent": "claude", "cwd": "/", "prompt": "x"})) \
        == "tool_not_allowed"
    assert "tools/call" not in upstream.methods
    served = [t["name"] for t in rpc(server, world, "tools/list").json()["result"]["tools"]]
    assert not any("subagent" in name for name in served)


def test_a_public_dev_mcp_row_is_refused_by_the_gateway(world, server, upstream):
    ready_dev(world)
    owner_turn(world)
    tools_set(world, [{"raw_name": "list_dir", "outward": False}])
    pin(world, "list_dir")
    sql(world, "UPDATE state_steward_connections SET visibility='public' WHERE conn_id='dev'")  # never by a command
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        execute(world.owner, "steward_connection_probe", {"conn_id": "dev"}, allow_write=True)
    assert upstream.initialized == 0


def test_another_connection_whose_server_is_dev_mcp_is_refused(world, server, upstream):
    """A tunnel or an alias the address check could not see: the server names itself."""
    add(world, conn_id="box", visibility="private")
    answer = execute(world.owner, "steward_connection_probe", {"conn_id": "box"}, allow_write=True)
    assert answer["error"] == "reserved_endpoint" and upstream.initialized == 1
    assert rows(world, "box") == {}
    upstream.name = " Dev-Tools-MCP "
    assert execute(world.owner, "steward_connection_probe", {"conn_id": "box"},
                   allow_write=True)["error"] == "reserved_endpoint"
    assert "tools/list" not in upstream.methods


def test_the_dev_mcp_row_must_be_dev_mcp_itself(world, server, upstream):
    dev(world)
    upstream.name = "fake"
    assert execute(world.owner, "steward_connection_probe", {"conn_id": "dev"},
                   allow_write=True)["error"] == "reserved_endpoint"
    assert rows(world) == {}
    upstream.name = sc.DEV_MCP_SERVER_NAME
    listed_tools = execute(world.owner, "steward_connection_probe", {"conn_id": "dev"}, allow_write=True)["tools"]
    assert {t["raw_name"] for t in listed_tools} == {t["name"] for t in DEV_TOOLS}
    assert rows(world)["bash"]["outward"] == 1


def test_the_gateway_passes_the_flag_for_the_private_dev_mcp_row_alone(world, server, upstream, monkeypatch):
    seen = []
    original = sc.checked_address

    def spy(url, *args, **kw):
        seen.append((url, kw.get("dev_mcp_ok", False)))
        return original(url, *args, **kw)

    ready_dev(world)
    owner_turn(world)
    monkeypatch.setattr(sc, "checked_address", spy)
    execute(world.owner, "steward_connection_probe", {"conn_id": "dev"}, allow_write=True)
    assert seen == [(DEV_URL, True)]
    # The file fetch judges the same row the same way.
    seen.clear()
    turn = "t1"
    gw.gateway(server_app()).seen_urls[("dev", "http://dev_mcp:7676/f.png")] = turn
    response = httpx.get(server.url + gw.PATH + "/dev/fetch", params={"url": "http://dev_mcp:7676/f.png"},
                         headers=tsc.headers(world), timeout=30)
    assert seen == [(DEV_URL, True)] and response.status_code != 403
    sql(world, "UPDATE state_steward_connections SET visibility='public' WHERE conn_id='dev'")
    seen.clear()
    response = httpx.get(server.url + gw.PATH + "/dev/fetch", params={"url": "http://dev_mcp:7676/f.png"},
                         headers=tsc.headers(world), timeout=30)
    assert seen == [(DEV_URL, False)] and response.status_code == 403
    assert response.json()["detail"] == "reserved_endpoint"


def server_app():
    from api.main import app
    return app


# --- each layer holds alone ----------------------------------------------------------------------

def test_the_dashboard_refuses_a_public_dev_mcp_even_without_the_visibility_check(world, monkeypatch):
    """Two layers refuse a public dev_mcp registration: `private_only`, and the address
    check, which opens dev_mcp's address for a private registration alone."""
    monkeypatch.setattr(sc, "check_visibility", lambda url, visibility: None)
    with pytest.raises(StateGraphError, match="reserved_endpoint"):
        dev(world, visibility="public")
    assert listed(world)["connections"] == []


def _leaky(world, monkeypatch, raw_name, **facts):
    """The gateway's own judgement with an effective set that leaked (a bug there serving a
    dev-mcp tool it must not, or with loosened flags): the judge must refuse by itself."""
    entry = {"tool": {"name": f"dev__{raw_name}", "description": "", "inputSchema": {"type": "object"}},
             "conn_id": "dev", "raw_name": raw_name, "gw_name": raw_name, "visibility": "private",
             "egress": False, "outward": False, "blocked_args": [], "daily_cap": None, **facts}
    monkeypatch.setattr(sc, "effective", lambda conn: [entry])


def test_the_judge_refuses_a_subagent_tool_even_pinnable_and_leaked(world, server, upstream, monkeypatch):
    uid = ready_dev(world)
    owner_turn(world)
    caps.ensure_schema(world.db)
    # A future edit making it pinnable, a route naming it, an effective set serving it.
    monkeypatch.setattr(caps, "DEV_MCP_PINNABLE", caps.DEV_MCP_PINNABLE + ("subagent_start",))
    sql(world, "INSERT INTO state_steward_capabilities(cap_id,route_json,version,set_via,proposal_id,updated_at) "
               "VALUES('code',?,1,'dashboard',NULL,'2026-11-24T00:00:00Z')",
        json.dumps([{"kind": "conn", "conn_id": "dev", "conn_uid": uid, "raw_name": "subagent_start"}]))
    _leaky(world, monkeypatch, "subagent_start")
    assert code_of(call(server, world, "dev__subagent_start", {"agent": "c", "cwd": "/", "prompt": "x"})) \
        == "tool_not_allowed"
    assert "tools/call" not in upstream.methods


def test_the_judge_refuses_a_public_dev_mcp_row_even_leaked(world, server, upstream, monkeypatch):
    ready_dev(world)
    owner_turn(world)
    pin(world, "list_dir")
    sql(world, "UPDATE state_steward_connections SET visibility='public' WHERE conn_id='dev'")
    _leaky(world, monkeypatch, "list_dir", visibility="public")
    assert code_of(call(server, world, "dev__list_dir", {"path": "/"})) == "tool_not_allowed"
    assert "tools/call" not in upstream.methods


@pytest.mark.parametrize("name", sc.DEV_MCP_WRITES)
def test_the_judge_confirms_a_write_tool_even_served_not_outward(world, server, upstream, monkeypatch, name):
    ready_dev(world)
    owner_turn(world)
    pin(world, name)
    _leaky(world, monkeypatch, name)
    assert code_of(call(server, world, f"dev__{name}", {"path": "/", "cmd": "rm -rf /"})) == "confirmation_required"
    assert "tools/call" not in upstream.methods


def test_compose_passes_the_tunnel_names_to_the_backend():
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
    backends = [s for s in compose["services"].values()
                if "AITELIER_STEWARD_HOST_ADDRESSES" in (s.get("environment") or {})]
    assert backends and all(s["environment"]["AITELIER_DEV_MCP_HOSTS"] == "${AITELIER_DEV_MCP_HOSTS:-}"
                            for s in backends)
