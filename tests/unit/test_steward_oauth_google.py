"""Step 24.3, Google: the steward's OAuth and gateway against Google's official remote MCP
servers (Drive, Docs; design-steward 7.8.14), as they answer today.

No Google server is reached. The fake below copies what their public, unauthenticated
documents and answers show: `initialize` and `tools/list` answer 200 without a token
(no 401 to read a `WWW-Authenticate` from); the protected-resource metadata is at
`/.well-known/oauth-protected-resource/mcp/v1` and names `https://accounts.google.com/`
(with a trailing '/') while that server's metadata says `https://accounts.google.com`;
no dynamic registration (the owner's own client); `iss` on the callback; an id_token
only for `openid`; a refresh answer without a new refresh token."""
from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api import steward_gateway as gw
from core import state_steward_connections as sc
from core import steward_oauth
from core.state_commands import execute
from core.state_graph import StateGraphError
from tests.unit.test_steward_connections import (ADDRESSES, add, allow, body_of, discover, listed, rpc, server,  # noqa: F401
                                                 upstream, world)

DRIVE = "https://drivemcp.googleapis.com/mcp/v1"
DOCS = "https://docsmcp.googleapis.com/mcp/v1"
GOOGLE = "https://accounts.google.com"
TOKEN = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
BASE = "https://aitelier.example"
SCOPE = "https://www.googleapis.com/auth/"
DRIVE_SCOPES = [SCOPE + "drive", SCOPE + "drive.readonly", SCOPE + "drive.file"]
CLIENT, CLIENT_SECRET = "1234-abc.apps.googleusercontent.com", "GOCSPX-owner"


def _id_token(claims: dict) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return part({"alg": "RS256"}) + "." + part(claims) + ".sig"


