"""OAuth for the steward's connections (step 24.1b, design-steward 7.8.6).

A connection with `auth_kind = oauth` (postkeep's mail, later Google) is authorized by
the owner on the dashboard, never by the steward:

- DISCOVERY: one MCP request without a token; the 401's `WWW-Authenticate` names the
  protected-resource metadata (RFC 9728), which names the authorization server, whose
  metadata (RFC 8414 / OIDC discovery) names the endpoints. Every address comes from
  someone else's server and is judged here: `https://`, or `http://` only to the very
  host the connection already names; never a refused address (one judgment for the
  connection's own address and every endpoint, core.state_steward_connections
  `address_refused`: the backend, dev_mcp, zvec-grep, the host itself, loopback,
  link-local, multicast - also when carried in an IPv4-mapped, 6to4 or Teredo address -
  and a private one only on the connection's own host), and the address judged is the
  one connected to. The metadata's `issuer` must be the server asked (a trailing
  '/' aside); the resource must be on the connection's origin; S256 PKCE must be
  supported. A server that answers without a token (Google's Workspace servers) is
  read at the well-known protected-resource address instead of a 401's header.
- CLIENT: the owner's own client_id (and secret) - entered for the connection's address
  and bound to the issuer of its first authorization; another address or issuer drops it
  (the owner enters it again) - or one dynamic registration (RFC 7591) at that issuer,
  kept in the connection's secret file. A client is used only with its own issuer.
- SCOPE: the owner's (dashboard), else least privilege: the read-only scopes the server
  lists; a server that lists none is refused until the owner chooses. Google's with
  `openid email` added, for the identity.
- AUTHORIZE: `start` makes a `state` (bound to the owner session that asked, single
  use, 10 minutes) and a PKCE S256 verifier, and answers the authorization URL (with
  `resource`, RFC 8707, and `prompt=login` - Google: `select_account consent` - so a
  browser's existing login is not reused silently). The callback checks `state`, the
  owner session and `iss` (RFC 9207), exchanges the code, keeps
  {access_token, refresh_token, expires_at, ...} in the secret file - never in State -
  and reads the bound identity (the id_token's email or sub, else userinfo).
- IDENTITY: `auth_identity` is shown on the dashboard; the connection can be enabled
  only after the owner confirms it (`identity_confirmed`). A new authorization that
  binds another identity returns the connection to unconfirmed and disabled.
- REFRESH (`ensure_token`, the gateway's httpx.Auth): under 60 s left, or once after an
  upstream 401. A refresh the server refuses marks the connection `needs_consent`:
  out of the effective set, the steward told through its notice line, once.
- REVOKE: deleting a connection or signing out revokes at `revocation_endpoint` when
  the server has one, then deletes the tokens.

Nothing here logs a token, a code, a verifier or a client secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from core import state_steward_connections as sc
from core.state_graph import StateGraphError

log = logging.getLogger(__name__)

CALLBACK_PATH = "/api/steward/connections/oauth/callback"
PENDING_TTL = timedelta(minutes=10)
REFRESH_MARGIN = timedelta(seconds=60)
RESPONSE_MAX = 1024 * 1024
TIMEOUT = 15.0
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "aitelier-steward", "version": "1"}}}
CLIENT_NAME = "AItelier steward"


class OAuthError(StateGraphError):
    """A refusal with a code (the message's first word), shown to the owner as it is."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redirect_uri() -> str:
    """`<the dashboard's public address>/api/steward/connections/oauth/callback`."""
    base = os.environ.get("AITELIER_PUBLIC_BASE_URL", "").strip().rstrip("/")
    parts = urlsplit(base) if base else None
    if not parts or parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise OAuthError("oauth_redirect_unconfigured: set AITELIER_PUBLIC_BASE_URL to the dashboard's https address")
    return base + CALLBACK_PATH


# ---------------------------------------------------------------- HTTP to judged addresses
class _PinnedSync(httpx.HTTPTransport):
    def __init__(self, address: str, **kwargs):
        super().__init__(**kwargs)
        self.address = address

    def handle_request(self, request):
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": request.url.host}
        request.url = request.url.copy_with(host=self.address)
        return super().handle_request(request)


def transport_for(address: str):
    """The transport to one judged address (tests replace this with a fake server)."""
    return _PinnedSync(address)


def judge_endpoint(url: str, conn_url: str) -> str:
    """An address taken from someone else's metadata: https, or http only to the host the
    connection itself names; no user info or fragment; never reserved. Returns the judged
    address to connect to."""
    if not isinstance(url, str) or not url or len(url) > 2000 or any(c.isspace() for c in url):
        raise OAuthError("oauth_endpoint_refused: not a URL")
    try:
        parts = urlsplit(url)
        _port = parts.port
    except ValueError:
        raise OAuthError("oauth_endpoint_refused: not a URL") from None
    host = parts.hostname or ""
    conn = urlsplit(conn_url)
    if (not host or not host.isascii() or parts.username is not None or parts.password is not None
            or parts.fragment or "@" in parts.netloc):
        raise OAuthError("oauth_endpoint_refused: not a plain URL")
    if parts.scheme == "http":
        if conn.scheme != "http" or host != conn.hostname:
            raise OAuthError("oauth_endpoint_refused: http only to the connection's own host")
    elif parts.scheme != "https":
        raise OAuthError("oauth_endpoint_refused: https only")
    found = sc._resolve(host, sc.RESOLVER)
    if not found:
        raise OAuthError("oauth_endpoint_refused: unresolvable host")
    reserved = sc.reserved_addresses()
    # A private address only on the host the connection itself names (judged as the
    # connection's own address); every other endpoint is a public one.
    private_ok = host == conn.hostname
    if any(sc.address_refused(address, reserved, private_ok=private_ok) for address in found):
        raise OAuthError("oauth_endpoint_refused: reserved_endpoint")
    return sc.preferred_address(found)


def _request(method: str, url: str, conn_url: str, **kwargs) -> httpx.Response:
    address = judge_endpoint(url, conn_url)
    body = bytearray()
    try:
        with httpx.Client(transport=transport_for(address), follow_redirects=False, timeout=TIMEOUT) as client:
            with client.stream(method, url, **kwargs) as response:
                # Read no further than RESPONSE_MAX (decoded): a larger answer is refused
                # before the rest of it arrives.
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > RESPONSE_MAX:
                        raise OAuthError("oauth_response_too_large")
    except httpx.HTTPError as exc:
        raise OAuthError(f"oauth_unreachable: {type(exc).__name__}") from None
    # The body as read (already decoded): the headers that described the wire form go.
    headers = [(k, v) for k, v in response.headers.multi_items()
               if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
    return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=response.request)


def _json(response: httpx.Response, code: str) -> dict:
    try:
        value = response.json()
    except ValueError:
        raise OAuthError(f"{code}: not JSON") from None
    if not isinstance(value, dict):
        raise OAuthError(f"{code}: not a JSON object")
    return value


def parse_www_authenticate(value: str) -> dict:
    """The Bearer challenge's parameters (resource_metadata, scope, error)."""
    out = {}
    if not isinstance(value, str):
        return out
    text = value.strip()
    if text[:6].lower() == "bearer":
        text = text[6:]
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in " ,":
            i += 1
        eq = text.find("=", i)
        if eq < 0:
            break
        name = text[i:eq].strip().lower()
        i = eq + 1
        if i < len(text) and text[i] == '"':
            j, buf = i + 1, []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1
                buf.append(text[j])
                j += 1
            out[name], i = "".join(buf), j + 1
        else:
            j = text.find(",", i)
            j = len(text) if j < 0 else j
            out[name], i = text[i:j].strip(), j
    return out


def _origin(url: str) -> tuple:
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))


