"""`POST /{project_id}/completion` starts its response at once and keeps it alive.

THE DEFECT (measured 2026-11-18): the handler awaited `gateway.generate_native`
in a threadpool and only THEN returned a dict, so no response header left the
backend until the model had answered. The resident driver calls this endpoint
with Node's global `fetch` (undici), whose default `headersTimeout` is 300 s:
one turn ended at exactly 300 s with `fetch failed` while the backend's own row
showed the model answered 309 s after the step started. The paid answer was
discarded and a driver phase went `failed`.

WHAT IS WITNESSED HERE, and how - every arm holds the model call OPEN on a
`threading.Event` that only this test sets, and only after the bytes it asserts
on are already in hand (the stub runs in a worker thread, so the event is a
`threading.Event`: an `asyncio.Event` cannot be waited for from there):

  a. `test_the_response_starts_and_a_keepalive_arrives_before_the_model_answers`
     enters `client.stream(...)` - which returns once `http.response.start` has
     been received - and reads at least one body byte while the stub is still
     blocked. Both facts are recorded before the event is set, and the stub's
     own "I am inside the call" flag is asserted, so the witness cannot be
     vacuous.
  b. `test_the_keepalive_repeats_while_the_model_is_outstanding` patches the
     interval constant short, holds the call open across two intervals, and
     demands at least two whitespace-only chunks PLUS the fact that no
     non-whitespace byte arrived before the last one (undici's `bodyTimeout` is
     300 s too, which is why ONE keepalive is not enough).
  c. `test_the_streamed_body_is_the_same_six_key_object` strips the keepalive
     whitespace and parses the remainder: exactly the six keys the endpoint has
     always returned, with the values the stub produced, and the answering model
     recorded in the database by then.

  A fourth witness pins the trap the streaming shape introduces: the per-session
  `asyncio.Lock` must be picked up INSIDE the response body. Returning the
  `StreamingResponse` from inside `async with lock:` releases it before the
  generator runs, and two concurrent turns of one session would share one
  gateway - the stub's peak concurrency says whether they did.

TRANSPORT: httpx 0.28.1's bundled `ASGITransport` CANNOT answer this file's
question. It awaits the whole app and then returns `b"".join(body_parts)`, so
`client.stream(...)` hands back the entire body at once and the headers only
after the response has ended - with it every arm below would fail on a fix that
works. `_StreamingASGITransport` therefore implements the same ASGI call with
PER-MESSAGE delivery (start as soon as the app sends it, one response-stream
chunk per `http.response.body` message), and
`test_a_real_socket_sends_the_headers_and_a_keepalive_before_the_answer` repeats
the essential arm over a REAL loopback socket served by uvicorn, so the
in-process transport is not the only witness.

The stubbed thing is the WHOLE model call (`AIGateway.generate_native`), not a
smaller seam: what is under test is when bytes are written relative to that
call, so the call must be one opaque block whose beginning and end this file can
see. Everything else is real - the real `api.main.app` (so the middleware stack
is included), a real `DriverRegistry`, a real `DshDrivers`, real authorization,
and the real `record_model_call` write.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from api import dsh_driver_routers as routers

ROOT_SESSION = "sess-root"
# The route the session ASKS for and the concrete model that answers it. They
# differ because the store refuses to record a value that IS the route alias.
ROOT_ROUTE = "worker"
ROOT_ENDPOINT = "opencodego/deepseek-v4.1-flash"

# The patched interval. Short enough that the whole file costs seconds, long
# enough that two keepalives cannot be confused with one.
_FAST_KEEPALIVE = 0.2
# Every wait is bounded, so a regression can never hang the suite.
_TIMEOUT = 10.0
_STUB_ESCAPE = 30.0

# What the stubbed model produced: six values, and the six keys the body must
# carry them in.
TEXT = "streamed answer"
REASONING = "checked before answering"
TOOL_CALLS = [{"id": "call-1", "type": "function",
               "function": {"name": "wait", "arguments": "{}"}}]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}

# The app tasks driven by `_StreamingASGITransport`; referenced so a running app
# cannot be garbage-collected out from under its own stream.
_TASKS: set[asyncio.Task] = set()


# ── a transport that delivers the app's messages AS THEY ARE SENT ────────────

class _MessageStream(httpx.AsyncByteStream):
    """One ASGI body message at a time, the way a server writes it out."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                # The app died mid-body: a real server closes the connection
                # here, and the client sees a transport error, not a shorter body.
                raise item
            yield item