class FakeGoogle:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.codes: dict[str, dict] = {}
        self.access: dict[str, str] = {}
        self.refresh: dict[str, str] = {}
        self.token_forms: list[dict] = []
        self.revoked: list[str] = []
        self.account = "family.home@gmail.com"
        self.serial = 0
        self.mcp_status = 200

    def authorize(self, url: str) -> tuple[str, str]:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.serial += 1
        code = f"4/code-{self.serial}"
        self.codes[code] = {"client_id": query["client_id"], "scope": query["scope"].split(),
                            "redirect_uri": query["redirect_uri"]}
        return code, query["state"]

    def _issue(self, scopes, *, with_refresh=True):
        self.serial += 1
        access = f"ya29.access-{self.serial}"
        self.access[access] = self.account
        answer = {"access_token": access, "token_type": "Bearer", "expires_in": 3599, "scope": " ".join(scopes)}
        if with_refresh:
            refresh = f"1//refresh-{self.serial}"
            self.refresh[refresh] = self.account
            answer["refresh_token"] = refresh
        if "openid" in scopes:
            claims = {"iss": GOOGLE, "sub": "1077", "aud": CLIENT}
            if "email" in scopes:
                claims["email"] = self.account
            answer["id_token"] = _id_token(claims)
        return answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, method = str(request.url), request.method
        if url in (DRIVE, DOCS) and method == "POST":
            if self.mcp_status != 200:
                return httpx.Response(self.mcp_status)
            return httpx.Response(200, json={"id": 1, "jsonrpc": "2.0", "result": {
                "capabilities": {"tools": {"listChanged": False}}, "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "StatelessServer", "version": "ESF"}}})
        if url == "https://drivemcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1":
            return httpx.Response(200, json={"authorization_servers": [GOOGLE + "/"], "bearer_methods_supported": ["header"],
                                             "resource": DRIVE, "scopes_supported": DRIVE_SCOPES})
        if url == GOOGLE + "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": GOOGLE, "authorization_endpoint": GOOGLE + "/o/oauth2/v2/auth", "token_endpoint": TOKEN,
                "userinfo_endpoint": USERINFO, "revocation_endpoint": REVOKE,
                "code_challenge_methods_supported": ["plain", "S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                "authorization_response_iss_parameter_supported": True})
        if url == TOKEN:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append({**form, "_auth": request.headers.get("authorization")})
            basic = "Basic " + base64.b64encode(f"{CLIENT}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != basic:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form["grant_type"] == "authorization_code":
                bound = self.codes.pop(form["code"], None)
                if bound is None or bound["redirect_uri"] != form["redirect_uri"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue(bound["scope"]))
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] not in self.refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue(["openid"], with_refresh=False))
        if url == REVOKE:
            self.revoked.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()}["token"])
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def google(world, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(steward_oauth, "transport_for", lambda address: httpx.MockTransport(fake.handle))
    for host, address in (("drivemcp.googleapis.com", "172.217.112.4"), ("docsmcp.googleapis.com", "172.217.112.4"),
                          ("accounts.google.com", "74.125.206.84"), ("oauth2.googleapis.com", "142.250.110.95"),
                          ("openidconnect.googleapis.com", "172.217.112.4")):
        monkeypatch.setitem(ADDRESSES, host, address)
    monkeypatch.setenv("AITELIER_PUBLIC_BASE_URL", BASE)
    return fake


def drive_connection(world, conn_id="gdrive", visibility="private", url=DRIVE):
    add(world, conn_id=conn_id, label="Google Drive", url=url, auth_kind="oauth", visibility=visibility, enabled=False)
    world.owner.steward_connections.oauth_client_set(conn_id, CLIENT, CLIENT_SECRET)


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def authorize(world, google, conn_id="gdrive", iss=GOOGLE):
    url = world.owner.steward_connections.oauth_start(conn_id)["authorize_url"]
    code, state = google.authorize(url)
    return world.owner.steward_connections.oauth_complete(state, code, iss), url


# --- discovery and the authorization request -------------------------------------------------------

def test_a_google_server_that_answers_without_a_token_is_discovered_at_its_well_known_address(world, google):
    drive_connection(world)
    url = world.owner.steward_connections.oauth_start("gdrive")["authorize_url"]
    query = query_of(url)
    assert url.startswith(GOOGLE + "/o/oauth2/v2/auth?")
    assert query["client_id"] == CLIENT and query["redirect_uri"] == BASE + steward_oauth.CALLBACK_PATH
    assert query["code_challenge_method"] == "S256" and query["resource"] == DRIVE
    assert query["access_type"] == "offline" and query["prompt"] == "select_account consent"
    # What the server lists, and openid email for the account the tokens are bound to.
    assert query["scope"].split() == DRIVE_SCOPES + ["openid", "email"]
    # The owner's own client: no registration was tried; the issuer is the one its metadata spells.
    asked = [str(r.url) for r in google.requests]
    assert not any("register" in u for u in asked)
    assert sc.read_secret("gdrive")["pending"]["issuer"] == GOOGLE


def test_the_owner_narrows_the_scopes_and_the_listing_shows_them(world, google):
    drive_connection(world)
    service = world.owner.steward_connections
    answer = service.oauth_scope_set("gdrive", f"{SCOPE}drive.readonly  {SCOPE}drive.file {SCOPE}drive.readonly")
    assert answer["oauth_scope"] == f"{SCOPE}drive.readonly {SCOPE}drive.file"
    row = next(c for c in listed(world)["connections"] if c["conn_id"] == "gdrive")
    assert row["oauth_scope"] == f"{SCOPE}drive.readonly {SCOPE}drive.file"
    query = query_of(service.oauth_start("gdrive")["authorize_url"])
    assert query["scope"].split() == [SCOPE + "drive.readonly", SCOPE + "drive.file", "openid", "email"]
    # Given already: not twice.
    service.oauth_scope_set("gdrive", f"openid {SCOPE}drive.readonly email")
    assert query_of(service.oauth_start("gdrive")["authorize_url"])["scope"] == f"openid {SCOPE}drive.readonly email"
    # Cleared: what the server lists again.
    assert service.oauth_scope_set("gdrive", "")["oauth_scope"] is None
    assert query_of(service.oauth_start("gdrive")["authorize_url"])["scope"].split()[:3] == DRIVE_SCOPES


@pytest.mark.parametrize("scope", ['drive"x', "drive\\x", "aé", "x" * 1001, " ".join(["s"] * 31), ["s"]])
def test_a_scope_is_space_separated_printable_names(world, google, scope):
    drive_connection(world)
    with pytest.raises(StateGraphError, match="invalid_scope"):
        world.owner.steward_connections.oauth_scope_set("gdrive", scope)


def test_scopes_are_the_owners_and_only_for_an_oauth_connection(world, google):
    add(world, conn_id="media", enabled=False)
    with pytest.raises(StateGraphError, match="secret_not_applicable"):
        world.owner.steward_connections.oauth_scope_set("media", "x")
    assert next(c for c in listed(world)["connections"] if c["conn_id"] == "media")["oauth_scope"] is None
    drive_connection(world)
    with pytest.raises(StateGraphError):
        world.steward.steward_connections.oauth_scope_set("gdrive", "x")


@pytest.mark.parametrize("status", [500, 403, 302])
def test_a_server_that_neither_asks_for_a_token_nor_answers_is_not_discovered(world, google, status):
    drive_connection(world)
    google.mcp_status = status
    with pytest.raises(StateGraphError, match="oauth_discovery_failed"):
        world.owner.steward_connections.oauth_start("gdrive")


def test_a_server_that_answers_without_a_token_and_has_no_metadata_is_not_discovered(world, google):
    drive_connection(world, conn_id="gdocs", url=DOCS)  # the fake has no metadata for Docs
    with pytest.raises(StateGraphError, match="oauth_discovery_failed: no protected-resource metadata"):
        world.owner.steward_connections.oauth_start("gdocs")


# --- the callback, the account, refresh, sign-out ----------------------------------------------------

def test_the_whole_google_round_binds_the_account_of_the_id_token(world, google):
    drive_connection(world)
    done, _url = authorize(world, google)
    assert done["auth_identity"] == "family.home@gmail.com" and done["identity_confirmed"] is False
    exchange = next(f for f in google.token_forms if f["grant_type"] == "authorization_code")
    assert exchange["resource"] == DRIVE and len(exchange["code_verifier"]) >= 43
    assert "client_secret" not in exchange
    world.owner.steward_connections.identity_confirm("gdrive", "family.home@gmail.com")
    add(world, conn_id="gdrive", label="Google Drive", url=DRIVE, auth_kind="oauth", visibility="private", enabled=True)
    assert next(c for c in listed(world)["connections"] if c["conn_id"] == "gdrive")["usable"] is True
    # Under a minute left: refreshed; Google's answer carries no new refresh token - the old one stays.
    first = sc.read_secret("gdrive")["tokens"]
    secret = sc.read_secret("gdrive")
    secret["tokens"]["expires_at"] = steward_oauth._iso(datetime.now(timezone.utc) + timedelta(seconds=10))
    sc.write_secret("gdrive", secret)
    fresh = steward_oauth.ensure_token("gdrive", DRIVE)
    tokens = sc.read_secret("gdrive")["tokens"]
    assert fresh != first["access_token"] and tokens["refresh_token"] == first["refresh_token"]
    # Sign-out revokes at Google's revocation endpoint, the owner's scopes and client stay.
    world.owner.steward_connections.oauth_scope_set("gdrive", SCOPE + "drive.readonly")
    world.owner.steward_connections.oauth_sign_out("gdrive")
    assert first["refresh_token"] in google.revoked
    secret = sc.read_secret("gdrive")
    assert "tokens" not in secret and secret["client"]["client_id"] == CLIENT and secret["scope"] == SCOPE + "drive.readonly"


@pytest.mark.parametrize("iss", [GOOGLE + ".evil.example", "https://evil.example", None])
def test_the_callback_names_googles_issuer(world, google, iss):
    drive_connection(world)
    with pytest.raises(StateGraphError, match="oauth_issuer_mismatch"):
        authorize(world, google, iss=iss)


# --- the address connected to ----------------------------------------------------------------------

def test_an_ipv4_address_is_connected_to_before_an_ipv6_one(world, monkeypatch):
    # The backend's docker networks have no IPv6 route; Google's names resolve to both.
    assert sc.preferred_address({"2a00:1450:400c:c02::54", "74.125.206.84"}) == "74.125.206.84"
    assert sc.preferred_address({"172.217.113.4", "172.217.112.4", "2001:4860:4840:400::"}) == "172.217.112.4"
    assert sc.preferred_address({"2001:4860:4841:400::", "2001:4860:4840:400::"}) == "2001:4860:4840:400::"

    def both(host, port):
        if host != "accounts.google.com":
            raise socket.gaierror(host)
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2a00:1450:400c:c02::54", 0, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("74.125.206.84", 0))]
    monkeypatch.setattr(sc, "RESOLVER", both)
    assert steward_oauth.judge_endpoint(GOOGLE + "/o/oauth2/v2/auth", DRIVE) == "74.125.206.84"
    assert sc.checked_address(GOOGLE + "/mcp") == "74.125.206.84"


# --- the gateway with a Google-shaped upstream -------------------------------------------------------

class StatelessUpstream:
    """Google's MCP servers: JSON answers, no Mcp-Session-Id, the bearer checked on calls."""

    def __init__(self, google):
        self.google = google
        self.methods: list[str] = []
        self.unauthorized = 0
        self.app = Starlette(routes=[Route("/mcp/v1", self.post, methods=["POST"]),
                                     Route("/mcp/v1", lambda r: Response(status_code=405), methods=["GET", "DELETE"])])

    async def post(self, request: Request):
        body = json.loads(await request.body())
        method, mid = body.get("method"), body.get("id")
        self.methods.append(method)
        if method == "tools/call" and request.headers.get("authorization", "").removeprefix("Bearer ") \
                not in self.google.access:
            self.unauthorized += 1
            return Response(status_code=401)
        if mid is None:
            return Response(status_code=202)
        if method == "initialize":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False},
                                                                  "resources": {"listChanged": False}},
                "serverInfo": {"name": "StatelessServer", "version": "ESF"}}})
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "search_files", "description": "Search Drive", "inputSchema": {
                    "type": "object", "properties": {"query": {"type": "string"}}},
                 "annotations": {"readOnlyHint": True}}]}})
        if method == "tools/call":
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps({"files": [], "asked": body["params"]})}]}})
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


def test_the_gateway_lists_and_calls_a_google_shaped_server_with_the_bearer(world, google, server, monkeypatch):
    fake = StatelessUpstream(google)
    monkeypatch.setattr(gw, "transport_for", lambda address: httpx.ASGITransport(app=fake.app))
    drive_connection(world)
    authorize(world, google)
    world.owner.steward_connections.identity_confirm("gdrive", "family.home@gmail.com")
    add(world, conn_id="gdrive", label="Google Drive", url=DRIVE, auth_kind="oauth", visibility="private", enabled=True)
    probed = world.owner.steward_connections.probe("gdrive")
    assert [t["raw_name"] for t in probed["tools"]] == ["search_files"]
    allow(world, "gdrive", names=["search_files"])
    execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"}, allow_write=True)
    call = lambda: body_of(rpc(server, world, "tools/call", {"name": "gdrive__search_files",
                                                             "arguments": {"query": "budget"}}))[0]
    assert "result" in call()
    # Google stops accepting the token: one 401, one refresh, the call sent again.
    google.access.clear()
    assert "result" in call()
    assert fake.unauthorized == 1 and [f["grant_type"] for f in google.token_forms].count("refresh_token") == 1