def _well_known(base: str, name: str) -> list[str]:
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    found = [f"{origin}/.well-known/{name}{path}"] if path else []
    return found + [f"{origin}/.well-known/{name}"]


def discover(conn_url: str) -> dict:
    """{resource, issuer, scope, metadata} for one connection address (see the module)."""
    challenge = _request("POST", conn_url, conn_url, json=INITIALIZE,
                         headers={"Accept": "application/json, text/event-stream"})
    if challenge.status_code == 401:
        params = parse_www_authenticate(challenge.headers.get("www-authenticate", ""))
    elif 200 <= challenge.status_code < 300:
        # A server that answers `initialize` without a token (Google's Workspace servers
        # want one only for the calls): its metadata is at the well-known address.
        params = {}
    else:
        raise OAuthError(f"oauth_discovery_failed: the server answered {challenge.status_code}, not 401")
    candidates = [params["resource_metadata"]] if params.get("resource_metadata") else _well_known(
        conn_url, "oauth-protected-resource")
    prm = None
    for url in candidates:
        response = _request("GET", url, conn_url)
        if response.status_code == 200:
            prm = _json(response, "oauth_discovery_failed")
            break
    if prm is None:
        raise OAuthError("oauth_discovery_failed: no protected-resource metadata")
    resource = prm.get("resource")
    if not isinstance(resource, str) or _origin(resource) != _origin(conn_url):
        raise OAuthError("oauth_resource_mismatch: the resource is not the connection's address")
    servers = prm.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
        raise OAuthError("oauth_discovery_failed: no authorization server")
    metadata = authorization_server(servers[0], conn_url)
    scope = params.get("scope") or " ".join(s for s in (prm.get("scopes_supported") or []) if isinstance(s, str))
    # The issuer as the server's own metadata spells it (the `iss` the callback carries).
    return {"resource": resource, "issuer": metadata["issuer"], "scope": scope, "metadata": metadata}


