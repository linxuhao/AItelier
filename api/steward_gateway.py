"""The steward's one MCP address: the backend's aggregating gateway (step 24.1a,
design-steward 7.8.5).

To the steward's process it is an MCP server (`/api/steward/mcp`, streamable HTTP:
POST, GET, DELETE) - a small hand-written JSON-RPC (initialize, notifications, ping,
tools/list, tools/call) and a session-keyed GET stream on which
`notifications/tools/list_changed` goes out whenever the effective tool set changes,
so a newly allowed tool needs no steward restart. To each registered connection it is
an MCP client: ONE long-lived upstream session per connection (the `mcp` package),
reopened only when the connection's address, auth or generation changes.

Where it answers (director 487, option B): only on its own listener,
`AITELIER_STEWARD_GATEWAY_PORT` (4445), a second uvicorn server in this process that
docker-compose.yml publishes on the host's loopback alone; the backend's 4444 answers
404 for it, and the 4445 listener serves nothing else (`PortGuard`, by
`scope["server"]` port).

Who may enter is checked here once per HTTP request (POST, GET, DELETE), before any
JSON-RPC in it is read, whatever the write gate says: a loopback Host (and Origin,
when sent), a steward driver's token, not via Cloudflare. A JSON-RPC batch is
authorized once, for all its messages; each `tools/call` in it is still judged alone.
The peer address is logged, never judged. Refusals are logged at most once a minute
per source. The token is never sent upstream; arguments, results and secrets are
never logged.

`tools/list` never reaches upstream: it is the effective set from the database
(core.state_steward_connections.effective), byte for byte the definitions the owner
allowed. It is not filtered by the turn audience: a session lists once and lives
across turns, and a per-audience list would make every audience switch a list change.
The turn gates are the steward plugin's per-turn catalog and, here, the audience check
of `tools/call`, which reads step 23's turn audience record (`set_steward_turn_audience`,
core.state_groups; core.state_steward_connections.audience_of). A request here carries
no turn (design-steward 7.7.8a): the check relies on the steward's host recording each
turn's audience before that turn's first step runs (a failed record rejects the step),
and no record reads as not the owner's (fail closed). `tools/call` is judged in order -
connection usable, tool allowed and unchanged, private connection only in an `owner`
turn, no blocked argument (at any depth; on a public connection also no `url` / `*_url`
key anywhere in the arguments unless the owner unblocked that path), daily cap, an
outward tool consumes one confirmation its requester gave for exactly these arguments and
that run_confirmed armed in its own turn (core.state_steward_confirmations, 24.1b; the
gateway itself judges no turn, director 602 claim 7) - and, in the same write transaction, a
pending ledger row is reserved (so two concurrent calls never both pass the last unit
of a cap) and the confirmation consumed; then forwarded, and the row completed. An
outward call is forwarded once and never retried. When an owner turn's call consumed a
record a family group confirmed (director 581 lets it), the answer carries, first, a line
naming that group and who confirmed it - on a result, a tool error and a JSON-RPC error alike.

An oauth connection's upstream client carries `OAuthBearer` (core.steward_oauth): the
token is refreshed under a minute before expiry, and once after an upstream 401, without
reopening the session.

`GET /api/steward/mcp/{conn_id}/fetch?url=...` (step 24.2, design-steward 7.8.12 "结果里的
文件") hands the steward's host the file a connection's tool result named - agentmcp's
download addresses carry the gateway's own Host (`http://mcp_server:9003/files/...`), which
the host cannot resolve. Same entry checks as above, and then: the url must have appeared
VERBATIM in a result this gateway forwarded for that connection during the turn recorded now
(the host records the same (conn_id, url) and passes the conn_id; here the gateway's own
record is checked too, so a url the model invents or carries over from another turn or
connection is refused `url_not_seen`); a private connection only in an owner turn; the url's
origin (scheme, host, port) is the connection's own and a redirect to another origin is
refused; no upstream credential is sent (no header, no oauth token); at most the table's
outbound_bytes (25 MiB); the type is the first bytes' signature in the shared table
(core.media_types), never the upstream's Content-Type.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import re
import sqlite3
import uuid
from collections import OrderedDict
from contextlib import contextmanager, suppress
from urllib.parse import urljoin, urlsplit

import httpx
import uvicorn
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketClose

from api import authz
from core import media_types
from core import state_steward_capabilities as caps
from core import state_steward_confirmations as sconf
from core import state_steward_connections as sc
from core import steward_oauth
from core.state_graph import StateGraphError, StateGraphStore

logger = logging.getLogger(__name__)
# The mcp client logs every message it sends and receives at DEBUG - arguments and
# results included. The gateway's logs never carry those (7.8.5), whatever the root level.
logging.getLogger("mcp.client").setLevel(logging.INFO)
router = APIRouter(tags=["Steward gateway"])

PATH = "/api/steward/mcp"
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "aitelier-steward-gateway", "version": "1"}
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
# The one bound, shared with the steward host's media_call (media-types.json limits.call_body_bytes,
# 40 MiB): it refuses a call whose base64 would not fit before sending it.
MAX_BODY = media_types.limits()["call_body_bytes"]
STREAMS_PER_DRIVER = 4
SERVED_KEPT = 64  # sessions whose last listed hash is remembered (a killed client sends no DELETE)
KEEPALIVE_SECONDS = 15.0
RETRY_MS = 30000
COALESCE_SECONDS = 1.0
CHECK_SECONDS = 30.0
UPSTREAM_TIMEOUT = 170.0  # under the steward entry's toolCallTimeoutMs (180 s)
OPEN_TIMEOUT = 30.0


def _error(mid, code, text):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"{code}: {text}", "data": {"code": code}}}


# The steward's MCP client hands its host a JSON-RPC error and an upstream's isError result alike,
# as one message: the error's `<code>: <text>`, or the result's text. An error the gateway writes
# (`_error`) carries no upstream byte, so the steward's turn is not marked by it (design-steward
# 7.8.8; integrations/dsh-director/lib/catalog.mjs `gatewayError` keys on those codes). So that no
# upstream can pass for one, every upstream isError result is forwarded with this line first.
UPSTREAM_ERROR_LINE = "The connection's tool answered with an error:"


# ------------------------------------------------------------------ where it answers
PORT_ENV = "AITELIER_STEWARD_GATEWAY_PORT"
DEFAULT_PORT = 4445
LISTEN_HOST = "0.0.0.0"  # inside the container; the host publishes it on 127.0.0.1 only


def gateway_port() -> int:
    """The port the gateway alone is served on (a malformed setting falls back to the
    default rather than break every request through `PortGuard`)."""
    raw = os.environ.get(PORT_ENV, "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def _is_gateway_path(path: str) -> bool:
    return path == PATH or path.startswith(PATH + "/")


class PortGuard:
    """The gateway's listener serves the gateway alone, and no other listener serves
    the gateway: the outermost middleware, so nothing else sees such a request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            server = scope.get("server")
            on_gateway_port = bool(server) and server[1] == gateway_port()
            if on_gateway_port != _is_gateway_path(scope.get("path", "")):
                if scope["type"] == "websocket":
                    await WebSocketClose()(scope, receive, send)
                else:
                    await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _Listener(uvicorn.Server):
    """A second server in the backend's process: the main server owns the signals."""

    @contextmanager
    def capture_signals(self):
        yield


