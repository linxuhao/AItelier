"""Step 24.1b: OAuth with an identity check, and outward actions confirmed by the person
who asked (design-steward 7.8.6, 7.8.9, tests 7.8.18 "OAuth", "确认", "网关 / 对外").

No real authorization server is reached: a fake one (discovery, registration, PKCE,
token, refresh, revocation, userinfo) answers through core.steward_oauth.transport_for,
and the gateway's upstream is the fake MCP server of tests/unit/test_steward_connections.py
behind an ASGI transport, here one that wants a bearer token."""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api import steward_gateway as gw
from core import drivers, state_channels, state_steward_confirmations as sconf, state_steward_connections as sc
from core import steward_oauth
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_graph import StateGraphError, StateNotFound
from core.state_service import StateService
from tests.unit.test_steward_connections import (ADDRESSES, HOME, OWNER, PEPPER, TOOLS, FakeUpstream, add, allow,
                                                 body_of, discover, listed, rpc, server, upstream, world)  # noqa: F401

MAIL = "https://mail.example/mcp"
ISSUER = "https://auth.example"
BASE = "https://aitelier.example"
OTHER_OWNER = "owner:someone-else@example.com"


# --- a fake authorization server --------------------------------------------------------------

class FakeAuth:
    """The protected resource's 401 and metadata, and an authorization server: dynamic
    registration, an authorization code bound to its PKCE challenge, the token endpoint
    (code and refresh grants), revocation and userinfo. Every request is recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.registered = 0
        self.codes: dict[str, dict] = {}
        self.access: dict[str, str] = {}      # access token -> identity
        self.refresh: dict[str, str] = {}     # refresh token -> identity
        self.revoked: list[str] = []
        self.identity = "family@example.com"
        self.expires_in = 3600
        self.refuse_refresh = False
        self.grants: list[str] = []
        self.metadata_overrides: dict = {}
        self.prm_overrides: dict = {}
        self.token_forms: list[dict] = []
        self.serial = 0

    def metadata(self):
        return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register", "revocation_endpoint": ISSUER + "/revoke",
                "userinfo_endpoint": ISSUER + "/userinfo", "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True, **self.metadata_overrides}

    def authorize(self, url: str) -> tuple[str, str]:
        """What the browser does at the authorization endpoint: sign in, consent, come back
        with a code (bound to the challenge, the client and the resource) and the state."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.serial += 1
        code = f"code-{self.serial}"
        self.codes[code] = {"challenge": query["code_challenge"], "client_id": query["client_id"],
                            "redirect_uri": query["redirect_uri"], "resource": query["resource"]}
        return code, query["state"]

    def _issue(self, identity):
        self.serial += 1
        access, refresh = f"access-{self.serial}", f"refresh-{self.serial}"
        self.access[access] = identity
        self.refresh[refresh] = identity
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in, "refresh_token": refresh}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, method = str(request.url), request.method
        if url == MAIL and method == "POST":
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token", '
                                  'resource_metadata="https://mail.example/.well-known/oauth-protected-resource/mcp"'})
        if url == "https://mail.example/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": MAIL, "authorization_servers": [ISSUER],
                                             "scopes_supported": ["mail"], **self.prm_overrides})
        if url == ISSUER + "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=self.metadata())
        if url == ISSUER + "/register":
            self.registered += 1
            body = json.loads(request.content)
            assert body["token_endpoint_auth_method"] == "none" and body["redirect_uris"] == [BASE + steward_oauth.CALLBACK_PATH]
            return httpx.Response(201, json={"client_id": f"client-{self.registered}", "token_endpoint_auth_method": "none"})
        if url == ISSUER + "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append({**form, "_auth": request.headers.get("authorization")})
            self.grants.append(form["grant_type"])
            if form["grant_type"] == "authorization_code":
                bound = self.codes.pop(form["code"], None)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()
                                                     ).decode().rstrip("=")
                if (bound is None or bound["challenge"] != challenge or bound["redirect_uri"] != form["redirect_uri"]
                        or bound["resource"] != form["resource"]):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue(self.identity))
            if form["grant_type"] == "refresh_token":
                if self.refuse_refresh or form["refresh_token"] not in self.refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                identity = self.refresh.pop(form["refresh_token"])
                return httpx.Response(200, json=self._issue(identity))
        if url == ISSUER + "/revoke":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked.append(form["token"])
            return httpx.Response(200)
        if url == ISSUER + "/userinfo":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            return httpx.Response(200, json={"email": self.access[token]}) if token in self.access \
                else httpx.Response(401)
        return httpx.Response(404)