def _same_issuer(named, issuer: str) -> bool:
    """The metadata's issuer is the one asked; a trailing '/' alone does not differ
    (Google's resources name `https://accounts.google.com/`, its metadata
    `https://accounts.google.com`)."""
    return isinstance(named, str) and named.rstrip("/") == issuer.rstrip("/")


def authorization_server(issuer: str, conn_url: str) -> dict:
    judge_endpoint(issuer, conn_url)
    if urlsplit(issuer).query:
        raise OAuthError("oauth_endpoint_refused: an issuer has no query")
    # RFC 8414 (path inserted after /.well-known/), OIDC Discovery 1.0 (path appended).
    candidates = (_well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration")
                  + [issuer.rstrip("/") + "/.well-known/openid-configuration"])
    mismatch = False
    for url in dict.fromkeys(candidates):
        response = _request("GET", url, conn_url)
        if response.status_code != 200:
            continue
        metadata = _json(response, "oauth_discovery_failed")
        if not _same_issuer(metadata.get("issuer"), issuer):
            # Another issuer's document (an origin-root one for a path issuer): try the next.
            mismatch = True
            continue
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(metadata.get(key), str):
                raise OAuthError(f"oauth_discovery_failed: no {key}")
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint",
                    "userinfo_endpoint"):
            if metadata.get(key) is not None:
                judge_endpoint(metadata[key], conn_url)
        if "S256" not in (metadata.get("code_challenge_methods_supported") or []):
            raise OAuthError("oauth_pkce_unsupported: the server does not offer S256 PKCE")
        return metadata
    if mismatch:
        raise OAuthError("oauth_issuer_mismatch: the metadata names another issuer")
    raise OAuthError("oauth_discovery_failed: no authorization server metadata")