class _StreamingASGITransport(httpx.AsyncBaseTransport):
    """The ASGI protocol with per-message delivery (see the module docstring).

    Only CLIENT-side buffering is removed; `self.app` is called exactly as a
    server calls it, and the messages it chooses to send - and their order - are
    what the witnesses observe.
    """

    def __init__(self, app):
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        chunks = request.stream.__aiter__()
        request_complete = False
        response_complete = asyncio.Event()
        head: asyncio.Future = asyncio.get_running_loop().create_future()
        queue: asyncio.Queue = asyncio.Queue()

        async def receive() -> dict:
            nonlocal request_complete
            if request_complete:
                await response_complete.wait()
                return {"type": "http.disconnect"}
            try:
                body = await chunks.__anext__()
            except StopAsyncIteration:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": body, "more_body": True}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                if not head.done():
                    # THE instant the client's `headersTimeout` cares about.
                    head.set_result((message["status"], message.get("headers", [])))
                return
            if message["type"] != "http.response.body":
                return
            body = message.get("body", b"")
            if body and request.method != "HEAD":
                queue.put_nowait(body)
            if not message.get("more_body", False):
                response_complete.set()
                queue.put_nowait(None)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except BaseException as exc:  # a dead connection mid-body
                response_complete.set()
                if not head.done():
                    head.set_exception(exc)
                else:
                    queue.put_nowait(exc)

        task = asyncio.create_task(run())
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        status_code, headers = await head
        return httpx.Response(status_code, headers=headers,
                              stream=_MessageStream(queue))


# ── the model call, held open by this test ───────────────────────────────────

class _ModelGate:
    """One stub call's timeline, on the thread that runs it.

    `began` / `answered_at` are the two instants every arm is measured against,
    and `active` / `peak` say how many stub calls were inside the call at once
    (which is exactly what the per-session lock protects). `calls` refuses a
    vacuous witness: a stub that was never entered would make every timing
    assertion pass for the wrong reason.
    """

    def __init__(self):
        self.began = threading.Event()
        self.release = threading.Event()
        self.answered_at: float | None = None
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def stub_call(self) -> None:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.began.set()
        try:
            # Bounded: a test that fails before releasing must not hang.
            self.release.wait(_STUB_ESCAPE)
        finally:
            with self._lock:
                self.active -= 1
                if self.answered_at is None:
                    self.answered_at = time.monotonic()


def _stub_generate_native(gate: _ModelGate):
    """The one stubbed thing: the model call. No provider is called."""
    from core.ai_router import NativeTurn

    def generate_native(self, messages, *, tools=None, tool_choice="auto"):
        gate.stub_call()
        self.last_usage = dict(USAGE)
        self.active_model = ROOT_ENDPOINT
        return NativeTurn(text=TEXT, tool_calls=[dict(call) for call in TOOL_CALLS],
                          reasoning_content=REASONING, truncated=False)

    return generate_native


# ── the real dsh-driver router, over a real host database ────────────────────