@pytest.fixture
def auth(world, monkeypatch):
    fake = FakeAuth()
    monkeypatch.setattr(steward_oauth, "transport_for", lambda address: httpx.MockTransport(fake.handle))
    for host, address in (("mail.example", "93.184.216.40"), ("auth.example", "93.184.216.41"),
                          ("evil.example", "93.184.216.42")):
        monkeypatch.setitem(ADDRESSES, host, address)
    monkeypatch.setenv("AITELIER_PUBLIC_BASE_URL", BASE)
    return fake


def oauth_connection(world, conn_id="mailpub", visibility="public"):
    return add(world, conn_id=conn_id, label=conn_id.title(), url=MAIL, auth_kind="oauth", visibility=visibility,
               enabled=False)


def authorize(world, auth, conn_id="mailpub", service=None):
    """The owner's whole round: start, the browser at the server, the callback."""
    service = service or world.owner
    url = service.steward_connections.oauth_start(conn_id)["authorize_url"]
    code, state = auth.authorize(url)
    return service.steward_connections.oauth_complete(state, code, ISSUER), url


def tokens_of(conn_id="mailpub"):
    return (sc.read_secret(conn_id) or {}).get("tokens") or {}


# --- OAuth --------------------------------------------------------------------------------------

def test_the_whole_authorization_and_the_account_check_before_enabling(world, auth):
    oauth_connection(world)
    done, url = authorize(world, auth)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(ISSUER + "/authorize?")
    assert query["response_type"] == "code" and query["client_id"] == "client-1" and query["scope"] == "mail"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["resource"] == MAIL and query["prompt"] == "login"
    assert query["redirect_uri"] == BASE + "/api/steward/connections/oauth/callback"
    assert query["state"].startswith("mailpub.")
    # The tokens are in the 0600 secret file, never in State; the account is read back.
    assert done["auth_identity"] == "family@example.com" and done["identity_confirmed"] is False
    assert done["secret"] == "set" and done["usable"] is False
    path = world.tmp / "secrets" / "mailpub.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secret = json.loads(path.read_text())
    assert secret["tokens"]["access_token"].startswith("access-") and "pending" not in secret
    for file in world.tmp.glob("state.sqlite*"):
        data = file.read_bytes()
        assert secret["tokens"]["access_token"].encode() not in data and secret["tokens"]["refresh_token"].encode() not in data
    assert secret["tokens"]["access_token"] not in json.dumps(listed(world))
    # Not enabled until the owner confirms the account; then its tools are effective.
    discover(world, "mailpub", tools=TOOLS)
    allow(world, "mailpub", names=["send_message", "web_search"])
    with pytest.raises(StateGraphError, match="identity_unconfirmed"):
        add(world, conn_id="mailpub", label="Mailpub", url=MAIL, auth_kind="oauth", visibility="public", enabled=True)
    assert listed(world)["effective_tools"] == []
    with pytest.raises(StateGraphError, match="identity_changed"):
        world.owner.steward_connections.identity_confirm("mailpub", "owner@example.com")
    world.owner.steward_connections.identity_confirm("mailpub", "family@example.com")
    add(world, conn_id="mailpub", label="Mailpub", url=MAIL, auth_kind="oauth", visibility="public", enabled=True)
    assert listed(world)["effective_tools"] == ["mcp__sw__mailpub__send_message", "mcp__sw__mailpub__web_search"]
    # The code grant carried the verifier of the challenge and the resource indicator.
    exchange = next(f for f in auth.token_forms if f["grant_type"] == "authorization_code")
    assert exchange["resource"] == MAIL and exchange["client_id"] == "client-1" and exchange["_auth"] is None