# ---------------------------------------------------------------- the secret file
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(conn_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(conn_id, threading.Lock())


def _secret(conn_id: str) -> dict:
    return sc.read_secret(conn_id) or {}


def set_client(conn_id: str, client_id: str | None, client_secret: str | None, conn_url: str) -> None:
    """The owner's own registered client (Google) for the connection's address `conn_url`
    (bound to an issuer at its first authorization); empty clears it (dynamic
    registration then)."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        if client_id:
            secret["client"] = {"client_id": client_id, **({"client_secret": client_secret} if client_secret else {}),
                                "auth_method": "client_secret_basic" if client_secret else "none",
                                "owner_supplied": True, "conn_url": conn_url}
        else:
            secret.pop("client", None)
        sc.write_secret(conn_id, secret)


def set_scope(conn_id: str, scope: str | None) -> None:
    """The scopes the owner asks for (space-separated); empty: the read-only ones the server lists."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        if scope:
            secret["scope"] = scope
        else:
            secret.pop("scope", None)
        sc.write_secret(conn_id, secret)


def scope_of(conn_id: str) -> str | None:
    """The owner's scopes for this connection, if any (not a secret: shown on the dashboard)."""
    scope = _secret(conn_id).get("scope")
    return scope if isinstance(scope, str) and scope else None


GOOGLE_ISSUER_HOST = "accounts.google.com"
# Google issues an id_token with the account's email only for these; without them the
# bound identity is unknown and the connection can never be enabled (7.8.6).
IDENTITY_SCOPES = ("openid", "email")


def read_only_scope(scope: str) -> bool:
    """A scope that names read access alone: `.../drive.readonly`, `documents.readonly`,
    `Mail.Read` (not `Mail.ReadWrite`)."""
    last = scope.rsplit("/", 1)[-1].lower()
    return last.endswith("readonly") or last.endswith(".read")


def requested_scope(conn_id: str, issuer: str, listed: str) -> str:
    """The scope of an authorization request: the owner's (dashboard); else least
    privilege - the read-only scopes the server listed. A server that lists none is not
    guessed for: the owner chooses. For Google with `openid email` added."""
    words = (scope_of(conn_id) or "").split()
    if not words:
        words = [w for w in (listed or "").split() if read_only_scope(w)]
        if not words:
            raise OAuthError("oauth_scope_required: the server lists no read-only scope - choose the scopes "
                             "on the dashboard")
    if urlsplit(issuer).hostname == GOOGLE_ISSUER_HOST:
        words += [w for w in IDENTITY_SCOPES if w not in words]
    return " ".join(dict.fromkeys(words))


def _client_for(conn_id: str, conn_url: str, issuer: str, metadata: dict, redirect: str) -> dict:
    """The client of an authorization at `issuer`. The owner's: only for the address it was
    entered for and the issuer it was first used with (bound now if new) - another one
    drops it, and the owner enters it again. Else the one registered at this issuer, else
    a new registration."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        client = secret.get("client")
        if isinstance(client, dict) and client.get("owner_supplied"):
            if client.get("conn_url") != conn_url or client.get("issuer", issuer) != issuer:
                secret.pop("client")
                sc.write_secret(conn_id, secret)
                raise OAuthError("oauth_client_mismatch: the client was entered for another address or issuer - "
                                 "enter it again")
            if client.get("issuer") != issuer:
                client["issuer"] = issuer
                sc.write_secret(conn_id, secret)
            return client
        if isinstance(client, dict) and client.get("client_id") and client.get("issuer") == issuer:
            return client
    endpoint = metadata.get("registration_endpoint")
    if not endpoint:
        raise OAuthError("oauth_client_required: the server has no dynamic registration - enter a client_id")
    response = _request("POST", endpoint, conn_url, json={
        "client_name": CLIENT_NAME, "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    if response.status_code not in (200, 201):
        raise OAuthError(f"oauth_registration_failed: {response.status_code}")
    answer = _json(response, "oauth_registration_failed")
    if not isinstance(answer.get("client_id"), str) or not answer["client_id"]:
        raise OAuthError("oauth_registration_failed: no client_id")
    client = {"client_id": answer["client_id"], "issuer": issuer,
              "auth_method": answer.get("token_endpoint_auth_method") or ("client_secret_basic"
                                                                         if answer.get("client_secret") else "none")}
    if isinstance(answer.get("client_secret"), str):
        client["client_secret"] = answer["client_secret"]
    with _lock(conn_id):
        secret = _secret(conn_id)
        if (secret.get("client") or {}).get("owner_supplied"):
            raise OAuthError("oauth_client_changed: the owner entered a client meanwhile - start again")
        secret["client"] = client
        sc.write_secret(conn_id, secret)
    return client


def _client_of(secret: dict, issuer) -> dict:
    """The connection's client if it belongs to `issuer`; else none (no credentials sent)."""
    client = secret.get("client")
    return client if isinstance(client, dict) and client.get("issuer") == issuer else {"client_id": ""}


def _client_auth(client: dict, form: dict) -> dict:
    """Client authentication for the token and revocation endpoints: headers (basic) or
    form fields (post / none)."""
    if client.get("client_secret") and client.get("auth_method") != "client_secret_post":
        raw = f"{client['client_id']}:{client['client_secret']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    form["client_id"] = client["client_id"]
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    return {}


def _pkce() -> tuple[str, str]:
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------- authorize
def start(row, actor: str) -> dict:
    """The authorization URL for one oauth connection, asked by the owner session `actor`."""
    if row["auth_kind"] != "oauth":
        raise OAuthError("oauth_not_applicable: not an oauth connection")
    redirect = redirect_uri()
    try:
        sc.checked_address(row["url"])
    except sc.ProbeFailed as exc:
        raise OAuthError(f"oauth_unreachable: {exc.code}") from None
    found = discover(row["url"])
    metadata = found["metadata"]
    scope = requested_scope(row["conn_id"], found["issuer"], found["scope"])
    client = _client_for(row["conn_id"], row["url"], found["issuer"], metadata, redirect)
    verifier, challenge = _pkce()
    state = f"{row['conn_id']}.{_secrets.token_urlsafe(32)}"
    with _lock(row["conn_id"]):
        secret = _secret(row["conn_id"])
        secret["pending"] = {"state_sha256": _sha(state), "verifier": verifier, "actor_sha256": _sha(actor),
                             "created_at": _iso(_now()), "issuer": found["issuer"], "resource": found["resource"],
                             "redirect_uri": redirect, "metadata": {k: metadata.get(k) for k in (
                                 "token_endpoint", "revocation_endpoint", "userinfo_endpoint",
                                 "authorization_response_iss_parameter_supported")}}
        sc.write_secret(row["conn_id"], secret)
    query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
             "resource": found["resource"], "scope": scope}
    if urlsplit(found["issuer"]).hostname == GOOGLE_ISSUER_HOST:
        query.update(prompt="select_account consent", access_type="offline")
    else:
        query["prompt"] = "login"
    endpoint = metadata["authorization_endpoint"]
    return {"conn_id": row["conn_id"], "authorize_url": endpoint + ("&" if "?" in endpoint else "?") + urlencode(query)}


