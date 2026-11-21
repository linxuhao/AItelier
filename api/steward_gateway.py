"""The steward's one MCP address: the backend's aggregating gateway (step 24.1a,
design-steward 7.8.5).

To the steward's process it is an MCP server (`/api/steward/mcp`, streamable HTTP:
POST, GET, DELETE) - a small hand-written JSON-RPC (initialize, notifications, ping,
tools/list, tools/call) and a session-keyed GET stream on which
`notifications/tools/list_changed` goes out whenever the effective tool set changes,
so a newly allowed tool needs no steward restart. To each registered connection it is
an MCP client: ONE long-lived upstream session per connection (the `mcp` package),
reopened only when the connection's address, auth or generation changes.

Who may enter is checked here on every method, whatever the write gate says: a
steward driver's token, not via Cloudflare, from an allowed source address
(`AITELIER_STEWARD_GATEWAY_SOURCES`). The token is never sent upstream; arguments,
results and secrets are never logged.

`tools/list` never reaches upstream: it is the effective set from the database
(core.state_steward_connections.effective), byte for byte the definitions the owner
allowed. `tools/call` is judged in order - connection usable, tool allowed and
unchanged, private connection only in an `owner` turn, no blocked argument, daily cap,
outward tools need a confirmation (24.1b; until then refused) - then forwarded.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import suppress

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from api import authz
from core import state_steward_connections as sc
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
MAX_BODY = 40 * 1024 * 1024
STREAMS_PER_DRIVER = 4
KEEPALIVE_SECONDS = 15.0
RETRY_MS = 30000
COALESCE_SECONDS = 1.0
CHECK_SECONDS = 30.0
UPSTREAM_TIMEOUT = 170.0  # under the steward entry's toolCallTimeoutMs (180 s)
OPEN_TIMEOUT = 30.0


def _error(mid, code, text):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"{code}: {text}", "data": {"code": code}}}


# ------------------------------------------------------------------ who may enter
def _default_route_gateway() -> str | None:
    """The container's default gateway (the address a host process arrives from
    through a published port), read from /proc/net/route."""
    try:
        with open("/proc/net/route") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    raw = int(fields[2], 16)
                    return ".".join(str((raw >> s) & 0xFF) for s in (0, 8, 16, 24))
    except (OSError, ValueError):
        return None
    return None


def allowed_sources() -> set[str]:
    raw = os.environ.get("AITELIER_STEWARD_GATEWAY_SOURCES", "")
    if raw.strip():
        return {s.strip() for s in raw.split(",") if s.strip()}
    found = {"127.0.0.1", "::1"}
    gateway = _default_route_gateway()
    if gateway:
        found.add(gateway)
    return found


def steward_driver(request: Request) -> dict | None:
    """The steward driver behind this request, or None (-> 403)."""
    token = request.headers.get(authz.DRIVER_TOKEN_HEADER, "").strip()
    if not token or authz.is_via_cloudflare(request):
        return None
    host = request.client.host if request.client else None
    if host not in allowed_sources():
        return None
    registry = authz.driver_registry()
    if registry is None:
        return None
    row = registry.lookup_token(token)
    capabilities = (row or {}).get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("role") != "steward":
        return None
    return row


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
        address = await asyncio.to_thread(sc.checked_address, self.row["url"])
        headers = {}
        if self.row["auth_kind"] == "header":
            secret = await asyncio.to_thread(sc.read_secret, self.conn_id) or {}
            headers[self.row["header_name"] or "Authorization"] = secret.get("header_value", "")
        self.task = asyncio.create_task(self._run(address, headers))
        try:
            await asyncio.wait_for(self.ready.wait(), OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            self.error = "upstream_timeout"
        if self.session is None:
            await self.close()
            raise sc.ProbeFailed(self.error or "upstream_unavailable")

    async def _run(self, address, headers):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        try:
            async with httpx.AsyncClient(headers=headers, transport=transport_for(address), follow_redirects=False,
                                         timeout=httpx.Timeout(30.0, read=UPSTREAM_TIMEOUT)) as client:
                async with streamable_http_client(self.row["url"], http_client=client) as (read, write, _sid):
                    async with ClientSession(read, write, message_handler=self._message) as session:
                        result = await session.initialize()
                        self.instructions = getattr(result, "instructions", None)
                        self.session = session
                        self.ready.set()
                        await self.closing.wait()
        except BaseException as exc:  # noqa: BLE001 - the session is simply gone
            if not isinstance(exc, asyncio.CancelledError):
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
        self.tasks: set[asyncio.Task] = set()
        self.checker: asyncio.Task | None = None
        self.started = False

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
        removed = await asyncio.to_thread(_read, self.app, sc.prune_orphan_secrets)
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
        """`steward_tools_changed()`: coalesce one second, then compare the hash."""
        if self.pending is None or self.pending.done():
            self.pending = self.spawn(self._coalesced())

    async def _coalesced(self):
        await asyncio.sleep(COALESCE_SECONDS)
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
        digest = await asyncio.to_thread(_read, self.app, sc.current_hash)
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
                async with self._lock(conn_id):
                    if self.upstreams.get(conn_id) is upstream:
                        del self.upstreams[conn_id]
                await upstream.close()

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

    async def drop(self, conn_id):
        async with self._lock(conn_id):
            upstream = self.upstreams.pop(conn_id, None)
        if upstream is not None:
            await upstream.close()

    @staticmethod
    async def _list(upstream) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            page = await asyncio.wait_for(upstream.session.list_tools(cursor=cursor), UPSTREAM_TIMEOUT)
            tools += [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools]
            cursor = page.nextCursor
            if not cursor:
                break
        return tools

    async def relist(self, conn_id):
        row = await asyncio.to_thread(_read, self.app, lambda conn: conn.execute(
            "SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone())
        upstream = self.upstreams.get(conn_id)
        if row is None or upstream is None or not upstream.alive:
            return
        await self._record(conn_id, upstream)

    async def probe(self, row: dict) -> dict:
        """The tool list of one connection: through its open session when it has one
        (a second `initialize` would evict a browser's), else a short-lived client."""
        if row["enabled"]:
            upstream = await self.upstream(row)
            return {"tools": await self._list(upstream), "instructions": upstream.instructions}
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
        from mcp.shared.exceptions import McpError
        for attempt in (1, 2):
            upstream = await self.upstream(row)
            try:
                result = await asyncio.wait_for(upstream.session.call_tool(raw_name, arguments), UPSTREAM_TIMEOUT)
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)
            except McpError as exc:
                if "terminated" not in str(exc).lower() and "not found" not in str(exc).lower():
                    raise
                broken = exc
            except asyncio.TimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001 - the session itself failed
                broken = exc
            await self.drop(row["conn_id"])
            if not retry or attempt == 2:
                raise SessionBroken(type(broken).__name__)
        raise SessionBroken("unreachable")

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
                self.served[session_id] = digest
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

        def judge(conn):
            row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            if row is None:
                return "tool_not_allowed", None, None
            if not sc.connection_usable(row):
                return "connection_unavailable", None, None
            entry = next((e for e in sc.effective(conn) if e["conn_id"] == conn_id and e["gw_name"] == gw), None)
            if entry is None:
                return "tool_not_allowed", None, None
            if row["visibility"] == "private" and sc.audience_of(conn) != "owner":
                return "not_visible_in_this_turn", None, None
            if set(arguments) & set(entry["blocked_args"]):
                return "argument_blocked", None, None
            if entry["daily_cap"] is not None and sc.calls_today(conn, conn_id, entry["raw_name"]) >= entry["daily_cap"]:
                return "daily_cap_reached", None, None
            if entry["outward"]:
                return "confirmation_required", None, None
            return None, dict(row), entry

        refusal, row, entry = await asyncio.to_thread(_read, self.app, judge)
        texts = {"tool_not_allowed": "this tool is not allowed for you",
                 "connection_unavailable": "this connection is not available now",
                 "not_visible_in_this_turn": "this connection is not available in this conversation",
                 "argument_blocked": "an argument of this call is blocked by your owner",
                 "daily_cap_reached": "this tool reached its daily limit",
                 "confirmation_required": "an outward tool runs only after the requester confirms "
                                          "(call request_confirmation first)"}
        if refusal:
            logger.info("steward gateway: tools/call %s refused %s", name, refusal)
            return _error(mid, refusal, texts[refusal])
        started = time.monotonic()
        request_bytes = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8", "replace"))
        rpc_error, is_error, result = None, False, None
        try:
            result = await self.call(row, entry["raw_name"], arguments, retry=not entry["outward"])
            is_error = bool(result.get("isError"))
            answer = {"jsonrpc": "2.0", "id": mid, "result": result}
        except asyncio.TimeoutError:
            rpc_error = "upstream_timeout"
        except (SessionBroken, sc.ProbeFailed) as exc:
            rpc_error = getattr(exc, "code", None) or "upstream_unavailable"
        except StateGraphError as exc:
            rpc_error = str(exc).split(":")[0]
        except Exception as exc:  # noqa: BLE001 - an upstream JSON-RPC error, by its code
            code = getattr(getattr(exc, "error", None), "code", None)
            rpc_error = f"upstream_error:{code}" if code is not None else "upstream_error"
        if rpc_error:
            answer = _error(mid, rpc_error, "the connection did not answer this call")
        response_bytes = len(json.dumps(answer, ensure_ascii=False).encode("utf-8", "replace"))
        duration = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(sc.record_call, _store(self.app), conn_id, entry["raw_name"], rpc_error=rpc_error,
                                is_error=is_error, request_bytes=request_bytes, response_bytes=response_bytes,
                                duration_ms=duration)
        logger.info("steward gateway: tools/call %s status=%s bytes=%d/%d %dms", name,
                    rpc_error or ("tool_error" if is_error else "ok"), request_bytes, response_bytes, duration)
        return answer

    async def stale(self, session_id) -> bool:
        current = await asyncio.to_thread(_read, self.app, sc.current_hash)
        return self.served.get(session_id) != current


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
    driver = steward_driver(request)
    if driver is None:
        return _forbidden()
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
    driver = steward_driver(request)
    if driver is None:
        return _forbidden()
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


@router.delete(PATH)
async def delete(request: Request):
    if steward_driver(request) is None:
        return _forbidden()
    return Response(status_code=200)