def test_the_state_is_this_owner_sessions_used_once_and_for_ten_minutes(world, auth, monkeypatch):
    oauth_connection(world)
    url = world.owner.steward_connections.oauth_start("mailpub")["authorize_url"]
    code, state = auth.authorize(url)
    other = StateService(world.db, actor=OTHER_OWNER, project_read_trusted=True)
    with pytest.raises(StateGraphError, match="oauth_state_unknown"):
        other.steward_connections.oauth_complete(state, code, ISSUER)
    # The other session's try consumed it: single use, whoever tried.
    with pytest.raises(StateGraphError, match="oauth_state_unknown"):
        world.owner.steward_connections.oauth_complete(state, code, ISSUER)
    with pytest.raises(StateGraphError, match="oauth_state_unknown"):
        world.owner.steward_connections.oauth_complete("mailpub.made-up", code, ISSUER)
    url = world.owner.steward_connections.oauth_start("mailpub")["authorize_url"]
    code, state = auth.authorize(url)
    world.owner.steward_connections.oauth_complete(state, code, ISSUER)
    with pytest.raises(StateGraphError, match="oauth_state_unknown"):
        world.owner.steward_connections.oauth_complete(state, code, ISSUER)
    url = world.owner.steward_connections.oauth_start("mailpub")["authorize_url"]
    code, state = auth.authorize(url)
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    monkeypatch.setattr(steward_oauth, "_now", lambda: later)
    with pytest.raises(StateGraphError, match="oauth_state_expired"):
        world.owner.steward_connections.oauth_complete(state, code, ISSUER)
    # Only the owner's browser session starts or completes one.
    with pytest.raises(StateGraphError, match="owner_session_required"):
        world.steward.steward_connections.oauth_start("mailpub")
    with pytest.raises(StateGraphError, match="owner_session_required"):
        world.director.steward_connections.oauth_complete(state, code, ISSUER)


def test_the_issuer_in_the_answer_must_be_the_server_asked(world, auth):
    oauth_connection(world)
    for iss, match in (("https://evil.example", "oauth_issuer_mismatch"), (None, "oauth_issuer_mismatch")):
        url = world.owner.steward_connections.oauth_start("mailpub")["authorize_url"]
        code, state = auth.authorize(url)
        with pytest.raises(StateGraphError, match=match):
            world.owner.steward_connections.oauth_complete(state, code, iss)
    assert "token_endpoint" not in json.dumps([str(r.url) for r in auth.requests if r.url.path == "/token"])
    assert not tokens_of()
    assert listed(world)["connections"][0]["last_error"] == "oauth_issuer_mismatch"


@pytest.mark.parametrize("overrides,where,code", [
    ({"token_endpoint": "http://auth.example/token"}, "metadata", "oauth_endpoint_refused"),
    ({"token_endpoint": "https://lanhost/token"}, "metadata", "reserved_endpoint"),
    ({"token_endpoint": "https://127.0.0.1/token"}, "metadata", "reserved_endpoint"),
    ({"registration_endpoint": "https://aitelier/register"}, "metadata", "reserved_endpoint"),
    ({"issuer": "https://evil.example"}, "metadata", "oauth_issuer_mismatch"),
    ({"code_challenge_methods_supported": ["plain"]}, "metadata", "oauth_pkce_unsupported"),
    ({"authorization_servers": ["http://auth.example"]}, "prm", "oauth_endpoint_refused"),
    ({"authorization_servers": ["https://dev_mcp:7676"]}, "prm", "reserved_endpoint"),
    ({"resource": "https://evil.example/mcp"}, "prm", "oauth_resource_mismatch"),
])
def test_discovery_judges_every_address_it_is_given(world, auth, overrides, where, code):
    oauth_connection(world)
    (auth.metadata_overrides if where == "metadata" else auth.prm_overrides).update(overrides)
    with pytest.raises(StateGraphError, match=code):
        world.owner.steward_connections.oauth_start("mailpub")
    assert auth.registered == 0 and "pending" not in (sc.read_secret("mailpub") or {})


def test_an_owner_registered_client_is_used_without_registration(world, auth):
    oauth_connection(world)
    world.owner.steward_connections.oauth_client_set("mailpub", "owner-client", "owner-secret")
    _done, url = authorize(world, auth)
    assert auth.registered == 0 and parse_qs(urlsplit(url).query)["client_id"] == ["owner-client"]
    exchange = next(f for f in auth.token_forms if f["grant_type"] == "authorization_code")
    assert exchange["_auth"] == "Basic " + base64.b64encode(b"owner-client:owner-secret").decode()
    assert "client_secret" not in exchange and "owner-secret" not in json.dumps(listed(world))