def conn_id_of_state(state) -> str | None:
    if not isinstance(state, str) or "." not in state or len(state) > 200:
        return None
    conn_id = state.split(".", 1)[0]
    return conn_id if sc.CONN_ID.match(conn_id) else None


def take_pending(conn_id: str, state: str, actor: str) -> dict:
    """The pending authorization this `state` names - removed at once (single use), and
    only for the owner session that started it, within 10 minutes."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        pending = secret.get("pending")
        if not isinstance(pending, dict) or not hmac.compare_digest(pending.get("state_sha256", ""), _sha(state)):
            raise OAuthError("oauth_state_unknown: start the authorization again")
        secret.pop("pending", None)
        sc.write_secret(conn_id, secret)
    if not hmac.compare_digest(pending.get("actor_sha256", ""), _sha(actor)):
        raise OAuthError("oauth_state_unknown: started by another session")
    if _now() - datetime.fromisoformat(pending["created_at"].replace("Z", "+00:00")) > PENDING_TTL:
        raise OAuthError("oauth_state_expired: start the authorization again")
    return pending


def _claims(id_token) -> dict:
    """The id_token's payload. It came straight from the token endpoint over the judged
    connection (OIDC Core 3.1.3.7: TLS server validation instead of the signature)."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


def _identity(conn_url: str, tokens: dict, metadata: dict) -> str | None:
    claims = _claims(tokens.get("id_token")) if tokens.get("id_token") else {}
    for key in ("email", "preferred_username", "sub"):
        if isinstance(claims.get(key), str) and claims[key]:
            return claims[key][:200]
    endpoint = metadata.get("userinfo_endpoint")
    if endpoint:
        response = _request("GET", endpoint, conn_url, headers={"Authorization": "Bearer " + tokens["access_token"]})
        if response.status_code == 200:
            info = _json(response, "oauth_userinfo_failed")
            for key in ("email", "preferred_username", "sub"):
                if isinstance(info.get(key), str) and info[key]:
                    return info[key][:200]
    return None