@pytest.fixture
def stream_world(tmp_path, monkeypatch):
    """The REAL `api.main.app`, a real DBManager, a real DriverRegistry.

    Authorization is not replaced: the gate is ON because the DEPLOYMENT is
    configured (`cf_access` is told it has a team domain and an AUD, which is
    the real switch) and the driver presents its own registered token, so a 403
    or a 404 here would be the app's decision. What the test owns is the driver
    ROW (as a started process would have written it), a tmp route table, and the
    model call itself.
    """
    from api import authz
    from api.main import app
    from core import cf_access, drivers
    from core.db_manager import DBManager
    from core.drivers import DriverRegistry
    from core.dsh_driver import DshDrivers
    import core.ai_router as ai

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "host.sqlite"))
    registry = DriverRegistry(db, "completion-stream-pepper-" * 2)
    registered = registry.register("dsh-fixture", "DSH fixture", actor="test")
    token = registered["token"]
    runtime = DshDrivers(db, registry, tmp_path / "drivers")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO dsh_drivers(project_id,driver_id,enabled,phase,provider,model,"
            "session_id,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("alpha", "dsh-fixture", 1, "running", "aitelier", ROOT_ROUTE,
             ROOT_SESSION, "2026-11-18T00:00:00+00:00"))
        conn.commit()
    monkeypatch.setattr(DshDrivers, "running", lambda self, project_id: True)
    providers = tmp_path / "llm_providers.json"
    routes = tmp_path / "model_routes.json"
    providers.write_text(json.dumps({ROOT_ENDPOINT.split("/", 1)[0]: {
        "base_url": "https://example.invalid/v1", "api_key_env": "FIXTURE"}}))
    routes.write_text(json.dumps({ROOT_ROUTE: [ROOT_ENDPOINT]}))
    gateway_class = ai.AIGateway
    monkeypatch.setattr(ai, "_read_secret", lambda name: "fixture-key")
    monkeypatch.setattr(ai, "_ENDPOINT_COOLDOWN", {})
    monkeypatch.setattr(ai, "AIGateway", lambda model, **kw: gateway_class(
        model, config_path=str(providers), routes_path=str(routes), **kw))
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "fixture.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "fixture-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    monkeypatch.setattr(drivers, "_LAST_SEEN", {})
    previous = getattr(app.state, "dsh_drivers", None)
    previous_test_mode = getattr(app.state, "_test_mode", False)
    app.state.dsh_drivers = runtime
    app.state._test_mode = False
    try:
        yield SimpleNamespace(app=app, db=db, registry=registry, runtime=runtime,
                              gateway_class=gateway_class, token=token,
                              headers={"X-AItelier-Driver-Token": token},
                              completion={"session_id": ROOT_SESSION,
                                          "messages": [{"role": "user", "content": "work"}]})
    finally:
        app.state.dsh_drivers = previous
        app.state._test_mode = previous_test_mode


@pytest.fixture
def served_world(stream_world):
    """The same real app on a REAL loopback socket: uvicorn in a thread.

    The in-process transport observes the ASGI messages the app emits; this one
    proves that nothing between those messages and the wire holds a byte back -
    the shape the driver's `fetch` actually talks to.
    """
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(stream_world.app, log_level="error",
                                           lifespan="off", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + _TIMEOUT
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started, "the loopback ASGI server never started"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=_TIMEOUT)
        sock.close()


def _client(app) -> httpx.AsyncClient:
    """ONE event loop, no portal thread: the app really runs on this loop, and
    the response is still delivered message by message."""
    return httpx.AsyncClient(transport=_StreamingASGITransport(app=app),
                             base_url="http://testserver")


def _keepalive(chunk: bytes) -> bool:
    """A keepalive is whitespace and nothing else - ordinary JSON padding."""
    return chunk.strip() == b""


class _Turn:
    """What one witnessed completion showed, and WHEN it showed it."""

    def __init__(self):
        self.status_code: int | None = None
        self.model_answered_at_headers: float | None = None
        self.before_release: list[bytes] = []
        self.after_release: list[bytes] = []
        self.first_chunk_at: float | None = None
        self.last_keepalive_at: float | None = None
        self.model_answered_at_first_chunk: float | None = None
        self.model_began_at_release: bool | None = None
        self.model_answered_at_release: float | None = None
        self.answer_chunk_before_release: bytes | None = None
        self.also_waited = True

    @property
    def chunks(self) -> list[bytes]:
        return self.before_release + self.after_release

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    @property
    def keepalives_before_release(self) -> list[bytes]:
        return [chunk for chunk in self.before_release if _keepalive(chunk)]