def test_a_second_authorization_with_another_account_is_unconfirmed_and_off(world, auth):
    oauth_connection(world)
    authorize(world, auth)
    world.owner.steward_connections.identity_confirm("mailpub", "family@example.com")
    add(world, conn_id="mailpub", label="Mailpub", url=MAIL, auth_kind="oauth", visibility="public", enabled=True)
    generation = listed(world)["connections"][0]["generation"]
    # The same account again: still confirmed and on (a new generation).
    again, _ = authorize(world, auth)
    assert again["identity_confirmed"] is True and again["enabled"] is True and again["generation"] == generation + 1
    # The browser reused the owner's own login: another account - unconfirmed and off.
    auth.identity = "owner@example.com"
    other, _ = authorize(world, auth)
    assert other["auth_identity"] == "owner@example.com"
    assert other["identity_confirmed"] is False and other["enabled"] is False and other["usable"] is False


def test_sign_out_delete_and_a_new_address_revoke_and_forget_the_tokens(world, auth):
    oauth_connection(world)
    authorize(world, auth)
    first = tokens_of()
    world.owner.steward_connections.oauth_sign_out("mailpub")
    assert first["refresh_token"] in auth.revoked and first["access_token"] in auth.revoked
    row = listed(world)["connections"][0]
    assert row["secret"] == "oauth_needs_consent" and row["auth_identity"] is None and not tokens_of()
    assert (sc.read_secret("mailpub") or {}).get("client"), "the registration stays"
    authorize(world, auth)
    second = tokens_of()
    add(world, conn_id="mailpub", label="Mailpub", url="https://evil.example/mcp", auth_kind="oauth",
        visibility="public", enabled=False)
    assert second["refresh_token"] in auth.revoked and not tokens_of(), "never sent to a new address"
    assert listed(world)["connections"][0]["auth_identity"] is None
    add(world, conn_id="mailpub", label="Mailpub", url=MAIL, auth_kind="oauth", visibility="public", enabled=False)
    authorize(world, auth)
    third = tokens_of()
    execute(world.owner, "steward_connection_delete", {"conn_id": "mailpub"}, allow_write=True)
    assert third["refresh_token"] in auth.revoked and not (world.tmp / "secrets" / "mailpub.json").exists()


def test_startup_revokes_then_deletes_the_tokens_of_a_connection_that_is_gone(world, auth):
    oauth_connection(world)
    authorize(world, auth)
    tokens = tokens_of()
    with world.db.get_connection() as conn:
        conn.execute("DELETE FROM state_steward_connections WHERE conn_id='mailpub'")
        conn.commit()
    assert sc.prune_orphan_secrets(world.owner.store) == ["mailpub"]
    assert tokens["refresh_token"] in auth.revoked and not (world.tmp / "secrets" / "mailpub.json").exists()


def test_refresh_under_a_minute_left_and_a_refused_refresh_needs_consent_once(world, auth):
    oauth_connection(world)
    authorize(world, auth)
    world.owner.steward_connections.identity_confirm("mailpub", "family@example.com")
    first = tokens_of()["access_token"]
    assert steward_oauth.ensure_token("mailpub", MAIL) == first and auth.grants == ["authorization_code"]
    secret = sc.read_secret("mailpub")
    secret["tokens"]["expires_at"] = steward_oauth._iso(datetime.now(timezone.utc) + timedelta(seconds=30))
    sc.write_secret("mailpub", secret)
    fresh = steward_oauth.ensure_token("mailpub", MAIL)
    assert fresh != first and auth.grants[-1] == "refresh_token"
    refresh_form = auth.token_forms[-1]
    assert refresh_form["resource"] == MAIL and refresh_form["client_id"] == "client-1"
    # A token another call already replaced is reused, not refreshed again.
    assert steward_oauth.ensure_token("mailpub", MAIL, stale=first) == fresh and auth.grants.count("refresh_token") == 1
    changes = []
    world_hooks = sc.TOOLS_CHANGED_HOOKS
    world_hooks.append(lambda: changes.append(1))
    auth.refuse_refresh = True
    for _ in range(2):
        with pytest.raises(steward_oauth.RefreshRefused):
            steward_oauth.ensure_token("mailpub", MAIL, stale=fresh, store=world.owner.store)
    assert listed(world)["connections"][0]["secret"] == "oauth_needs_consent" and changes
    with world.db.get_connection() as conn:
        notes = conn.execute("SELECT body FROM driver_inbox_messages WHERE subject='A connection needs authorization'"
                             ).fetchall()
    assert len(notes) == 1 and "mailpub" in notes[0][0], "told once"
    # A new authorization clears it.
    auth.refuse_refresh = False
    authorize(world, auth)
    assert listed(world)["connections"][0]["secret"] == "set"