def _tokens_of(answer: dict, previous: dict | None = None) -> dict:
    if not isinstance(answer.get("access_token"), str) or not answer["access_token"]:
        raise OAuthError("oauth_token_failed: no access_token")
    if str(answer.get("token_type", "bearer")).lower() != "bearer":
        raise OAuthError("oauth_token_failed: not a bearer token")
    lifetime = answer.get("expires_in")
    expires = _now() + timedelta(seconds=int(lifetime)) if isinstance(lifetime, (int, float)) and lifetime > 0 else None
    refresh = answer.get("refresh_token") if isinstance(answer.get("refresh_token"), str) else (previous or {}).get(
        "refresh_token")
    out = {"access_token": answer["access_token"], "expires_at": _iso(expires) if expires else None}
    if refresh:
        out["refresh_token"] = refresh
    if isinstance(answer.get("id_token"), str):
        out["id_token"] = answer["id_token"]
    return out


def complete(row, pending: dict, code: str, iss: str | None) -> dict:
    """Exchange the code and read the identity. Returns {identity, tokens}: the tokens are
    NOT installed here - the caller first records what the identity means for the
    connection (unconfirmed, disabled), then installs them (`install_tokens`), so no call
    already judged goes out under a newly bound account."""
    issuer = pending["issuer"]
    if iss is not None and iss != issuer:
        raise OAuthError("oauth_issuer_mismatch: the answer names another issuer")
    if iss is None and pending["metadata"].get("authorization_response_iss_parameter_supported"):
        raise OAuthError("oauth_issuer_mismatch: the answer names no issuer")
    if not isinstance(code, str) or not code or len(code) > 2000:
        raise OAuthError("oauth_code_missing")
    secret = _secret(row["conn_id"])
    client = secret.get("client") or {}
    if not client.get("client_id"):
        raise OAuthError("oauth_client_required")
    if client.get("issuer") != issuer or (client.get("owner_supplied") and client.get("conn_url") != row["url"]):
        # Another client than the one the authorization started with: its secret is
        # never sent to this issuer.
        raise OAuthError("oauth_client_mismatch: the client changed since the authorization started - start again")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"], "resource": pending["resource"]}
    headers = _client_auth(client, form)
    endpoint = pending["metadata"]["token_endpoint"]
    response = _request("POST", endpoint, row["url"], data=form, headers=headers)
    if response.status_code != 200:
        raise OAuthError(f"oauth_token_failed: {response.status_code}")
    tokens = _tokens_of(_json(response, "oauth_token_failed"))
    identity = _identity(row["url"], tokens, pending["metadata"])
    return {"identity": identity,
            "tokens": {**tokens, "token_endpoint": endpoint, "issuer": issuer, "resource": pending["resource"],
                       "revocation_endpoint": pending["metadata"].get("revocation_endpoint")}}


def install_tokens(conn_id: str, tokens: dict, conn_url: str) -> None:
    """The tokens of a finished authorization, with the address they were issued for, in
    one write under the connection's lock (a concurrent refresh is never overwritten)."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        secret["tokens"] = {**tokens, "conn_url": conn_url}
        secret["needs_consent"] = False
        secret.pop("consent_noticed", None)
        sc.write_secret(conn_id, secret)


# ---------------------------------------------------------------- refresh
class RefreshRefused(Exception):
    """The server refused the refresh: the connection needs the owner's consent again."""


def token_status(conn_id: str) -> str:
    """set / oauth_needs_consent (never a token)."""
    secret = _secret(conn_id)
    tokens = secret.get("tokens")
    if secret.get("needs_consent") or not isinstance(tokens, dict) or not tokens.get("access_token"):
        return "oauth_needs_consent"
    return "set"