async def _streamed_turn(client, world, gate: _ModelGate, *,
                         keepalives_wanted: int = 1,
                         also_wait: asyncio.Event | None = None) -> _Turn:
    """One completion, released only after the bytes this file asserts on exist.

    The model call is freed AFTER `keepalives_wanted` whitespace chunks have
    arrived (and after the stub's own "I am inside the call" flag is set, and
    after `also_wait` if one is given) - so "this byte arrived before the model
    answered" is a fact about the wire, not about the order this coroutine
    happened to resume in. The request is streamed with `client.stream(...)`:
    entering that context manager already means `http.response.start` arrived.
    """
    turn = _Turn()
    async with client.stream("POST", "/api/dsh-drivers/alpha/completion",
                             json=world.completion, headers=world.headers,
                             timeout=_TIMEOUT) as response:
        turn.status_code = response.status_code
        turn.model_answered_at_headers = gate.answered_at
        raw = response.aiter_raw()
        while len(turn.keepalives_before_release) < keepalives_wanted:
            try:
                chunk = await asyncio.wait_for(raw.__anext__(), _TIMEOUT)
            except StopAsyncIteration:
                break
            if turn.first_chunk_at is None:
                turn.first_chunk_at = time.monotonic()
                turn.model_answered_at_first_chunk = gate.answered_at
            turn.before_release.append(chunk)
            if not _keepalive(chunk):
                # The answer beat the keepalive: nothing more to wait for.
                turn.answer_chunk_before_release = chunk
                break
            turn.last_keepalive_at = time.monotonic()
        # Non-vacuity: the stub must really be INSIDE the call that is being
        # kept alive. This waits without releasing anything.
        turn.model_began_at_release = await asyncio.to_thread(gate.began.wait, _TIMEOUT)
        if also_wait is not None:
            try:
                await asyncio.wait_for(also_wait.wait(), _TIMEOUT)
            except asyncio.TimeoutError:
                turn.also_waited = False
        turn.model_answered_at_release = gate.answered_at
        gate.release.set()
        async for chunk in raw:
            turn.after_release.append(chunk)
    return turn


def _assert_the_object_is_the_last_chunk(turn: _Turn) -> None:
    """Every byte before the final chunk is keepalive whitespace."""
    assert turn.chunks, "the completion streamed no bytes at all"
    assert turn.chunks[-1].strip() == turn.body.strip(), (
        "the JSON object is not the last chunk: "
        f"{turn.chunks[-1][:80]!r} vs {turn.body.strip()[:80]!r}")
    for chunk in turn.chunks[:-1]:
        assert _keepalive(chunk), (
            f"a non-whitespace chunk arrived before the JSON: {chunk[:80]!r}")


def _recorded_model(world) -> list[str]:
    with world.db.get_connection() as conn:
        return [row["served_by"] for row in conn.execute(
            "SELECT served_by FROM dsh_driver_session_models "
            "WHERE project_id=? AND session_id=?", ("alpha", ROOT_SESSION))]


@pytest.fixture
def fast_keepalive(monkeypatch):
    """Patch the interval through its module constant - the switch a test has."""
    monkeypatch.setattr(routers, "_DSH_COMPLETION_KEEPALIVE_SEC", _FAST_KEEPALIVE)
    return _FAST_KEEPALIVE


def _hold_open(world, gate, monkeypatch) -> None:
    monkeypatch.setattr(world.gateway_class, "generate_native",
                        _stub_generate_native(gate))


# ── witness a: the response starts, and a byte lands, before the answer ──────