# --- the gateway with an OAuth upstream ------------------------------------------------------------

class BearerUpstream(FakeUpstream):
    """The fake MCP server, wanting one of the fake server's current access tokens."""

    def __init__(self, auth_server):
        super().__init__()
        self.auth_server = auth_server
        self.unauthorized = 0

    async def guarded(self, request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.auth_server.access:
            self.unauthorized += 1
            return Response(status_code=401)
        return await self.post(request)


def _bearer_server(world, auth, monkeypatch):
    fake = BearerUpstream(auth)
    from starlette.applications import Starlette
    from starlette.routing import Route
    fake.app = Starlette(routes=[Route("/mcp", fake.guarded, methods=["POST"]),
                                 Route("/mcp", fake.other, methods=["GET", "DELETE"])])
    monkeypatch.setattr(gw, "transport_for", lambda address: httpx.ASGITransport(app=fake.app))
    return fake


def oauth_ready(world, auth):
    oauth_connection(world)
    authorize(world, auth)
    world.owner.steward_connections.identity_confirm("mailpub", "family@example.com")
    add(world, conn_id="mailpub", label="Mailpub", url=MAIL, auth_kind="oauth", visibility="public", enabled=True)
    discover(world, "mailpub", tools=TOOLS)
    allow(world, "mailpub", names=["web_search", "send_message"])
    execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"}, allow_write=True)


def test_the_gateway_refreshes_without_reopening_the_session(world, auth, server, monkeypatch):
    fake = _bearer_server(world, auth, monkeypatch)
    oauth_ready(world, auth)
    call = lambda q: body_of(rpc(server, world, "tools/call", {"name": "mailpub__web_search", "arguments": {"query": q}}))[0]
    assert "result" in call("a")
    # The upstream stops accepting the token (revoked there): one 401, one refresh, one resend.
    auth.access.clear()
    assert "result" in call("b")
    assert fake.unauthorized == 1 and auth.grants.count("refresh_token") == 1
    # Under a minute left: refreshed before it is sent.
    secret = sc.read_secret("mailpub")
    secret["tokens"]["expires_at"] = steward_oauth._iso(datetime.now(timezone.utc) + timedelta(seconds=10))
    sc.write_secret("mailpub", secret)
    assert "result" in call("c")
    assert auth.grants.count("refresh_token") == 2 and fake.unauthorized == 1
    assert fake.initialized == 1, "a refreshed token never opens a new upstream session"
    # The refresh is refused: out of the effective set, the call unavailable, nothing forwarded.
    auth.refuse_refresh = True
    auth.access.clear()
    before = fake.methods.count("tools/call")
    assert call("d")["error"]["data"]["code"] == "connection_unavailable"
    assert listed(world)["effective_tools"] == []
    assert call("e")["error"]["data"]["code"] == "connection_unavailable"
    assert fake.methods.count("tools/call") == before
    assert not [t for t in rpc(server, world, "tools/list").json()["result"]["tools"] if t["name"].startswith("mailpub")]


# --- confirmations --------------------------------------------------------------------------------

def bind_slack(world, monkeypatch, sender="U0OWNER", channel="slack"):
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    monkeypatch.setenv("AITELIER_CHANNEL_RATE_PER_MINUTE", "100")
    code = world.owner.channels.issue_code(channel)["code"]
    execute(world.steward, "bind_owner_channel", {"request_key": f"bind-{channel}", "channel": channel,
                                                  "sender_ref": sender, "code": code}, allow_write=True)


def relay(world, body, ref, channel="slack", sender="U0OWNER", at="now"):
    channel_at = state_channels._now().isoformat() if at == "now" else at
    return execute(world.steward, "relay_owner_message", {
        "request_key": f"relay-{ref}", "channel": channel, "sender_ref": sender, "channel_message_ref": ref,
        "body": body, **({"channel_at": channel_at} if channel_at is not None else {})}, allow_write=True)


def dashboard(world, body, key, service=None):
    return execute(service or world.owner, "send_user_conversation_message",
                   {"request_key": key, "project_id": HOME, "body": body}, allow_write=True)


def mail_ready(world, conn_id="mailpub", visibility="public"):
    """A header connection (no OAuth needed for the confirmation path) with send_message outward."""
    add(world, conn_id=conn_id, label="家庭公共邮箱" if conn_id == "mailpub" else conn_id, url="https://good.example/mcp",
        auth_kind="header", visibility=visibility)
    execute(world.owner, "steward_connection_secret_set", {"conn_id": conn_id, "value": "Bearer x"}, allow_write=True)
    discover(world, conn_id, tools=TOOLS)
    allow(world, conn_id, names=["web_search", "send_message"])
    execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "owner"}, allow_write=True)