def ensure_token(conn_id: str, conn_url: str, *, stale: str | None = None, store=None) -> str:
    """The access token to send now: refreshed when under a minute is left, or when the
    upstream refused `stale` (once - a token another call already replaced is reused)."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        tokens = secret.get("tokens") or {}
        if secret.get("needs_consent") or not tokens.get("access_token"):
            raise RefreshRefused("oauth_needs_consent")
        expires = tokens.get("expires_at")
        due = bool(expires) and datetime.fromisoformat(expires.replace("Z", "+00:00")) - _now() < REFRESH_MARGIN
        if stale is not None and stale != tokens["access_token"]:
            return tokens["access_token"]
        if not due and stale is None:
            return tokens["access_token"]
        client = secret.get("client")
        if not tokens.get("refresh_token") or not isinstance(client, dict) or client.get("issuer") != tokens.get("issuer"):
            # No refresh token, or the client is not the one these tokens were issued to.
            _refused(conn_id, secret, store)
        form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        if tokens.get("resource"):
            form["resource"] = tokens["resource"]
        headers = _client_auth(client, form)
        response = _request("POST", tokens["token_endpoint"], conn_url, data=form, headers=headers)
        if response.status_code in (400, 401):
            _refused(conn_id, secret, store)
        if response.status_code != 200:
            raise OAuthError(f"oauth_refresh_failed: {response.status_code}")
        fresh = _tokens_of(_json(response, "oauth_refresh_failed"), tokens)
        secret["tokens"] = {**tokens, **fresh}
        sc.write_secret(conn_id, secret)
        return fresh["access_token"]


def _refused(conn_id: str, secret: dict, store) -> None:
    """Mark the connection needs_consent (out of the effective set), tell the steward once."""
    first = not secret.get("needs_consent")
    secret["needs_consent"] = True
    sc.write_secret(conn_id, secret)
    log.warning("steward connection %s: the authorization server refused a refresh; it needs consent", conn_id)
    if first and not secret.get("consent_noticed") and store is not None:
        secret["consent_noticed"] = True
        sc.write_secret(conn_id, secret)
        notice_consent(store, conn_id)
    sc.tools_changed()
    raise RefreshRefused("oauth_needs_consent")


CONSENT_NOTICE = ("连接「{label}」的授权已失效，需要业主在面板「连接」页重新授权。"
                  " (The connection {conn_id} needs to be authorized again on the dashboard connections page.)")


def notice_consent(store, conn_id: str) -> None:
    """One note into the steward's own inbox (it passes it on); the dashboard shows the state."""
    try:
        from core.driver_inbox import deliver_notice
        from core.state_steward import steward_project
        with store.transaction(write=True) as conn:
            row = conn.execute("SELECT label FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            driver = conn.execute("SELECT driver_id FROM dsh_drivers WHERE project_id=?", (steward_project(),)).fetchone()
            if row is None or driver is None:
                return
            deliver_notice(conn, {"target_driver_id": driver[0], "sender_driver_id": None,
                                  "project_id": steward_project(), "kind": "note",
                                  "subject": "A connection needs authorization",
                                  "body": CONSENT_NOTICE.format(label=row["label"], conn_id=conn_id), "refs": {}})
    except Exception:  # noqa: BLE001 - the dashboard still shows it
        log.warning("steward connection %s: the consent notice was not delivered", conn_id)


# ---------------------------------------------------------------- revoke
def revoke(conn_id: str, conn_url: str, secret: dict | None = None) -> bool:
    """Revoke the refresh and access tokens where the server offers it (best effort)."""
    secret = secret if secret is not None else _secret(conn_id)
    tokens = secret.get("tokens") or {}
    endpoint = tokens.get("revocation_endpoint")
    if not endpoint or not conn_url:
        return False
    done = False
    for kind in ("refresh_token", "access_token"):
        if not tokens.get(kind):
            continue
        form = {"token": tokens[kind], "token_type_hint": kind}
        headers = _client_auth(_client_of(secret, tokens.get("issuer")), form)
        try:
            response = _request("POST", endpoint, conn_url, data=form, headers=headers)
            done = done or response.status_code == 200
        except StateGraphError as exc:
            log.warning("steward connection %s: revocation failed (%s)", conn_id, str(exc).split(":")[0])
    return done


def drop_tokens(conn_id: str, *, client: bool = False) -> None:
    """Forget the tokens (and a pending authorization); the client registration stays
    unless `client` (the connection's address changed: the owner enters it again)."""
    with _lock(conn_id):
        secret = _secret(conn_id)
        for key in ("tokens", "pending", "needs_consent", "consent_noticed") + (("client",) if client else ()):
            secret.pop(key, None)
        sc.write_secret(conn_id, secret)