async def test_the_response_starts_and_a_keepalive_arrives_before_the_model_answers(
        stream_world, fast_keepalive, monkeypatch):
    """`headersTimeout` is about the START of the response, so this is the arm
    that failed on 2026-11-18: `client.stream(...)` must return while the model
    is still thinking, and a body byte must follow."""
    world = stream_world
    gate = _ModelGate()
    _hold_open(world, gate, monkeypatch)
    async with _client(world.app) as client:
        turn = await _streamed_turn(client, world, gate, keepalives_wanted=1)

    assert turn.status_code == 200
    assert turn.model_answered_at_headers is None, (
        "the response headers arrived only after the model had answered: the "
        "client's headersTimeout is still armed with nothing to show for it")
    assert gate.calls == 1, "the stubbed model call never ran"
    assert turn.keepalives_before_release, (
        "the body did not start with a whitespace keepalive")
    assert _keepalive(turn.keepalives_before_release[0])
    assert turn.model_answered_at_first_chunk is None, (
        "the first body byte arrived only after the model had answered")
    assert turn.model_began_at_release, (
        "the model call was never in flight: the witness would be vacuous")
    assert turn.model_answered_at_release is None, (
        "the model had already answered when the keepalive was released")


# ── witness a over a real socket (uvicorn), not just ASGI messages ───────────

async def test_a_real_socket_sends_the_headers_and_a_keepalive_before_the_answer(
        stream_world, served_world, fast_keepalive, monkeypatch):
    """The same arm on a real HTTP connection: uvicorn's own write path must not
    be where the byte waits for the model."""
    world = stream_world
    gate = _ModelGate()
    _hold_open(world, gate, monkeypatch)
    async with httpx.AsyncClient(base_url=served_world, timeout=_TIMEOUT) as client:
        turn = await _streamed_turn(client, world, gate, keepalives_wanted=1)

    assert turn.status_code == 200
    assert turn.model_answered_at_headers is None, (
        "over a real socket the headers arrived only after the model answered")
    assert gate.calls == 1
    assert turn.keepalives_before_release, (
        "over a real socket no keepalive byte arrived before the answer")
    assert turn.model_answered_at_first_chunk is None
    assert turn.model_began_at_release
    assert turn.model_answered_at_release is None


# ── witness b: it REPEATS, and the JSON is the last chunk ────────────────────

async def test_the_keepalive_repeats_while_the_model_is_outstanding(
        stream_world, fast_keepalive, monkeypatch):
    """One keepalive only covers the first 300 s: undici's `bodyTimeout` is also
    300 s, so the padding has to keep coming for as long as the call is open."""
    world = stream_world
    gate = _ModelGate()
    _hold_open(world, gate, monkeypatch)
    # Three keepalives span TWO whole intervals before the release.
    wanted = 3
    async with _client(world.app) as client:
        turn = await _streamed_turn(client, world, gate, keepalives_wanted=wanted)

    assert turn.status_code == 200
    assert gate.calls == 1
    assert turn.answer_chunk_before_release is None, (
        "the answer arrived before the keepalive cadence did")
    keepalives = turn.keepalives_before_release
    assert len(keepalives) >= 2, (
        f"only {len(keepalives)} whitespace chunk(s) arrived while the model "
        f"was outstanding: the keepalive does not repeat")
    assert all(_keepalive(chunk) for chunk in keepalives)
    assert turn.model_answered_at_release is None, (
        "the model had answered before the keepalive cadence was witnessed")
    # The chunks really were paced over the patched interval, not flushed at
    # once when the answer arrived: three chunks span two intervals.
    span = turn.last_keepalive_at - turn.first_chunk_at
    assert span >= fast_keepalive, (
        f"the {len(keepalives)} keepalives spanned only {span:.3f}s, less than "
        f"one {fast_keepalive:.3f}s interval: they were not paced")
    _assert_the_object_is_the_last_chunk(turn)
    payload = json.loads(turn.chunks[-1])
    assert payload["text"] == TEXT


# ── witness c: the body is the same object, six keys, same values ────────────