SEND = "mcp__sw__mailpub__send_message"


def ask(world, message_id, arguments=None, name=SEND):
    return execute(world.steward, "create_steward_confirmation", {
        "name": name, "arguments": arguments if arguments is not None else {"to": "a@example.com", "body": "周六聚餐"},
        "requester_message_id": message_id}, allow_write=True)


def status_of(world, code):
    return execute(world.steward, "get_steward_confirmation", {"code": code}, allow_write=True)["status"]


def test_only_a_persons_message_of_a_channel_or_the_dashboard_can_ask(world, monkeypatch):
    mail_ready(world)
    # The steward's own message, a made-up id, a message of another project.
    sent = execute(world.steward, "send_user_message", {"request_key": "s1", "project_id": HOME, "subject": "x",
                                                        "body": "hello"}, allow_write=True)
    for message_id in (sent["message_id"], "no-such-message"):
        with pytest.raises(StateGraphError, match="no_requester"):
            ask(world, message_id)
    # The owner's terminal talks to the steward but never confirms (CLI_OWNER_ACTOR).
    cli = StateService(world.db, actor=drivers.CLI_OWNER_ACTOR, project_read_trusted=True)
    from_terminal = dashboard(world, "send it", "cli-1", service=cli)
    with pytest.raises(StateGraphError, match="no_requester"):
        ask(world, from_terminal["message_id"])
    from_browser = dashboard(world, "send it", "o-1")
    assert ask(world, from_browser["message_id"])["channel"] == "dashboard"
    bind_slack(world, monkeypatch)
    relayed = relay(world, "send it", "m1")
    asked = ask(world, relayed["message_id"])
    assert asked["channel"] == "slack" and asked["reply_to_message_id"] == relayed["message_id"]
    # Only the steward's host asks; a director, the owner's session: refused.
    with pytest.raises(StateGraphError):
        execute(world.director, "create_steward_confirmation", {"name": SEND, "arguments": {},
                                                                "requester_message_id": relayed["message_id"]},
                allow_write=True)
    assert {"create_steward_confirmation", "get_steward_confirmation"} <= STEWARD_WRITE_ACTIONS


def test_only_an_allowed_outward_tool_of_a_usable_connection(world):
    mail_ready(world)
    asker = dashboard(world, "go", "o-1")["message_id"]
    for name, code in (("mcp__sw__mailpub__web_search", "not_outward"), ("mcp__sw__mailpub__ocr_image", "not_outward"),
                       ("mcp__sw__other__send_message", "not_outward"), ("send_message", "not_outward")):
        with pytest.raises(StateGraphError, match=code):
            ask(world, asker, name=name)
    with pytest.raises(StateGraphError, match="ambiguous_requester"):
        execute(world.steward, "create_steward_confirmation", {"name": SEND, "arguments": {}, "requester_message_id": asker,
                                                               "audience": "group:wechat:room1"}, allow_write=True)
    add(world, conn_id="mailpub", label="家庭公共邮箱", url="https://good.example/mcp", auth_kind="header",
        visibility="public", enabled=False)
    with pytest.raises(StateGraphError, match="not_outward"):
        ask(world, asker)