async def start_listener(app):
    """With `AITELIER_STEWARD_GATEWAY_PORT` set (docker-compose.yml), serve `app` on
    0.0.0.0:<port> too, until `stop_listener`. Unset (tests, `aitelier server
    --no-docker`): no listener. A port that cannot be bound is logged, never fatal."""
    if not os.environ.get(PORT_ENV, "").strip():
        return None
    port = gateway_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((LISTEN_HOST, port))
    except OSError:
        sock.close()
        logger.exception("steward gateway: cannot listen on %s:%d - the steward has no gateway", LISTEN_HOST, port)
        return None
    server = _Listener(uvicorn.Config(app, lifespan="off", log_config=None, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started and not task.done():
        await asyncio.sleep(0.02)
    if server.started:
        logger.info("steward gateway: listening on %s:%d", LISTEN_HOST, port)
    else:
        logger.error("steward gateway: the listener on %s:%d did not start", LISTEN_HOST, port)
    return server, task


async def stop_listener(app, listener):
    if listener is None:
        return
    server, task = listener
    found = getattr(app.state, "steward_gateway", None)
    for stream in list(found.streams.values()) if found is not None else []:
        stream.queue.put_nowait(("close", None))  # open GET streams end now, not at the drain timeout
    server.should_exit = True
    with suppress(Exception):
        await asyncio.wait_for(task, 10)


# ------------------------------------------------------------------ who may enter
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
REFUSAL_LOG_SECONDS = 60.0
_refusals_logged: dict[str, float] = {}  # source -> when its last refusal was logged


def _loopback_hosts() -> set[str]:
    port = gateway_port()
    return {name for host in LOOPBACK_HOSTS for name in (host, f"{host}:{port}")}


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def steward_driver(request: Request) -> dict | None:
    """The steward driver behind this request's token, or None."""
    token = request.headers.get(authz.DRIVER_TOKEN_HEADER, "").strip()
    if not token or authz.is_via_cloudflare(request):
        return None
    registry = authz.driver_registry()
    if registry is None:
        return None
    row = registry.lookup_token(token)
    capabilities = (row or {}).get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("role") != "steward":
        return None
    return row


def admit(request: Request):
    """(the steward driver, None), or (None, the 403). A Host or Origin naming
    anything but loopback is refused before the token is looked at (a browser page
    or a container that dialled the port by another name)."""
    allowed = _loopback_hosts()
    origin = request.headers.get("origin")
    if request.headers.get("host", "").lower() not in allowed:
        reason = "host"
    elif origin is not None and origin.lower() not in {"http://" + name for name in allowed}:
        reason = "origin"
    else:
        driver = steward_driver(request)
        if driver is not None:
            return driver, None
        reason = "token"
    _log_refusal(_peer(request), reason)
    return None, _forbidden()


def _log_refusal(source: str, reason: str):
    """At most one line per source a minute."""
    now = time.monotonic()
    last = _refusals_logged.get(source)
    if last is not None and now - last < REFUSAL_LOG_SECONDS:
        return
    if len(_refusals_logged) >= 1024:
        _refusals_logged.clear()
    _refusals_logged[source] = now
    logger.warning("steward gateway: refused a request from %s (%s)", source, reason)


def _forbidden():
    return JSONResponse({"detail": "steward_gateway_forbidden"}, status_code=403)


# ------------------------------------------------------------------ the database
def _db(app):
    from api.dependencies import get_db_manager
    return app.dependency_overrides.get(get_db_manager, get_db_manager)()


def _store(app) -> StateGraphStore:
    db = _db(app)
    if getattr(app.state, "_steward_gateway_schema", None) is not db:
        sc.ensure_schema(db)
        app.state._steward_gateway_schema = db
    return StateGraphStore(db, project_read_trusted=True)


def _read(app, fn):
    with _store(app).transaction() as conn:
        return fn(conn)


# ------------------------------------------------------------------ upstream
class PinnedTransport(httpx.AsyncHTTPTransport):
    """Connects to the address `checked_address` judged, never a second lookup (DNS
    rebinding); the Host header and the TLS server name stay the connection's host."""

    def __init__(self, address: str, **kwargs):
        super().__init__(**kwargs)
        self.address = address

    async def handle_async_request(self, request):
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": request.url.host}
        request.url = request.url.copy_with(host=self.address)
        return await super().handle_async_request(request)


def transport_for(address: str):
    """The upstream transport for one judged address (tests replace this)."""
    return PinnedTransport(address)


class OAuthBearer(httpx.Auth):
    """The connection's access token on every upstream request: refreshed when under a
    minute is left, and once after a 401 (then the request is sent once more)."""

    def __init__(self, conn_id: str, url: str, store):
        self.conn_id, self.url, self.store = conn_id, url, store

    async def async_auth_flow(self, request):
        token = await asyncio.to_thread(steward_oauth.ensure_token, self.conn_id, self.url, store=self.store)
        request.headers["Authorization"] = "Bearer " + token
        response = yield request
        if response.status_code == 401:
            token = await asyncio.to_thread(steward_oauth.ensure_token, self.conn_id, self.url, stale=token,
                                            store=self.store)
            request.headers["Authorization"] = "Bearer " + token
            yield request


class _Refused(Exception):
    """A refusal found after the judgement wrote: raised to roll its transaction back."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SessionBroken(Exception):
    """The upstream session is gone (closed stream, 404): reopen once for a non-outward tool."""


class Upstream:
    """One connection's long-lived client session, held open by its own task."""

    def __init__(self, gateway, row: dict):
        self.gateway, self.conn_id = gateway, row["conn_id"]
        self.fingerprint = fingerprint(row)
        self.row = row
        self.session = None
        self.instructions = None
        self.error: str | None = None
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def open(self):
        # Step 24.4c: dev_mcp's address only for the private `dev_mcp` row (7.8.24 "九" 5).
        address = await asyncio.to_thread(lambda: sc.checked_address(
            self.row["url"], dev_mcp_ok=sc.dev_mcp_private(self.row)))
        headers, auth = {}, None
        if self.row["auth_kind"] == "header":
            secret = await asyncio.to_thread(sc.read_secret, self.conn_id) or {}
            headers[self.row["header_name"] or "Authorization"] = secret.get("header_value", "")
        elif self.row["auth_kind"] == "oauth":
            auth = OAuthBearer(self.conn_id, self.row["url"], _store(self.gateway.app))
        self.task = asyncio.create_task(self._run(address, headers, auth))
        try:
            await asyncio.wait_for(self.ready.wait(), OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            self.error = "upstream_timeout"
        if self.session is None:
            await self.close()
            raise sc.ProbeFailed(self.error or "upstream_unavailable")

    async def _run(self, address, headers, auth=None):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        try:
            async with httpx.AsyncClient(headers=headers, auth=auth, transport=transport_for(address),
                                         follow_redirects=False,
                                         timeout=httpx.Timeout(30.0, read=UPSTREAM_TIMEOUT)) as client:
                async with streamable_http_client(self.row["url"], http_client=client) as (read, write, _sid):
                    async with ClientSession(read, write, message_handler=self._message) as session:
                        result = await session.initialize()
                        # Step 24.4c: the server's own name must fit the row - dev-mcp only behind
                        # the private `dev_mcp` row, nothing else there (a tunnel, an alias).
                        server = getattr(getattr(result, "serverInfo", None), "name", None)
                        if not sc.dev_mcp_server_ok(self.row, server):
                            self.error = "reserved_endpoint"
                            logger.info("steward gateway: %s refused: its server name does not fit (dev-mcp)",
                                        self.conn_id)
                            return
                        self.instructions = getattr(result, "instructions", None)
                        self.session = session
                        self.ready.set()
                        await self.closing.wait()
        except BaseException as exc:  # noqa: BLE001 - the session is simply gone
            if _refused_refresh(exc):
                self.error = self.error or "connection_unavailable"
            elif not isinstance(exc, asyncio.CancelledError):
                self.error = self.error or ("upstream_unavailable:" + type(exc).__name__)
        finally:
            self.session = None
            self.ready.set()

    async def _message(self, message):
        from mcp import types
        root = getattr(message, "root", None)
        if isinstance(root, types.ToolListChangedNotification):
            self.gateway.spawn(self.gateway.relist(self.conn_id))

    @property
    def alive(self) -> bool:
        return self.session is not None and self.task is not None and not self.task.done()

    async def close(self):
        self.closing.set()
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self.task.cancel()
                with suppress(BaseException):
                    await self.task


async def _call_tool(session, raw_name: str, arguments: dict):
    """tools/call without the client's own output-schema check: the steward is never given
    an outputSchema (7.8.5), so a result is passed on as the upstream answered it - a text
    answer from a tool that declares one is not turned into a broken session."""
    from mcp import types
    request = types.ClientRequest(types.CallToolRequest(params=types.CallToolRequestParams(name=raw_name,
                                                                                           arguments=arguments)))
    return await session.send_request(request, types.CallToolResult)


def _session_lost(exc: BaseException) -> bool:
    """The upstream session is gone, as the mcp client reports it: a 404 becomes
    ErrorData(32600, "Session terminated"), a reader that ended becomes
    ErrorData(CONNECTION_CLOSED, "Connection closed"); a send on a closed stream raises
    anyio's closed/broken-resource errors; httpx raises a TransportError."""
    import anyio
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED
    if isinstance(exc, McpError):
        return (exc.error.code, exc.error.message) in ((32600, "Session terminated"),
                                                       (CONNECTION_CLOSED, "Connection closed"))
    return isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream,
                            httpx.TransportError))


def _refused_refresh(exc: BaseException) -> bool:
    """A refused OAuth refresh, also when the mcp client's task group wraps it."""
    if isinstance(exc, steward_oauth.RefreshRefused):
        return True
    return any(_refused_refresh(e) for e in getattr(exc, "exceptions", ()) or ())


def fingerprint(row) -> tuple:
    return (row["url"], row["auth_kind"], row["header_name"], row["generation"])


# ------------------------------------------------------------------ streams and notices
class Stream:
    def __init__(self, driver_id: str, session_id: str):
        self.driver_id, self.session_id = driver_id, session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.notified: str | None = None  # the effective hash this stream was last told about


class Gateway:
    def __init__(self, app):
        self.app = app
        self.streams: dict[str, Stream] = {}
        self.served: dict[str, str] = {}  # session id -> the hash it last listed
        self.upstreams: dict[str, Upstream] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: asyncio.Task | None = None
        self.dirty = False  # a change arrived while a coalesced check was already running
        self.tasks: set[asyncio.Task] = set()
        self.checker: asyncio.Task | None = None
        self.started = False
        self.version = 0  # bumped by every change notice; the cached hash below is for one version
        self.digest: tuple[int, str] | None = None
        # Step 24.2: (conn_id, url) -> the recorded turn its result came back in (bounded).
        self.seen_urls: OrderedDict[tuple[str, str], str | None] = OrderedDict()

    def spawn(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # -- lifecycle
    def start(self, *, periodic=False):
        """Hooks installed on first use; the 30-second check (and the one at startup)
        runs when the backend's lifespan starts the gateway."""
        if not self.started:
            self.started = True
            self.loop = asyncio.get_running_loop()
            sc.TOOLS_CHANGED_HOOKS.append(self._changed_threadsafe)
            sc.PROBER = self._probe_threadsafe
        if periodic and self.checker is None:
            self.checker = self.spawn(self._check_loop())

    async def prune(self):
        removed = await asyncio.to_thread(sc.prune_orphan_secrets, _store(self.app))
        if removed:
            logger.info("steward gateway: removed %d orphaned secret file(s)", len(removed))

    async def stop(self):
        with suppress(ValueError):
            sc.TOOLS_CHANGED_HOOKS.remove(self._changed_threadsafe)
        if sc.PROBER == self._probe_threadsafe:
            sc.PROBER = None
        for task in list(self.tasks):
            task.cancel()
        for upstream in list(self.upstreams.values()):
            await upstream.close()
        self.upstreams.clear()
        self.started, self.checker = False, None

    def _changed_threadsafe(self):
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.changed)

    def changed(self):
        """`steward_tools_changed()`: coalesce one second, then compare the hash; a change
        that lands while that check runs gets one more check after it."""
        self.dirty = True
        self.version += 1
        if self.pending is None or self.pending.done():
            self.pending = self.spawn(self._coalesced())

    async def _coalesced(self):
        while self.dirty:
            await asyncio.sleep(COALESCE_SECONDS)
            self.dirty = False
            await self.check()

    async def _check_loop(self):
        try:
            await self.prune()
        except Exception:  # noqa: BLE001 - startup never depends on it
            logger.exception("steward gateway: orphaned secret cleanup failed")
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - the next round tries again
                logger.exception("steward gateway check failed")
            await asyncio.sleep(CHECK_SECONDS)

    async def check(self):
        """Hash compare (and a notice on change), upstream sessions reconciled, the
        fallback markers judged - after a change, every 30 s and once at startup."""
        digest = await self.current(fresh=True)
        self.announce(digest)
        await self.reconcile()
        drivers = await asyncio.to_thread(self._steward_drivers)
        if drivers:
            def judge():
                with _store(self.app).transaction(write=True) as conn:
                    return sc.check_stream_markers(conn, drivers)
            await asyncio.to_thread(judge)

    def _steward_drivers(self) -> list[dict]:
        from core.state_steward import steward_project
        from api.dsh_driver_routers import manager
        from starlette.requests import Request as _Request
        try:
            runtime = manager(_Request({"type": "http", "app": self.app}))
        except Exception:  # noqa: BLE001 - no runtime, no process to restart
            return []
        project = steward_project()
        row = runtime.get(project) if hasattr(runtime, "get") else None
        if row is None:
            return []
        live = {s.driver_id for s in self.streams.values()}
        return [{"driver_id": row["driver_id"], "enabled": bool(row["enabled"]), "phase": row.get("phase"),
                 "running": bool(runtime.running(project)), "has_stream": row["driver_id"] in live}]

    def announce(self, digest: str):
        """One list_changed on every live stream whose session last listed another hash
        and that was not told about this one yet (changes inside the coalescing second,
        and an undo before the session listed again, make no extra notice)."""
        told = 0
        for stream in list(self.streams.values()):
            if self.served.get(stream.session_id) != digest and stream.notified != digest:
                stream.notified = digest
                stream.queue.put_nowait(("message", LIST_CHANGED))
                told += 1
        if told:
            logger.info("steward gateway: list_changed to %d stream(s)", told)

    # -- upstream sessions
    def _lock(self, conn_id):
        return self.locks.setdefault(conn_id, asyncio.Lock())

    async def reconcile(self):
        rows = await asyncio.to_thread(_read, self.app, lambda conn: {
            r["conn_id"]: dict(r) for r in conn.execute("SELECT * FROM state_steward_connections")})
        for conn_id, upstream in list(self.upstreams.items()):
            row = rows.get(conn_id)
            if row is None or not row["enabled"] or fingerprint(row) != upstream.fingerprint or not upstream.alive:
                await self.drop(conn_id, upstream)

    async def upstream(self, row: dict) -> Upstream:
        async with self._lock(row["conn_id"]):
            current = self.upstreams.get(row["conn_id"])
            if current is not None and current.alive and current.fingerprint == fingerprint(row):
                return current
            if current is not None:
                self.upstreams.pop(row["conn_id"], None)
                await current.close()
            fresh = Upstream(self, row)
            await fresh.open()
            self.upstreams[row["conn_id"]] = fresh
        # A session that opens reads the connection's tool list again (7.8.5).
        self.spawn(self._record(row["conn_id"], fresh))
        return fresh

    async def _record(self, conn_id, upstream):
        try:
            listed = await self._list(upstream)
        except Exception:  # noqa: BLE001 - kept for the next read
            return
        await asyncio.to_thread(sc.record_discovery, _store(self.app), conn_id, listed, upstream.instructions)

    async def drop(self, conn_id, session):
        """Close one connection's cached session. `session` is the one the caller saw fail: it
        is dropped only while it is still the cached one, because a task that lost the race
        must not close the session its winner has already opened under it. It is required,
        not optional: a drop that does not name the session it failed on is the race itself."""
        async with self._lock(conn_id):
            if session is not None and self.upstreams.get(conn_id) is not session:
                return
            upstream = self.upstreams.pop(conn_id, None)
        if upstream is not None:
            await upstream.close()

    @staticmethod
    async def _list(upstream) -> list[dict]:
        """The server's WHOLE tool list (discovery deletes what it does not name): a list
        longer than 20 pages is a failed read, never a partial one."""
        tools, cursor = [], None
        for _ in range(20):
            # Like a call, a page ends with its session: a refused OAuth refresh inside the
            # request is not left to the timeout.
            page = await Gateway._within_session(upstream, upstream.session.list_tools(cursor=cursor))
            tools += [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools]
            cursor = page.nextCursor
            if not cursor:
                return tools
        raise sc.ProbeFailed("tool_list_too_long")

    async def relist(self, conn_id):
        row = await asyncio.to_thread(_read, self.app, lambda conn: conn.execute(
            "SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone())
        upstream = self.upstreams.get(conn_id)
        if row is None or upstream is None or not upstream.alive:
            return
        await self._record(conn_id, upstream)

    async def probe(self, row: dict) -> dict:
        """The tool list of one connection: through its open session when it has one
        (a second `initialize` would evict a browser's), else a short-lived client. A
        refused OAuth refresh is `oauth_needs_consent` (the dashboard asks to authorize)."""
        try:
            return await self._probe(row)
        except steward_oauth.RefreshRefused:
            raise sc.ProbeFailed("oauth_needs_consent") from None

    async def _probe(self, row: dict) -> dict:
        if row["enabled"]:
            # Same one-reopen policy as `call`: a recreated upstream leaves the cached
            # session's `alive` flag true, so the list goes down a dead session. The lost
            # session is dropped and reopened once - dropped only while it is still the
            # cached one, since another task may have replaced it in the meantime - and a
            # second failure is the upstream's own error, recorded as its own type.
            for attempt in (1, 2):
                upstream = await self.upstream(row)
                try:
                    return {"tools": await self._list(upstream), "instructions": upstream.instructions}
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:  # noqa: BLE001 - judged just below
                    if not _session_lost(exc):
                        raise
                    broken = exc
                logger.info("steward gateway: upstream session of %s failed while probing (%s)",
                            row["conn_id"], type(broken).__name__)
                await self.drop(row["conn_id"], upstream)
                if attempt == 2:
                    raise broken
            raise SessionBroken("unreachable")
        temporary = Upstream(self, row)
        await temporary.open()
        try:
            return {"tools": await self._list(temporary), "instructions": temporary.instructions}
        finally:
            await temporary.close()

    def _probe_threadsafe(self, row: dict) -> dict:
        """core.state_steward_connections.PROBER: runs on this gateway's loop."""
        future = asyncio.run_coroutine_threadsafe(self.probe(row), self.loop)
        return future.result(timeout=OPEN_TIMEOUT + UPSTREAM_TIMEOUT + 10)

    async def call(self, row: dict, raw_name: str, arguments, *, retry: bool):
        """Forward one call. Only a lost session (the client's 404 "Session terminated",
        its "Connection closed", a closed stream or a transport failure) drops the session
        and, for a non-outward tool, reopens it once; any other error - a JSON-RPC error
        or an answer that does not parse - is the upstream's answer, passed on as it is."""
        for attempt in (1, 2):
            upstream = await self.upstream(row)
            try:
                result = await self._within_session(upstream, _call_tool(upstream.session, raw_name, arguments))
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)
            except asyncio.TimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001 - judged just below
                if not _session_lost(exc):
                    raise
                broken = exc
            logger.info("steward gateway: upstream session of %s failed (%s)", row["conn_id"], type(broken).__name__)
            await self.drop(row["conn_id"], upstream)
            if not retry or attempt == 2:
                raise SessionBroken(type(broken).__name__)
        raise SessionBroken("unreachable")

    @staticmethod
    async def _within_session(upstream, coroutine):
        """Await one request while its session lives. The mcp client does not answer a
        pending request when its transport dies under it (a refused OAuth refresh inside a
        POST, an HTTP error status): the session's own task ends instead, so that ends the
        wait - a refused refresh as RefreshRefused, anything else as a lost session."""
        call = asyncio.ensure_future(coroutine)
        waiting = {call} | ({upstream.task} if upstream.task is not None else set())
        done, _ = await asyncio.wait(waiting, timeout=UPSTREAM_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        if call in done:
            return call.result()
        call.cancel()
        with suppress(BaseException):
            await call
        if not done:
            raise asyncio.TimeoutError()
        if upstream.error == "connection_unavailable":
            raise steward_oauth.RefreshRefused("oauth_needs_consent")
        import anyio
        raise anyio.ClosedResourceError()

    # -- JSON-RPC
    async def handle(self, message, driver: dict, session_id: str | None) -> dict | None:
        method, mid = message.get("method"), message.get("id")
        if not isinstance(method, str):
            return None if mid is None else {"jsonrpc": "2.0", "id": mid,
                                             "error": {"code": -32600, "message": "Invalid Request"}}
        if mid is None:
            return None  # notifications are acknowledged and dropped
        if method == "initialize":
            asked = (message.get("params") or {}).get("protocolVersion")
            version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": True}}, "serverInfo": SERVER_INFO}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            entries = await asyncio.to_thread(self._serve, driver["driver_id"])
            digest = sc.effective_hash(entries)
            if session_id:
                self.served.pop(session_id, None)
                self.served[session_id] = digest
                while len(self.served) > SERVED_KEPT:
                    del self.served[next(iter(self.served))]
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [e["tool"] for e in entries]}}
        if method == "tools/call":
            return await self.tools_call(mid, message.get("params") or {}, driver)
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}

    def _serve(self, driver_id):
        with _store(self.app).transaction(write=True) as conn:
            entries = sc.effective(conn)
            sc.record_served(conn, driver_id, sc.effective_hash(entries))
        return entries

    async def tools_call(self, mid, params, driver):
        name, arguments = params.get("name"), params.get("arguments")
        if arguments is None:
            arguments = {}
        parts = sc.split_name(name) if isinstance(name, str) else None
        if parts is None or not isinstance(arguments, dict):
            return _error(mid, "tool_not_allowed", "no such tool")
        conn_id, gw = parts

        def judge():
            """One BEGIN IMMEDIATE transaction: the judgement and, when it passes, the
            reserved ledger row (the daily cap counts reserved rows too) and, for an
            outward call, the confirmation consumed (7.8.9 step 6). A refusal after the
            reservation rolls the whole transaction back, the row with it."""
            with _store(self.app).transaction(write=True) as conn:
                row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
                if row is None:
                    return "tool_not_allowed", None, None, None, None, None
                if not sc.connection_usable(row):
                    return "connection_unavailable", None, None, None, None, None
                entry = next((e for e in sc.effective(conn) if e["conn_id"] == conn_id and e["gw_name"] == gw), None)
                if entry is None:
                    return "tool_not_allowed", None, None, None, None, None
                if row["visibility"] == "private" and sc.audience_of(conn) != "owner":
                    return "not_visible_in_this_turn", None, None, None, None, None
                if sc.is_dev_mcp(row):
                    # Step 24.4c (7.8.24 "九" 5): private only, never a subagent_* tool, only a
                    # tool the owner pinned on `code`, and a write tool always outward (the
                    # owner confirms every call) whatever its stored flag says.
                    if (row["visibility"] != "private" or entry["raw_name"].startswith(sc.DEV_MCP_SUBAGENT)
                            or not caps.code_pin(conn, row["conn_uid"], entry["raw_name"])):
                        return "tool_not_allowed", None, None, None, None, None
                    if entry["raw_name"] in sc.dev_mcp_writes():
                        entry = {**entry, "outward": True, "egress": True}
                if sc.blocked_present(arguments, entry["blocked_args"]) or (
                        row["visibility"] == "public"
                        and sc.url_argument_present(arguments, sc.unblocked_url_paths(entry))):
                    return "argument_blocked", None, None, None, None, None
                call_id = sc.reserve_call(conn, conn_id, entry["raw_name"], entry["daily_cap"])
                if call_id is None:
                    return "daily_cap_reached", None, None, None, None, None
                code = note = None
                if entry["outward"]:
                    try:
                        code = sconf.consume(conn, row, entry["raw_name"], arguments)
                    except StateGraphError:
                        raise _Refused("invalid_arguments")
                    if code is None:
                        raise _Refused("confirmation_required")
                    note = sconf.owner_turn_note(conn, code)
                return None, dict(row), entry, call_id, code, note

        def judged():
            try:
                return judge()
            except _Refused as refused:  # the transaction rolled back: no row, nothing consumed
                return refused.code, None, None, None, None, None

        refusal, row, entry, call_id, confirmation, note = await asyncio.to_thread(judged)
        texts = {"tool_not_allowed": "this tool is not allowed for you",
                 "connection_unavailable": "this connection is not available now",
                 "not_visible_in_this_turn": "this connection is not available in this conversation",
                 "argument_blocked": "an argument of this call is blocked by your owner",
                 "daily_cap_reached": "this tool reached its daily limit",
                 "confirmation_required": "an outward tool runs only after the requester confirms "
                                          "(call request_confirmation, then run_confirmed with the code)",
                 "invalid_arguments": "the arguments are not canonical JSON text"}
        if refusal:
            logger.info("steward gateway: tools/call %s refused %s", name, refusal)
            return _error(mid, refusal, texts[refusal])
        started = time.monotonic()
        request_bytes = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8", "replace"))
        rpc_error, is_error, result = None, False, None
        try:
            result = await self.call(row, entry["raw_name"], arguments, retry=not entry["outward"])
            is_error = bool(result.get("isError"))
            if is_error:
                content = result.get("content") if isinstance(result.get("content"), list) else []
                result = {**result, "content": [{"type": "text", "text": UPSTREAM_ERROR_LINE}, *content]}
            answer = {"jsonrpc": "2.0", "id": mid, "result": result}
        except asyncio.TimeoutError:
            rpc_error = "upstream_timeout"
        except (SessionBroken, sc.ProbeFailed) as exc:
            rpc_error = getattr(exc, "code", None) or "upstream_unavailable"
        except steward_oauth.RefreshRefused:
            rpc_error = "connection_unavailable"
        except StateGraphError as exc:
            rpc_error = str(exc).split(":")[0]
        except Exception as exc:  # noqa: BLE001 - an upstream JSON-RPC error, by its code
            code = getattr(getattr(exc, "error", None), "code", None)
            rpc_error = f"upstream_error:{code}" if code is not None else "upstream_error"
        if rpc_error:
            answer = _error(mid, rpc_error, "the connection did not answer this call" + (f" {note}" if note else ""))
        elif note:
            # An owner turn that took a family group's confirmation is told whose it was,
            # first in what it reads, whatever the outcome (director 581).
            content = result.get("content") if isinstance(result.get("content"), list) else []
            answer["result"] = {**result, "content": [{"type": "text", "text": note}, *content]}
        if not rpc_error and not is_error:
            turn = await asyncio.to_thread(_read, self.app, recorded_turn)
            self.remember_urls(conn_id, result, turn)
        response_bytes = len(json.dumps(answer, ensure_ascii=False).encode("utf-8", "replace"))
        duration = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(sc.finish_call, _store(self.app), call_id, rpc_error=rpc_error,
                                is_error=is_error, request_bytes=request_bytes, response_bytes=response_bytes,
                                duration_ms=duration)
        if confirmation is not None:
            # The record says how its one forward went (never the result body).
            def settle():
                with _store(self.app).transaction(write=True) as conn:
                    sconf.finish(conn, confirmation, not rpc_error and not is_error,
                                 rpc_error or ("tool_error" if is_error else None))
            await asyncio.to_thread(settle)
        logger.info("steward gateway: tools/call %s status=%s bytes=%d/%d %dms", name,
                    rpc_error or ("tool_error" if is_error else "ok"), request_bytes, response_bytes, duration)
        return answer

    def remember_urls(self, conn_id: str, result, turn):
        """Every url in a forwarded result, verbatim, for /fetch (bounded, oldest out)."""
        try:
            text = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        for url in URL_IN_TEXT.findall(text):
            self.seen_urls.pop((conn_id, url), None)
            self.seen_urls[(conn_id, url)] = turn
            while len(self.seen_urls) > SEEN_URLS_KEPT:
                self.seen_urls.popitem(last=False)

    async def current(self, *, fresh=False) -> str:
        """The effective hash, cached per change version: a POST does not rescan the tool
        table unless a change notice came since (the 30-second check reads it fresh, which
        also catches a write that sent no notice)."""
        version = self.version
        if not fresh and self.digest is not None and self.digest[0] == version:
            return self.digest[1]
        digest = await asyncio.to_thread(_read, self.app, sc.current_hash)
        self.digest = (version, digest)
        return digest

    async def stale(self, session_id) -> bool:
        return self.served.get(session_id) != await self.current()