async def test_the_streamed_body_is_the_same_six_key_object(
        stream_world, fast_keepalive, monkeypatch):
    """A driver parses the body, so the whitespace is stripped explicitly and
    the OBJECT is compared - the padding must not become part of the contract's
    shape by accident, and the answering model must still be recorded."""
    world = stream_world
    gate = _ModelGate()
    _hold_open(world, gate, monkeypatch)
    async with _client(world.app) as client:
        turn = await _streamed_turn(client, world, gate, keepalives_wanted=2)

    assert turn.status_code == 200
    _assert_the_object_is_the_last_chunk(turn)
    body = turn.body.strip()
    assert body, "the completion body was nothing but keepalive whitespace"
    payload = json.loads(body)
    assert set(payload) == {"text", "tool_calls", "reasoning_content",
                            "truncated", "usage", "served_by"}
    assert payload["text"] == TEXT
    assert payload["tool_calls"] == TOOL_CALLS
    assert payload["reasoning_content"] == REASONING
    assert payload["truncated"] is False
    assert payload["usage"] == USAGE
    assert payload["served_by"] == ROOT_ENDPOINT
    # The record write still happens before the body is finished: the row is
    # there by the time the driver has the whole answer.
    assert _recorded_model(world) == [ROOT_ENDPOINT], (
        "the answering model was not recorded before the body ended")


# ── the trap: one session's lock must cover the STREAMED body ────────────────

async def test_two_turns_of_one_session_never_share_the_gateway(
        stream_world, fast_keepalive, monkeypatch):
    """The lock has to be acquired INSIDE the generator. Returned from inside
    `async with lock:`, the StreamingResponse releases it before the body runs,
    and a second turn of the same session would enter `generate_native` while
    the first is still paying for its answer - two turns re-binding one
    gateway's endpoint, prefix and budget under each other.
    """
    world = stream_world
    gate = _ModelGate()
    _hold_open(world, gate, monkeypatch)
    second_arrived = asyncio.Event()
    arrivals = {"count": 0, "answered_at": None}
    real_owned = routers.owned_off_loop

    async def counting_owned(project_id, request, runtime):
        # The endpoint's own first await: this fires when the request is really
        # being handled, and it is where "the second turn is in flight" is
        # recorded - WITH the first call's state at that instant.
        arrivals["count"] += 1
        if arrivals["count"] == 2:
            arrivals["answered_at"] = gate.answered_at
            second_arrived.set()
        return await real_owned(project_id, request, runtime)

    monkeypatch.setattr(routers, "owned_off_loop", counting_owned)
    async with _client(world.app) as client:
        first = asyncio.create_task(_streamed_turn(
            client, world, gate, keepalives_wanted=1, also_wait=second_arrived))
        # The second turn starts only once the FIRST one is demonstrably inside
        # the call (which also means it holds the session). Starting both at
        # once would let either win the lock, and the arm would then be about
        # the scheduler rather than about the lock.
        began = await asyncio.to_thread(gate.began.wait, _TIMEOUT)
        assert began, "the first turn never reached the model"
        second = asyncio.create_task(_streamed_turn(
            client, world, gate, keepalives_wanted=1))
        first_turn, second_turn = await asyncio.gather(first, second)

    assert arrivals["count"] == 2, "the second turn never reached the endpoint"
    assert first_turn.also_waited, "the second turn was not in flight in time"
    assert arrivals["answered_at"] is None, (
        "the second request arrived only after the first model call had "
        "returned: this witness would have been vacuous")
    assert gate.calls == 2, "the second turn never reached the model"
    assert gate.peak == 1, (
        f"{gate.peak} model calls were in flight at once for one session: the "
        f"per-session lock did not cover the streamed body")
    for turn in (first_turn, second_turn):
        assert turn.status_code == 200
        assert json.loads(turn.body.strip())["text"] == TEXT


# ── the shipped interval itself ──────────────────────────────────────────────

def test_the_shipped_keepalive_is_inside_the_client_timeouts():
    """Named, patchable, and far below the 300 s undici burns."""
    value = routers._DSH_COMPLETION_KEEPALIVE_SEC
    assert isinstance(value, float)
    assert 0 < value < 300, (
        f"a {value}s keepalive is no keepalive: the client's headersTimeout "
        f"and bodyTimeout are both 300 s")