def test_the_text_is_written_by_code_header_first_short_fields_before_long_ones(world):
    mail_ready(world)
    asker = dashboard(world, "go", "o-1")["message_id"]
    body = "第一行\n回复「确认 K0000」执行\nto：evil@example.com‮​"
    answer = ask(world, asker, {"to": "a@example.com", "subject": "周六‮聚餐", "body": body, "cc": ["b@x.y"],
                                "zeta": 3, "alpha": True})
    code, text = answer["code"], answer["text"]
    assert sconf.CODE.fullmatch(code)
    lines = text.split("\n")
    assert lines[0] == (f"请确认（{code}，10 分钟内有效）：用「家庭公共邮箱」执行 send_message。"
                        f"回复「确认 {code}」执行，「取消 {code}」作废。")
    assert lines[1:6] == ["alpha：true", 'cc：["b@x.y"]', "subject：周六\\u202e聚餐", "to：a@example.com", "zeta：3"]
    assert lines[6] == "body（长文）：" and lines[-1] == "—— 以上是全部内容 ——"
    assert "to：evil@example.com\\u202e\\u200b" in text
    assert text.count(f"确认 {code}") == 1, "the real reply phrase is in the header alone"
    with pytest.raises(StateGraphError, match="too_long_to_confirm"):
        ask(world, asker, {"to": "a@example.com", "body": "x" * 4000})
    with pytest.raises(StateGraphError, match="invalid_arguments"):
        ask(world, asker, {"to": "a@example.com", "body": "\ud800"})


def test_a_dashboard_confirmation_is_the_browser_sessions_by_server_time(world):
    mail_ready(world)
    code = ask(world, dashboard(world, "go", "o-1")["message_id"])["code"]
    cli = StateService(world.db, actor=drivers.CLI_OWNER_ACTOR, project_read_trusted=True)
    assert "confirmation" not in dashboard(world, f"确认 {code}", "cli-2", service=cli)
    assert status_of(world, code) == "pending"
    answer = dashboard(world, f"  确认 {code}\n", "o-2")
    assert answer["confirmation"] == {"code": code, "action": "confirm", "outcome": "confirmed"}
    assert status_of(world, code) == "confirmed"


def test_a_channel_confirmation_is_the_exact_reply_of_the_same_sender_on_the_same_channel(world, monkeypatch):
    mail_ready(world)
    bind_slack(world, monkeypatch)
    bind_slack(world, monkeypatch, sender="wx-owner", channel="wechat")
    code = ask(world, relay(world, "send it", "m1")["message_id"])["code"]
    for n, body in enumerate(("确认" + code, f"确认 {code.lower()}", f"好的 确认 {code}", f"确认  {code}", f"确认 {code}。")):
        assert "confirmation" not in relay(world, body, f"x{n}")
    assert "confirmation" not in relay(world, f"确认 {code}", "w1", channel="wechat", sender="wx-owner")
    assert "confirmation" not in dashboard(world, f"确认 {code}", "o-9")
    assert status_of(world, code) == "pending"
    undated = relay(world, f"确认 {code}", "u1", at=None)
    assert undated["confirmation"] == {"code": code, "action": "confirm", "outcome": "refused",
                                       "refusal": "confirmation_undated"}
    assert status_of(world, code) == "pending"
    assert relay(world, f"确认 {code}", "ok1")["confirmation"]["outcome"] == "confirmed"
    assert "confirmation" not in relay(world, f"确认 {code}", "ok2"), "a second reply is an ordinary message"
    # Cancelled, then confirmed: the confirmation no longer matches.
    second = ask(world, relay(world, "another", "m2")["message_id"])["code"]
    assert relay(world, f"取消 {second}", "c1")["confirmation"]["outcome"] == "cancelled"
    assert "confirmation" not in relay(world, f"确认 {second}", "c2")
    assert status_of(world, second) == "cancelled"
    # Written 11 minutes after it was asked: expired.
    third = ask(world, relay(world, "third", "m3")["message_id"])["code"]
    late = datetime.now(timezone.utc) + timedelta(minutes=11)
    monkeypatch.setattr(sconf, "_utcnow", lambda: late)
    assert relay(world, f"确认 {third}", "late", at=late.isoformat())["confirmation"]["outcome"] == "expired"
    assert status_of(world, third) == "expired"