# ------------------------------------------------------------------ files (step 24.2)
URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\`)\]}]+")
SEEN_URLS_KEPT = 2000
FETCH_REDIRECTS = 3


def recorded_turn(conn) -> str | None:
    """The turn the steward's host recorded last (core.state_groups' one-row record)."""
    try:
        row = conn.execute("SELECT turn FROM state_steward_turn_audience WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _origin(url: str):
    """(scheme, host, port) of an http(s) url, or None; a malformed url (a port out of range, an
    unclosed [ ) raises ValueError - the caller refuses it as a bad url, never a 500. So does one
    urlsplit takes but httpx would not send (a control character such as DEL, a part too long):
    it is judged here, before the client ever builds a request from it."""
    try:
        httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(str(exc)) from None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _fetch_refused(status: int, code: str):
    logger.info("steward gateway: fetch refused %s", code)
    return JSONResponse({"detail": code}, status_code=status)


def gateway(app) -> Gateway:
    found = getattr(app.state, "steward_gateway", None)
    if found is None:
        found = Gateway(app)
        app.state.steward_gateway = found
    if not found.started:
        found.start()
    return found


def _sse(item) -> str:
    return "event: message\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"


@router.post(PATH)
async def post(request: Request):
    driver, refused = admit(request)
    if refused is not None:
        return refused
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return JSONResponse({"detail": "request_too_large"}, status_code=413)
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            return JSONResponse({"detail": "request_too_large"}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    batch = body if isinstance(body, list) else [body]
    if not batch or not all(isinstance(m, dict) for m in batch):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
    gw = gateway(request.app)
    session_id = request.headers.get("mcp-session-id")
    initializing = any(m.get("method") == "initialize" for m in batch)
    if initializing:
        session_id = uuid.uuid4().hex
        logger.info("steward gateway: a session opened by %s from %s", driver["driver_id"], _peer(request))
    if all(m.get("id") is None for m in batch):
        return Response(status_code=202)
    answers = [a for a in [await gw.handle(m, driver, session_id) for m in batch] if a is not None]
    headers = {"Mcp-Session-Id": session_id} if initializing else {}
    payload = answers if isinstance(body, list) else answers[0]
    # A session that missed a change (its GET stream gave up, the backend restarted):
    # the answer comes as an event stream carrying list_changed first (step 0, 7(d)).
    listing = any(m.get("method") in ("tools/list", "initialize") for m in batch)
    if session_id and not listing and await gw.stale(session_id):
        async def events():
            yield _sse(LIST_CHANGED)
            for answer in answers:
                yield _sse(answer)
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
    return JSONResponse(payload, headers=headers)


@router.get(PATH)
async def get(request: Request):
    driver, refused = admit(request)
    if refused is not None:
        return refused
    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return JSONResponse({"detail": "mcp_session_required"}, status_code=400)
    gw = gateway(request.app)
    others = [s for s in gw.streams.values() if s.driver_id == driver["driver_id"] and s.session_id != session_id]
    if len(others) >= STREAMS_PER_DRIVER:
        return Response(status_code=405)
    old = gw.streams.get(session_id)
    if old is not None:
        old.queue.put_nowait(("close", None))
    stream = Stream(driver["driver_id"], session_id)
    current = await asyncio.to_thread(_read, request.app, sc.current_hash)
    stale = gw.served.get(session_id) != current
    stream.notified = current
    gw.streams[session_id] = stream

    async def events():
        try:
            yield f"retry: {RETRY_MS}\n\n"
            if stale:
                yield _sse(LIST_CHANGED)
            while True:
                try:
                    kind, item = await asyncio.wait_for(stream.queue.get(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "close":
                    return
                yield _sse(item)
        finally:
            # Disconnects are learnt only through cancellation (uvicorn drops writes to a
            # closed client silently), so the stream is deregistered here.
            if gw.streams.get(session_id) is stream:
                del gw.streams[session_id]

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get(PATH + "/{conn_id}/fetch")
async def fetch(conn_id: str, request: Request, url: str = ""):
    """One file a connection's result named, for the steward's host (see the module text)."""
    _driver, refused = admit(request)
    if refused is not None:
        return refused
    gw = gateway(request.app)

    def judge():
        with _store(request.app).transaction() as conn:
            row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            if row is None:
                return "unknown_connection", None, None
            if not sc.connection_usable(row):
                return "connection_unavailable", None, None
            if row["visibility"] == "private" and sc.audience_of(conn) != "owner":
                return "not_visible_in_this_turn", None, None
            return None, dict(row), recorded_turn(conn)

    code, row, turn = await asyncio.to_thread(judge)
    if code:
        return _fetch_refused({"unknown_connection": 404, "connection_unavailable": 409}.get(code, 403), code)
    if not url or (conn_id, url) not in gw.seen_urls or gw.seen_urls[(conn_id, url)] != turn or turn is None:
        return _fetch_refused(403, "url_not_seen")
    origin = _origin(row["url"])
    try:
        asked = _origin(url)
    except ValueError:
        return _fetch_refused(400, "bad_url")
    if origin is None or asked != origin:
        return _fetch_refused(403, "other_origin")
    cap = media_types.limits()["outbound_bytes"]
    try:
        # The url's origin is the connection's own (checked above), so the address is the one the
        # connection's own host resolves to - judged the same way as its upstream session's.
        address = await asyncio.to_thread(lambda: sc.checked_address(row["url"], dev_mcp_ok=sc.dev_mcp_private(row)))
    except (StateGraphError, sc.ProbeFailed) as exc:
        return _fetch_refused(403, getattr(exc, "code", None) or str(exc).split(":")[0])
    # No upstream credential: no header, no oauth token, no cookie - a bare client.
    async with httpx.AsyncClient(transport=transport_for(address), follow_redirects=False,
                                 timeout=httpx.Timeout(30.0, read=UPSTREAM_TIMEOUT)) as client:
        target = url
        try:
            for _hop in range(FETCH_REDIRECTS + 1):
                async with client.stream("GET", target) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        try:
                            target = urljoin(target, response.headers.get("location", ""))
                            moved = _origin(target)
                        except ValueError:
                            return _fetch_refused(502, "redirect_bad_url")
                        if moved != origin:
                            return _fetch_refused(502, "redirect_other_origin")
                        continue
                    if response.status_code != 200:
                        return _fetch_refused(502, "upstream_status")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > cap:
                        return _fetch_refused(413, "too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > cap:
                            return _fetch_refused(413, "too_large")
                    break
            else:
                return _fetch_refused(502, "redirect_other_origin")
        except httpx.HTTPError:
            return _fetch_refused(502, "upstream_unavailable")
    kind = media_types.identify(bytes(body))  # an OOXML file by its zip directory (7.8.12c "六")
    if kind is None:
        return _fetch_refused(415, "type")
    logger.info("steward gateway: fetch %s mime=%s bytes=%d", conn_id, kind["mime"], len(body))
    return Response(content=bytes(body), media_type=kind["mime"],
                    headers={"X-AItelier-Media-Kind": kind["kind"], "X-Content-Type-Options": "nosniff"})


@router.delete(PATH)
async def delete(request: Request):
    _driver, refused = admit(request)
    if refused is not None:
        return refused
    session_id = request.headers.get("mcp-session-id")
    if session_id:
        gateway(request.app).served.pop(session_id, None)
    return Response(status_code=200)