def test_at_most_three_wait_for_one_person_and_records_go_after_30_days(world):
    mail_ready(world)
    asker = dashboard(world, "go", "o-1")["message_id"]
    for n in range(3):
        ask(world, asker, {"to": f"{n}@example.com"})
    with pytest.raises(StateGraphError, match="too_many_pending"):
        ask(world, asker, {"to": "4@example.com"})
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_confirmations SET created_at='2000-01-01T00:00:00.000000Z'")
        conn.commit()
    ask(world, asker, {"to": "5@example.com"})
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_steward_confirmations").fetchone()[0] == 1


def test_the_gateway_consumes_one_confirmation_per_forward_and_never_retries(world, server, upstream, monkeypatch):
    mail_ready(world)
    monkeypatch.setitem(ADDRESSES, "good.example", "93.184.216.35")
    args = {"to": "a@example.com", "body": "hi"}

    def confirmed(arguments=None):
        code = ask(world, dashboard(world, "send", f"o-{time.time_ns()}")["message_id"], arguments or args)["code"]
        assert dashboard(world, f"确认 {code}", f"c-{time.time_ns()}")["confirmation"]["outcome"] == "confirmed"
        return code

    def call(arguments=None):
        return body_of(rpc(server, world, "tools/call", {"name": "mailpub__send_message", "arguments": arguments or args}))[0]

    assert call()["error"]["data"]["code"] == "confirmation_required"
    code = confirmed()
    assert "result" in call()
    assert upstream.methods.count("tools/call") == 1
    assert call()["error"]["data"]["code"] == "confirmation_required", "one confirmation, one forward"
    assert upstream.methods.count("tools/call") == 1
    assert status_of(world, code) == "done"
    # Key order does not matter (canonical form); one character does.
    confirmed()
    assert "result" in call({"body": "hi", "to": "a@example.com"})
    confirmed()
    assert call({"to": "a@example.com", "body": "hi!"})["error"]["data"]["code"] == "confirmation_required"
    # A new generation (new secret) or a connection deleted and added again: the old one is dead.
    pending = confirmed()
    execute(world.owner, "steward_connection_secret_set", {"conn_id": "mailpub", "value": "Bearer y"}, allow_write=True)
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert status_of(world, pending) == "confirmed"
    gone = confirmed()
    execute(world.owner, "steward_connection_delete", {"conn_id": "mailpub"}, allow_write=True)
    mail_ready(world)
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert status_of(world, gone) == "confirmed"
    # Confirmed more than 10 minutes ago: refused.
    old = confirmed()
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_confirmations SET confirmed_at='2000-01-01T00:00:00.000000Z' WHERE code=?", (old,))
        conn.commit()
    assert call()["error"]["data"]["code"] == "confirmation_required"
    # The upstream fails: recorded failed, never sent again on the same confirmation.
    failing = confirmed()
    upstream.expire_next_call = True
    before = upstream.methods.count("tools/call")
    assert call()["error"]["data"]["code"] == "upstream_unavailable"
    assert upstream.methods.count("tools/call") == before + 1, "an outward call is not retried"
    assert status_of(world, failing) == "failed"
    assert call()["error"]["data"]["code"] == "confirmation_required"


def test_the_owner_lists_the_confirmations_with_their_text(world):
    mail_ready(world)
    asked = ask(world, dashboard(world, "go", "o-1")["message_id"])
    listed_ = world.owner.steward_confirmations.list_recent()["confirmations"]
    assert [c["code"] for c in listed_] == [asked["code"]] and listed_[0]["message_text"] == asked["text"]
    with pytest.raises(StateGraphError, match="owner_session_required"):
        world.steward.steward_confirmations.list_recent()
    with pytest.raises(StateNotFound):
        execute(world.steward, "get_steward_confirmation", {"code": "K0000"}, allow_write=True)
